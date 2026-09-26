#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PSD Label Exporter — Desktop (Windows + Photoshop)
==================================================
همان رابط کاربری HTML، ولی این بار با موتور پایتون:
  • فولدرها را واقعاً از روی دیسک اسکن می‌کند (بدون BAT ایندکس)
  • دکمهٔ «شروع خروجی» مستقیماً فتوشاپ را بالا می‌آورد و فایل‌ها را یکی‌یکی باز و JPEG می‌کند
  • پیشرفت لحظه‌ای را در خود اپ نشان می‌دهد

اجرا:
    pip install pywin32          (فقط ویندوز، برای کنترل فتوشاپ)
    python app.py                → مرورگر خودکار باز می‌شود

تست بدون فتوشاپ (هر سیستم‌عاملی):
    python app.py --demo
"""

import argparse
import json
import os
import shutil
import socket
import zipfile
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --------------------------------------------------------------------- paths
FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    BUNDLE = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    EXEDIR = Path(sys.executable).resolve().parent
else:
    BUNDLE = EXEDIR = Path(__file__).resolve().parent
HERE = BUNDLE

UI_CANDIDATES = [
    BUNDLE / "psd-label-exporter.html",
    EXEDIR / "psd-label-exporter.html",
    BUNDLE.parent / "psd-label-exporter.html",
    BUNDLE / "ui.html",
]
BRIDGE_CANDIDATES = [BUNDLE / "bridge.js", EXEDIR / "bridge.js"]


def _settings_path():
    """settings.json کنار exe؛ اگر مسیر نصب فقط‌خواندنی بود → %APPDATA%"""
    for base in (EXEDIR, Path(os.environ.get("APPDATA", str(Path.home()))) / "PSDLabelExporter"):
        try:
            base.mkdir(parents=True, exist_ok=True)
            probe = base / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return base / "settings.json"
        except Exception:
            continue
    return Path.home() / "psd-label-exporter-settings.json"


def _setup_std_streams():
    """در بیلد بدون کنسول (console=False) مقدار sys.stdout/sys.stderr برابر None است.
    هر print یا هر لاگِ سرور در آن حالت استثنا می‌دهد و پاسخ HTTP نیمه‌کاره می‌ماند
    (خطای ERR_EMPTY_RESPONSE در مرورگر). پس جریان‌ها را به یک فایل لاگ می‌بندیم."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    import tempfile
    stream = None
    for cand in (EXEDIR / "run.log", Path(tempfile.gettempdir()) / "psd-label-exporter.log"):
        try:
            stream = open(cand, "a", encoding="utf-8", errors="replace", buffering=1)
            break
        except Exception:
            continue
    if stream is None:
        stream = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    try:
        sys.stdout.write("\n===== %s | start =====\n" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        pass


_setup_std_streams()

SETTINGS = _settings_path()
IS_WIN = os.name == "nt"
DEMO = False
INSTANCE = os.urandom(8).hex()        # شناسهٔ یکتای این اجرا (برای تشخیص نسخهٔ زامبی روی همان پورت)

# ----------------------------------------------------------------------------- job state
JOB = {
    "running": False, "total": 0, "done": 0, "ok": 0, "fail": 0,
    "current": "", "log": [], "finished": False, "outDir": "", "error": "",
    "cancel": False, "failed": [], "files": [], "zipPath": "",
}
JOB_LOCK = threading.Lock()


def jset(**kw):
    with JOB_LOCK:
        JOB.update(kw)


def jlog(line):
    with JOB_LOCK:
        JOB["log"].append(line)
        if len(JOB["log"]) > 500:
            del JOB["log"][:-500]


# ----------------------------------------------------------------------------- helpers
def ui_file():
    for p in UI_CANDIDATES:
        if p.exists():
            return p
    return None


def resolve_out_dir(out_root: str, folder_name: str) -> str:
    out_root = (out_root or "").strip()
    if out_root in ("", "<DESKTOP>"):
        base = Path.home() / "Desktop"
    elif out_root == "<DOCUMENTS>":
        base = Path.home() / "Documents"
    else:
        base = Path(out_root)
    return str(base / folder_name)


def _file_info(root, rel, name, full):
    """اطلاعات یک فایل: نام، مسیر نسبی، حجم و تاریخ آخرین تغییر."""
    try:
        st = os.stat(full)
        return {"root": root, "rel": rel, "name": name,
                "size": int(st.st_size), "mtime": int(st.st_mtime)}
    except OSError:
        return {"root": root, "rel": rel, "name": name, "size": 0, "mtime": 0}


def scan_folders(folders, include_sub=True):
    """همهٔ PSDها را برمی‌گرداند؛ فیلتر زیرفولدر سمت رابط کاربری انجام می‌شود."""
    out = []
    for root in folders:
        root = (root or "").strip().rstrip("\\/")
        if not root or not os.path.isdir(root):
            continue
        if include_sub:
            for dirpath, _dirs, files in os.walk(root):
                rel = os.path.relpath(dirpath, root)
                rel = "" if rel == "." else rel.replace("/", "\\") + "\\"
                for fn in files:
                    if fn.lower().endswith((".psd", ".psb")):
                        out.append(_file_info(root, rel, fn, os.path.join(dirpath, fn)))
        else:
            try:
                for fn in os.listdir(root):
                    full = os.path.join(root, fn)
                    if fn.lower().endswith((".psd", ".psb")) and os.path.isfile(full):
                        out.append(_file_info(root, "", fn, full))
            except OSError:
                pass
    return out


def pick_folder(initial=""):
    """دیالوگ انتخاب فولدر بومی ویندوز."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(initialdir=initial or str(Path.home()))
        root.destroy()
        return path.replace("/", "\\") if path else ""
    except Exception:
        traceback.print_exc()
        return ""


def find_photoshop_exe():
    cands = []
    for var in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(var)
        if not base:
            continue
        adobe = Path(base) / "Adobe"
        if adobe.is_dir():
            cands += list(adobe.glob("*/Photoshop.exe"))
    return str(sorted(cands)[-1]) if cands else ""


# ----------------------------------------------------------------------------- app window
WIN_W, WIN_H = 1320, 1010         # اندازهٔ دلخواه پنجرهٔ برنامه (بلندتر = بدون اسکرول)
MIN_W, MIN_H = 980, 660


def work_area():
    """ناحیهٔ قابل استفادهٔ مانیتور اصلی: (x, y, w, h) — بدون تسک‌بار."""
    if IS_WIN:
        try:
            import ctypes
            from ctypes import wintypes
            rect = wintypes.RECT()
            # SPI_GETWORKAREA = 0x0030 → ناحیهٔ بدون تسک‌بار
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                w, h = rect.right - rect.left, rect.bottom - rect.top
                if w > 200 and h > 200:
                    return rect.left, rect.top, w, h
            u = ctypes.windll.user32
            w, h = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
            if w > 200 and h > 200:
                return 0, 0, w, h
        except Exception:
            pass
    try:
        import tkinter as tk
        r = tk.Tk()
        r.withdraw()
        w, h = r.winfo_screenwidth(), r.winfo_screenheight()
        r.destroy()
        if w > 200 and h > 200:
            return 0, 0, w, h
    except Exception:
        pass
    return 0, 0, 1920, 1080


def screen_size():
    _x, _y, w, h = work_area()
    return w, h


def window_geometry():
    """اندازه و مختصات پنجره: تا حد امکان بلند، ولی همیشه وسط ناحیهٔ قابل استفاده."""
    ox, oy, sw, sh = work_area()
    w = max(MIN_W, min(WIN_W, int(sw * 0.90)))
    h = max(MIN_H, min(WIN_H, int(sh * 0.96)))
    w, h = min(w, sw), min(h, sh)
    x = ox + max(0, (sw - w) // 2)
    y = oy + max(0, (sh - h) // 2)
    return w, h, x, y


def find_browser():
    """مسیر Chrome یا Edge برای باز کردن پنجرهٔ اختصاصی برنامه."""
    if IS_WIN:
        names = [
            r"Google\Chrome\Application\chrome.exe",
            r"Microsoft\Edge\Application\msedge.exe",
            r"BraveSoftware\Brave-Browser\Application\brave.exe",
        ]
        bases = [os.environ.get(v) for v in
                 ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA")]
        for base in [b for b in bases if b]:
            for n in names:
                p = Path(base) / n
                if p.exists():
                    return str(p)
        return ""
    for n in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
        p = shutil.which(n)
        if p:
            return p
    return ""


def profile_dir():
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    d = Path(base) / "PSDLabelExporter" / "window-v2"   # نسخهٔ جدید = کش آیکن قدیمی کروم دور ریخته می‌شود
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return str(d)


def icon_file():
    for base in (BUNDLE, EXEDIR):
        p = base / "assets" / "icon.ico"
        if p.exists():
            return str(p)
    return ""


def _win_icons(hwnd, ico):
    """آیکن واقعی (HICON) را از فایل .ico چندسایزی به پنجره می‌دهد.

    کروم در حالت app فقط favicon را مقیاس می‌دهد و در تسک‌بار محو می‌شود؛
    با WM_SETICON خودِ ویندوز تصویر هم‌اندازهٔ دقیق را از داخل .ico برمی‌دارد.
    """
    import ctypes
    u = ctypes.windll.user32
    IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x00000010, 0x0080
    ICON_SMALL, ICON_BIG = 0, 1
    small_px = u.GetSystemMetrics(49) or 16     # SM_CXSMICON
    for which, px in ((ICON_SMALL, small_px), (ICON_BIG, 64)):
        h = u.LoadImageW(None, ico, IMAGE_ICON, px, px, LR_LOADFROMFILE)
        if h:
            u.SendMessageW(hwnd, WM_SETICON, which, h)


def _set_app_id(hwnd, app_id="PSDLabelExporter.Desktop"):
    """شناسهٔ اپ برای پنجره؛ باعث می‌شود ویندوز آن را یک برنامهٔ مستقل
    (نه یک پنجرهٔ کروم) بشناسد و آیکن خودِ پنجره را در تسک‌بار نشان دهد."""
    try:
        from win32com.propsys import propsys, pscon  # noqa
        store = propsys.SHGetPropertyStoreForWindow(hwnd, propsys.IID_IPropertyStore)
        store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(app_id))
        store.Commit()
        return True
    except Exception:
        return False


def brand_window(proc, timeout=25.0):
    """منتظر می‌ماند تا پنجرهٔ برنامه ساخته شود، بعد آیکن و هویت آن را ست می‌کند."""
    if not IS_WIN:
        return
    ico = icon_file()
    if not ico:
        return
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def find_windows(pid):
            found = []

            def cb(hwnd, _l):
                p = wintypes.DWORD()
                u.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
                if p.value == pid and u.IsWindowVisible(hwnd):
                    buf = ctypes.create_unicode_buffer(64)
                    u.GetClassNameW(hwnd, buf, 64)
                    if buf.value.startswith("Chrome_WidgetWin"):
                        found.append(hwnd)
                return True

            u.EnumWindows(EnumProc(cb), 0)
            return found

        t0 = time.time()
        hwnds = []
        while time.time() - t0 < timeout:
            hwnds = find_windows(proc.pid)
            if hwnds:
                break
            time.sleep(0.4)
        if not hwnds:
            return
        # چند بار تکرار می‌کنیم چون کروم بعد از لود صفحه آیکن خودش را ست می‌کند
        for i in range(4):
            for hwnd in find_windows(proc.pid) or hwnds:
                if i == 0:
                    _set_app_id(hwnd)
                _win_icons(hwnd, ico)
            time.sleep(1.2)
    except Exception:
        pass


def switch_keyboard_en():
    """چیدمان کیبورد پنجرهٔ فعال را به انگلیسی (US) تغییر می‌دهد."""
    if not IS_WIN:
        return False
    try:
        import ctypes
        u = ctypes.windll.user32
        KLF_ACTIVATE = 0x00000001
        hkl = u.LoadKeyboardLayoutW("00000409", KLF_ACTIVATE)   # en-US
        if not hkl:
            return False
        hwnd = u.GetForegroundWindow()
        WM_INPUTLANGCHANGEREQUEST = 0x0050
        INPUTLANGCHANGE_FORWARD = 0x0002
        if hwnd:
            u.PostMessageW(hwnd, WM_INPUTLANGCHANGEREQUEST, INPUTLANGCHANGE_FORWARD, hkl)
            # پنجرهٔ فرزند (خود ناحیهٔ ورودی) هم پیام را بگیرد
            child = u.GetFocus() or 0
            if child:
                u.PostMessageW(child, WM_INPUTLANGCHANGEREQUEST, INPUTLANGCHANGE_FORWARD, hkl)
        u.ActivateKeyboardLayout(hkl, 0)
        return True
    except Exception:
        return False


def _force_foreground(hwnd):
    """پنجره را واقعاً جلو می‌آورد (ویندوز جلوی SetForegroundWindow از پراسس غیرفعال را می‌گیرد)."""
    import ctypes
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    SW_RESTORE, SW_SHOW = 9, 5
    try:
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, SW_RESTORE)
        else:
            u.ShowWindow(hwnd, SW_SHOW)
        fg = u.GetForegroundWindow()
        cur = k.GetCurrentThreadId()
        other = u.GetWindowThreadProcessId(fg, None) if fg else 0
        target = u.GetWindowThreadProcessId(hwnd, None)
        # اجازهٔ فعال‌سازی از پراسس ما
        try:
            u.AllowSetForegroundWindow(-1)      # ASFW_ANY
        except Exception:
            pass
        for t in (other, target):
            if t and t != cur:
                u.AttachThreadInput(cur, t, True)
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)
        u.SetActiveWindow(hwnd)
        for t in (other, target):
            if t and t != cur:
                u.AttachThreadInput(cur, t, False)
        return True
    except Exception:
        return False


def _raise_explorer(folder, timeout=4.0):
    """پنجرهٔ اکسپلورری که همین الان برای این پوشه باز شده را پیدا و جلو می‌آورد."""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    want = os.path.basename(os.path.normpath(folder)).lower() or folder.lower()
    t0 = time.time()
    while time.time() - t0 < timeout:
        hits = []

        def cb(hwnd, _l):
            if not u.IsWindowVisible(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(64)
            u.GetClassNameW(hwnd, cls, 64)
            if cls.value in ("CabinetWClass", "ExploreWClass"):
                ttl = ctypes.create_unicode_buffer(512)
                u.GetWindowTextW(hwnd, ttl, 512)
                if want in (ttl.value or "").lower():
                    hits.append(hwnd)
            return True

        u.EnumWindows(EnumProc(cb), 0)
        if hits:
            _force_foreground(hits[-1])
            return True
        time.sleep(0.25)
    return False


def reveal_in_explorer(path):
    """پوشهٔ فایل را در اکسپلورر باز می‌کند، فایل را انتخاب می‌کند و پنجره را جلو می‌آورد."""
    if not IS_WIN or not path:
        return False
    p = os.path.normpath(str(path))
    folder = p if os.path.isdir(p) else os.path.dirname(p)
    if not os.path.isdir(folder):
        return False
    try:
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(-1)   # اجازه به اکسپلورر برای آمدن جلو
        except Exception:
            pass
        if os.path.isfile(p):
            subprocess.Popen(["explorer", "/select,", p], close_fds=True)
        else:
            os.startfile(folder)  # noqa
        threading.Thread(target=_raise_explorer, args=(folder,), daemon=True).start()
        return True
    except Exception:
        traceback.print_exc()
        return False


def _watch_window(proc):
    """اگر کاربر پنجرهٔ برنامه را ببندد، سرور هم بسته شود.

    برای اطمینان، فقط وقتی پنجره واقعاً باز مانده باشد (بیش از ۱۰ ثانیه) خاموش می‌کنیم؛
    اگر مرورگر بلافاصله کار را به نمونهٔ دیگری بسپارد و خارج شود، سرور روشن می‌ماند.
    """
    t0 = time.time()
    try:
        proc.wait()
    except Exception:
        return
    if time.time() - t0 < 10:
        return
    if JOB.get("running"):          # وسط گرفتن خروجی هستیم → قطع نکن
        return
    print("پنجرهٔ برنامه بسته شد؛ خروج.")
    os._exit(0)


def open_app_window(url, force_default=False):
    """پنجرهٔ برنامه را دقیقاً وسط مانیتور باز می‌کند."""
    w, h, x, y = window_geometry()
    exe = "" if force_default else find_browser()
    if exe:
        import subprocess
        cmd = [
            exe,
            "--app=" + url,
            f"--window-size={w},{h}",
            f"--window-position={x},{y}",
            "--user-data-dir=" + profile_dir(),
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-translate",
            "--disable-features=Translate,TranslateUI,DefaultBrowserPromptRefresh",
            "--lang=en-US",          # چیدمان LTR برای فریم پنجره → دکمه‌های بستن/کوچک/بزرگ سمت راست
            "--force-ui-direction=ltr",
            "--disable-sync",
            "--app-auto-launched",
        ]
        try:
            kw = {}
            if IS_WIN:
                kw["creationflags"] = 0x00000008 | 0x08000000   # DETACHED_PROCESS | NO_WINDOW
            proc = subprocess.Popen(cmd, close_fds=True, **kw)
            threading.Thread(target=_watch_window, args=(proc,), daemon=True).start()
            threading.Thread(target=brand_window, args=(proc,), daemon=True).start()
            return True
        except Exception:
            traceback.print_exc()
    # fallback: مرورگر پیش‌فرض (وسط‌چین کردنش دست ویندوز است)
    try:
        webbrowser.open(url)
    except Exception:
        print("مرورگری پیدا نشد؛ این آدرس را دستی باز کن: " + url)
    return False


# ----------------------------------------------------------------------------- Photoshop engine
class Photoshop:
    """کنترل فتوشاپ از طریق COM (pywin32)."""

    # ثابت‌های عددی فتوشاپ
    DO_NOT_SAVE = 2          # psDoNotSaveChanges
    EXT_LOWER = 2            # psLowercase
    MODE_RGB = 2             # psConvertToRGB
    MODE_GRAY = 1            # psConvertToGrayscale
    DOC_RGB, DOC_CMYK, DOC_INDEXED, DOC_BITMAP = 2, 3, 6, 5

    def __init__(self, tries=6, wait=2.5, log=None):
        import pythoncom  # noqa
        import win32com.client as win32  # noqa
        self.win32 = win32
        self.app = None
        last = None
        for i in range(tries):
            try:
                self.app = win32.Dispatch("Photoshop.Application")
                break
            except Exception as e:
                last = e
                if log:
                    log(f"WARN | اتصال به فتوشاپ ناموفق ({i+1}/{tries}): {e}")
                time.sleep(wait)
        if self.app is None:
            raise RuntimeError(
                "اتصال به فتوشاپ برقرار نشد: " + str(last) +
                " — مطمئن شو فتوشاپ نصب است و یک‌بار دستی اجرا شده باشد."
            )
        try:
            self.app.Visible = True
        except Exception:
            pass
        try:
            self.app.BringToFront()
        except Exception:
            pass
        try:
            self.app.DisplayDialogs = 3  # psDisplayNoDialogs
        except Exception:
            pass

    def version(self):
        try:
            return str(self.app.Version)
        except Exception:
            return "?"

    @staticmethod
    def _busy(e):
        """خطاهای «الان نمی‌توانم، مشغولم» که با تکرار حل می‌شوند."""
        txt = str(e)
        return ("call was rejected" in txt.lower() or "-2147418111" in txt
                or "0x80010001" in txt or "RPC_E_CALL_REJECTED" in txt)

    def retry(self, fn, tries=5, wait=1.5):
        last = None
        for _ in range(tries):
            try:
                return fn()
            except Exception as e:
                last = e
                if not self._busy(e):
                    raise
                time.sleep(wait)
        raise last

    def flatten(self, doc):
        try:
            doc.Flatten()
        except Exception:
            pass

    def open_prepared(self, src, to8bit=True, to_rgb=False, flatten=True):
        """فایل را باز می‌کند و عمق رنگ/مُد را آماده می‌کند.

        اگر flatten=False باشد، لایه‌ها دست‌نخورده می‌مانند (برای PDF لایه‌باز).
        """
        doc = self.retry(lambda: self.app.Open(src))
        try:
            if flatten:
                self.flatten(doc)
            if to8bit:
                try:
                    if int(doc.BitsPerChannel) != 8:
                        doc.BitsPerChannel = 8
                except Exception:
                    pass
            try:
                mode = int(doc.Mode)
            except Exception:
                mode = self.DOC_RGB
            if to_rgb and mode != self.DOC_RGB:
                doc.ChangeMode(self.MODE_RGB)
            elif mode == self.DOC_INDEXED:
                doc.ChangeMode(self.MODE_RGB)
            elif mode == self.DOC_BITMAP:
                doc.ChangeMode(self.MODE_GRAY)

            return doc
        except Exception:
            try:
                doc.Close(self.DO_NOT_SAVE)
            except Exception:
                pass
            raise

    def save_jpeg(self, doc, dst, quality=12):
        opts = self.win32.Dispatch("Photoshop.JPEGSaveOptions")
        opts.Quality = int(quality)
        try:
            opts.EmbedColorProfile = True
            opts.FormatOptions = 1   # psStandardBaseline
            opts.Matte = 1           # psNoMatte
        except Exception:
            pass
        self.retry(lambda: doc.SaveAs(dst, opts, True, self.EXT_LOWER))

    @staticmethod
    def _set(obj, **props):
        """هر خاصیت را جدا ست می‌کند تا یک نام ناشناخته بقیه را خراب نکند."""
        for k, v in props.items():
            try:
                setattr(obj, k, v)
            except Exception:
                pass

    def save_pdf(self, doc, dst, quality=12, layered=True):
        """PDF خروجی.

        layered=True  → «Photoshop PDF» با لایه‌های قابل ویرایش (PreserveEditing/Layers)
        layered=False → PDF تخت و سبک با فشرده‌سازی JPEG
        """
        opts = self.win32.Dispatch("Photoshop.PDFSaveOptions")
        if layered:
            self._set(opts,
                      PreserveEditing=True,      # همان تیکِ «Preserve Photoshop Editing Capabilities»
                      Layers=True,
                      AlphaChannels=True,
                      SpotColors=True,
                      Annotations=True,
                      EmbedColorProfile=True,
                      EmbedThumbnail=True,
                      OptimizeForWeb=False,
                      Compatibility=2)           # psPDF16 (Acrobat 7+) — لایه‌ها را نگه می‌دارد
        else:
            self._set(opts,
                      PreserveEditing=False,
                      Layers=False,
                      EmbedColorProfile=True,
                      Encoding=3,                # psPDFJPEG
                      JPEGQuality=int(quality))
        self.retry(lambda: doc.SaveAs(dst, opts, True, self.EXT_LOWER))

    def save_tiff(self, doc, dst):
        opts = self.win32.Dispatch("Photoshop.TiffSaveOptions")
        try:
            opts.ImageCompression = 2         # psTiffLZW
            opts.Layers = False
            opts.EmbedColorProfile = True
        except Exception:
            pass
        self.retry(lambda: doc.SaveAs(dst, opts, True, self.EXT_LOWER))

    def close_doc(self, doc):
        try:
            doc.Close(self.DO_NOT_SAVE)
        except Exception:
            pass


def make_zip(out_dir):
    """همهٔ فایل‌های پوشهٔ خروجی را در یک zip کنار خودشان می‌گذارد."""
    folder = Path(out_dir)
    zip_path = unique_path(str(folder / (folder.name + ".zip")))
    files = [p for p in sorted(folder.rglob("*")) if p.is_file() and p.name != Path(zip_path).name]
    total = len(files) or 1
    jset(current="در حال فشرده‌سازی…")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for i, f in enumerate(files, 1):
            if JOB["cancel"]:
                break
            jset(current=f"فشرده‌سازی ({i}/{total}): {f.name}")
            z.write(f, f.relative_to(folder).as_posix())
    return zip_path


def unique_path(path):
    p = Path(path)
    if not p.exists():
        return str(p)
    stem, suf, parent, i = p.stem, p.suffix, p.parent, 2
    while (parent / f"{stem}_{i}{suf}").exists():
        i += 1
    return str(parent / f"{stem}_{i}{suf}")


def run_job(cfg):
    """پوستهٔ امن: COM را برای همین ترد آماده می‌کند و بعد کار اصلی را اجرا می‌کند.

    بدون CoInitialize، هر تماس COM در تردِ غیر اصلی با خطای
    «CoInitialize has not been called» شکست می‌خورد.
    """
    co = None
    if IS_WIN and not DEMO:
        try:
            import pythoncom
            pythoncom.CoInitialize()          # apartment-threaded؛ مدل موردنیاز فتوشاپ
            co = pythoncom
            jlog("COM initialized for worker thread.")
        except Exception as e:
            jlog(f"WARN | CoInitialize failed: {e}")
    try:
        _run_job(cfg)
    finally:
        if co is not None:
            try:
                co.CoUninitialize()
            except Exception:
                pass


def _run_job(cfg):
    """کار اصلی: کپی PSD + ساخت JPEG/PDF/TIFF با فتوشاپ."""
    items = cfg.get("items") or []
    out_dir = resolve_out_dir(cfg.get("outRoot", ""), cfg.get("folderName", "export"))
    do_jpeg = bool(cfg.get("doJpeg", True))
    do_pdf = bool(cfg.get("doPdf", False))
    pdf_layers = bool(cfg.get("pdfLayers", True))    # پیش‌فرض: PDF لایه‌باز
    do_zip = bool(cfg.get("doZip", False))
    do_tiff = bool(cfg.get("doTiff", False))
    do_copy = bool(cfg.get("doCopy", False))
    copy_sub = "PSD" if cfg.get("copyWhere", "sub") == "sub" else ""
    quality = int(cfg.get("quality", 12))
    to8 = bool(cfg.get("cvt8bit", True))
    torgb = bool(cfg.get("cvtRGB", False))
    date_suffix = cfg.get("dateSuffix", "")
    do_report = bool(cfg.get("doReport", False))   # پیش‌فرض: گزارش ساخته نشود

    fmts = [f for f, on in (("jpg", do_jpeg), ("pdf", do_pdf), ("tif", do_tiff)) if on]
    use_ps = bool(fmts)
    total = len(items) * (len(fmts) + (1 if do_copy else 0))
    jset(running=True, finished=False, total=total, done=0, ok=0, fail=0,
         current="", log=[], outDir=out_dir, error="", cancel=False, failed=[], zipPath="")
    jlog(f"OUTPUT: {out_dir}")
    jlog(f"ITEMS : {len(items)}   FORMATS={','.join(fmts) or '-'}  COPY={do_copy}"
         + (f"  PDF={'layered' if pdf_layers else 'flat'}" if do_pdf else "")
         + (f"  ZIP=yes" if do_zip else ""))

    os.makedirs(out_dir, exist_ok=True)
    ps = None
    try:
        if use_ps and not DEMO:
            jset(current="در حال باز کردن فتوشاپ…")
            jlog("Starting Photoshop…")
            ps = Photoshop(log=jlog)
            jlog(f"Photoshop version: {ps.version()}")

        # ---------- copy originals ----------
        if do_copy:
            dest_dir = os.path.join(out_dir, copy_sub) if copy_sub else out_dir
            os.makedirs(dest_dir, exist_ok=True)
            for it in items:
                if JOB["cancel"]:
                    break
                src = it["src"]
                name = Path(src).stem + (f"_{date_suffix}" if date_suffix else "") + Path(src).suffix
                dst = unique_path(os.path.join(dest_dir, name))
                jset(current=f"کپی: {Path(src).name}")
                try:
                    shutil.copy2(src, dst)
                    with JOB_LOCK:
                        JOB["ok"] += 1
                    jlog(f"COPY  | {dst}")
                except Exception as e:
                    with JOB_LOCK:
                        JOB["fail"] += 1
                        if src not in JOB["failed"]:
                            JOB["failed"].append(src)
                    jlog(f"ERROR | copy {src} -> {e}")
                with JOB_LOCK:
                    JOB["done"] += 1

        # ---------- jpeg / pdf / tiff via photoshop ----------
        if fmts:
            for it in items:
                if JOB["cancel"]:
                    break
                src = it["src"]
                base = Path(src).stem + (f"_{date_suffix}" if date_suffix else "")
                jset(current=f"فتوشاپ: {Path(src).name}")
                doc = None
                flattened = False
                # PDF لایه‌باز باید قبل از فلت‌شدن ذخیره شود
                ordered = ([f for f in fmts if f == "pdf"] + [f for f in fmts if f != "pdf"]) \
                    if (do_pdf and pdf_layers) else fmts
                try:
                    if not DEMO and not os.path.isfile(src):
                        raise FileNotFoundError(src)
                    if not DEMO:
                        # اگر PDF لایه‌باز خواسته شده، فایل بدون فلت باز می‌شود
                        doc = ps.open_prepared(src, to8, torgb,
                                               flatten=not (do_pdf and pdf_layers))
                    for fmt in ordered:
                        if JOB["cancel"]:
                            break
                        dst = unique_path(os.path.join(out_dir, base + "." + fmt))
                        try:
                            if DEMO:
                                time.sleep(0.3)
                                Path(dst).write_bytes(b"demo")
                            elif fmt == "pdf":
                                ps.save_pdf(doc, dst, quality, layered=pdf_layers)
                            else:
                                # JPEG و TIFF تخت لازم دارند؛ بعد از PDF لایه‌باز فلت می‌کنیم
                                if do_pdf and pdf_layers and not flattened:
                                    ps.flatten(doc)
                                    flattened = True
                                if fmt == "jpg":
                                    ps.save_jpeg(doc, dst, quality)
                                else:
                                    ps.save_tiff(doc, dst)
                            with JOB_LOCK:
                                JOB["ok"] += 1
                            jlog(f"{fmt.upper():5s} | {dst}")
                        except Exception as e:
                            with JOB_LOCK:
                                JOB["fail"] += 1
                                if src not in JOB["failed"]:
                                    JOB["failed"].append(src)
                            jlog(f"ERROR | {fmt} {src} -> {e}")
                        with JOB_LOCK:
                            JOB["done"] += 1
                except Exception as e:
                    with JOB_LOCK:
                        JOB["fail"] += len(fmts)
                        JOB["done"] += len(fmts)
                        if src not in JOB["failed"]:
                            JOB["failed"].append(src)
                    jlog(f"ERROR | {src} -> {e}")
                finally:
                    if doc is not None:
                        ps.close_doc(doc)

        # ---------- report (اختیاری) ----------
        try:
            if not do_report:
                raise StopIteration
            rep = Path(out_dir) / "_report.txt"
            with open(rep, "w", encoding="utf-8-sig") as f:
                f.write("\n".join(JOB["log"]))
                f.write(f"\n\nOK: {JOB['ok']}   FAILED: {JOB['fail']}\n")
        except StopIteration:
            pass
        except Exception:
            pass

        # ---------- فشرده‌سازی خروجی (اختیاری) ----------
        if do_zip and not JOB["cancel"]:
            try:
                zip_path = make_zip(out_dir)
                jset(zipPath=zip_path)
                jlog(f"ZIP   | {zip_path}")
            except Exception as e:
                jlog(f"ERROR | zip -> {e}")

        jset(current="پایان", finished=True, running=False)
        jlog(f"DONE. ok={JOB['ok']} fail={JOB['fail']}")
        try:
            if IS_WIN:
                os.startfile(out_dir)  # noqa
        except Exception:
            pass
    except Exception as e:
        traceback.print_exc()
        jset(running=False, finished=True, error=str(e), current="خطا")
        jlog(f"FATAL | {e}")


# ----------------------------------------------------------------------------- HTTP server
class Server(ThreadingHTTPServer):
    """سرور با bind انحصاری.

    ویندوز با SO_REUSEADDR اجازه می‌دهد دو پراسس هم‌زمان روی یک پورت bind کنند؛
    نتیجه این می‌شود که نسخهٔ قدیمی/زامبی همچنان درخواست‌ها را بگیرد و
    نسخهٔ جدید — با اینکه سالم بالا آمده — هیچ درخواستی نبیند (ERR_EMPTY_RESPONSE).
    """

    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self):
        if IS_WIN:
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except Exception:
                pass
        ThreadingHTTPServer.server_bind(self)


class Handler(BaseHTTPRequestHandler):
    server_version = "PSDLabelExporter/1.0"

    def log_message(self, fmt, *args):  # کمتر شلوغ + هرگز استثنا ندهد
        try:
            if "/api/progress" in (args[0] if args else ""):
                return
            out = sys.stderr or sys.stdout
            if out is not None:
                out.write("  %s\n" % (fmt % args))
        except Exception:
            pass

    def handle_one_request(self):
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True
        except Exception:
            try:
                traceback.print_exc()
            except Exception:
                pass
            try:
                self.close_connection = True
            except Exception:
                pass

    # ---------- utils ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---------- GET ----------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self.serve_ui()
        if path in ("/favicon.ico", "/icon.ico", "/icon.png"):
            name = "icon.png" if path.endswith(".png") else "icon.ico"
            ctype = "image/png" if path.endswith(".png") else "image/x-icon"
            for base in (BUNDLE, EXEDIR):
                cand = base / "assets" / name
                if cand.exists():
                    return self._send(200, cand.read_bytes(), ctype)
            return self._send(404, b"", ctype)
        if path == "/manifest.json":
            return self._json({
                "name": "سامانه خروجی لیبل‌ها",
                "short_name": "خروجی لیبل",
                "start_url": "/",
                "display": "standalone",
                "background_color": "#eef2f9",
                "theme_color": "#123a8f",
                "icons": [
                    {"src": "/icon.png", "sizes": "256x256", "type": "image/png", "purpose": "any"},
                    {"src": "/favicon.ico", "sizes": "16x16 24x24 32x32 48x48 64x64 128x128 256x256",
                     "type": "image/x-icon"},
                ],
            })
        if path == "/api/ping":
            return self._json({
                "ok": True, "token": INSTANCE, "os": os.name, "demo": DEMO,
                "photoshopExe": find_photoshop_exe() if IS_WIN else "",
                "pywin32": _has_pywin32(),
            })
        if path == "/api/progress":
            with JOB_LOCK:
                return self._json(dict(JOB))
        if path == "/api/settings":
            try:
                return self._json(json.loads(SETTINGS.read_text(encoding="utf-8")))
            except Exception:
                return self._json({})
        return self._send(404, "not found", "text/plain; charset=utf-8")

    # ---------- POST ----------
    def do_POST(self):
        path = self.path.split("?")[0]
        b = self._body()
        try:
            if path == "/api/pick-folder":
                return self._json({"path": pick_folder(b.get("initial", ""))})
            if path == "/api/kbd-en":
                return self._json({"ok": switch_keyboard_en()})
            if path == "/api/reveal":
                return self._json({"ok": reveal_in_explorer(b.get("path", ""))})
            if path == "/api/check":
                out, missing, total_bytes = [], [], 0
                for it in (b.get("items") or []):
                    src = it.get("src") if isinstance(it, dict) else str(it)
                    info = {"src": src, "exists": False, "size": 0, "mtime": 0}
                    try:
                        st = os.stat(src)
                        info.update(exists=True, size=int(st.st_size), mtime=int(st.st_mtime))
                        total_bytes += int(st.st_size)
                    except OSError:
                        missing.append(src)
                    out.append(info)
                return self._json({"items": out, "missing": missing,
                                   "totalBytes": total_bytes, "count": len(out)})
            if path == "/api/scan":
                files = scan_folders(b.get("folders") or [], bool(b.get("includeSub", True)))
                return self._json({"files": files, "count": len(files)})
            if path == "/api/export":
                if JOB["running"]:
                    return self._json({"error": "یک خروجی در حال اجراست."}, 409)
                if not (b.get("items") or []):
                    return self._json({"error": "سبد خالی است."}, 400)
                threading.Thread(target=run_job, args=(b,), daemon=True).start()
                return self._json({"started": True})
            if path == "/api/cancel":
                jset(cancel=True)
                return self._json({"ok": True})
            if path == "/api/open-folder":
                p = b.get("path") or JOB.get("outDir")
                if p and IS_WIN and os.path.isdir(p):
                    os.startfile(p)  # noqa
                return self._json({"ok": True})
            if path == "/api/settings":
                SETTINGS.write_text(json.dumps(b, ensure_ascii=False, indent=1), encoding="utf-8")
                return self._json({"ok": True})
        except Exception as e:
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)
        return self._json({"error": "unknown endpoint"}, 404)

    # ---------- UI ----------
    def serve_ui(self):
        f = ui_file()
        if not f:
            return self._send(500, "psd-label-exporter.html پیدا نشد.", "text/html; charset=utf-8")
        html = f.read_text(encoding="utf-8")
        bridge = ""
        for bp in BRIDGE_CANDIDATES:
            if bp.exists():
                bridge = bp.read_text(encoding="utf-8")
                break
        if bridge:
            html = html.replace("</body>", "<script>\n" + bridge + "\n</script>\n</body>")
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")


def _has_pywin32():
    try:
        import win32com.client  # noqa
        return True
    except Exception:
        return False


def port_taken(port):
    """آیا کسی روی این پورت گوش می‌دهد؟ (حتی اگر پاسخ درستی ندهد)"""
    with socket.socket() as s:
        s.settimeout(0.6)
        try:
            return s.connect_ex(("127.0.0.1", port)) == 0
        except OSError:
            return True


def probe(port, timeout=2.0):
    """توکن نسخه‌ای که روی این پورت جواب می‌دهد؛ None یعنی سالم جواب نداد."""
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")).get("token")
    except Exception:
        return None


def start_server(host, preferred, log=print):
    """سرور را روی اولین پورت واقعاً آزاد بالا می‌آورد و صحت پاسخ‌دهی را تست می‌کند.

    خروجی: (srv, port, other_instance_port)
    """
    for port in [preferred] + [p for p in range(preferred + 1, preferred + 25) if p != preferred]:
        if port_taken(port):
            tok = probe(port)
            if tok:
                log(f"  پورت {port}: یک نسخهٔ سالم از برنامه از قبل باز است.")
                return None, port, port
            log(f"  پورت {port} اشغال است (پاسخ سالم نمی‌دهد) → پورت بعدی.")
            continue
        try:
            srv = Server((host, port), Handler)
        except OSError as e:
            log(f"  پورت {port} قابل استفاده نیست ({e}) → پورت بعدی.")
            continue
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        if probe(port, 6.0) == INSTANCE:
            return srv, port, 0
        log(f"  ⚠️ پورت {port} پاسخ این نسخه را برنمی‌گرداند (نسخهٔ دیگری آن را گرفته) → پورت بعدی.")
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
    return None, 0, 0


def main():
    global DEMO
    ap = argparse.ArgumentParser(description="PSD Label Exporter — desktop server")
    ap.add_argument("--demo", action="store_true", help="بدون فتوشاپ، فقط شبیه‌سازی")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--default-browser", action="store_true",
                    help="به‌جای پنجرهٔ اختصاصی، مرورگر پیش‌فرض باز شود")
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    DEMO = a.demo

    import builtins

    def say(*a):
        try:
            builtins.print(*a, flush=True)
        except Exception:
            pass

    print = say  # noqa: A001  (چاپ امن حتی وقتی کنسولی وجود ندارد)

    if not ui_file():
        print("!! فایل psd-label-exporter.html کنار app.py پیدا نشد.")
        sys.exit(1)

    srv, port, other = start_server(a.host, a.port or 8777, print)
    if srv is None and other:
        print(f"برنامه از قبل روی پورت {other} باز است؛ همان پنجره را می‌آورم بالا.")
        if not a.no_browser:
            open_app_window(f"http://127.0.0.1:{other}/", a.default_browser)
        return
    if srv is None:
        print("!! هیچ پورت آزادی پیدا نشد. یک‌بار همهٔ نسخه‌های برنامه را ببند "
              "(بستن-برنامه.bat) و دوباره اجرا کن.")
        sys.exit(1)
    url = f"http://127.0.0.1:{port}/"
    print("=" * 62)
    print("  PSD Label Exporter — Desktop")
    print(f"  UI      : {url}")
    print(f"  Windows : {IS_WIN}   pywin32: {_has_pywin32()}   demo: {DEMO}")
    if IS_WIN and not DEMO:
        print(f"  Photoshop: {find_photoshop_exe() or 'با COM اجرا می‌شود'}")
    _g = window_geometry()
    print(f"  Window  : {_g[0]}x{_g[1]} @ {_g[2]},{_g[3]}  (وسط مانیتور)")
    print("  برای بستن: Ctrl+C")
    print("=" * 62)
    if not a.no_browser:
        threading.Timer(0.8, lambda: open_app_window(url, a.default_browser)).start()
    print("  SELFTEST: OK — سرور روی این پورت پاسخ می‌دهد.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nbye.")
        try:
            srv.shutdown()
        except Exception:
            pass


def _crash(exc):
    """در حالت exe خطا را جایی ثبت کن که کاربر ببیند."""
    txt = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        (EXEDIR / "error.log").write_text(txt, encoding="utf-8")
    except Exception:
        pass
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk(); root.withdraw()
        messagebox.showerror("PSD Label Exporter", "اجرای برنامه با خطا متوقف شد:\n\n" + str(exc))
        root.destroy()
    except Exception:
        print(txt)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as _e:
        _crash(_e)
        sys.exit(1)
