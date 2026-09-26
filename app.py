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
    "cancel": False,
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
                        out.append({"root": root, "rel": rel, "name": fn})
        else:
            try:
                for fn in os.listdir(root):
                    if fn.lower().endswith((".psd", ".psb")) and os.path.isfile(os.path.join(root, fn)):
                        out.append({"root": root, "rel": "", "name": fn})
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
WIN_W, WIN_H = 1280, 880          # اندازهٔ دلخواه پنجرهٔ برنامه
MIN_W, MIN_H = 980, 640


def screen_size():
    """اندازهٔ مانیتور اصلی (پیکسل منطقی)."""
    if IS_WIN:
        try:
            import ctypes
            u = ctypes.windll.user32
            w, h = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
            if w > 100 and h > 100:
                return w, h
        except Exception:
            pass
    try:
        import tkinter as tk
        r = tk.Tk()
        r.withdraw()
        w, h = r.winfo_screenwidth(), r.winfo_screenheight()
        r.destroy()
        if w > 100 and h > 100:
            return w, h
    except Exception:
        pass
    return 1920, 1080


def window_geometry():
    """اندازه و مختصات پنجره، طوری که دقیقاً وسط مانیتور بیفتد."""
    sw, sh = screen_size()
    w = max(MIN_W, min(WIN_W, int(sw * 0.86)))
    h = max(MIN_H, min(WIN_H, int(sh * 0.88)))
    w, h = min(w, sw), min(h, sh)
    x = max(0, (sw - w) // 2)
    y = max(0, (sh - h) // 2)
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
    d = Path(base) / "PSDLabelExporter" / "window"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return str(d)


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
            "--disable-features=Translate,DefaultBrowserPromptRefresh",
        ]
        try:
            kw = {}
            if IS_WIN:
                kw["creationflags"] = 0x00000008 | 0x08000000   # DETACHED_PROCESS | NO_WINDOW
            proc = subprocess.Popen(cmd, close_fds=True, **kw)
            threading.Thread(target=_watch_window, args=(proc,), daemon=True).start()
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

    def __init__(self):
        import win32com.client as win32  # noqa
        self.win32 = win32
        self.app = win32.Dispatch("Photoshop.Application")
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

    def export_jpeg(self, src, dst, quality=12, to8bit=True, to_rgb=False):
        doc = self.app.Open(src)
        try:
            try:
                doc.Flatten()
            except Exception:
                pass
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

            opts = self.win32.Dispatch("Photoshop.JPEGSaveOptions")
            opts.Quality = int(quality)
            try:
                opts.EmbedColorProfile = True
                opts.FormatOptions = 1   # psStandardBaseline
                opts.Matte = 1           # psNoMatte
            except Exception:
                pass
            doc.SaveAs(dst, opts, True, self.EXT_LOWER)
        finally:
            try:
                doc.Close(self.DO_NOT_SAVE)
            except Exception:
                pass


def unique_path(path):
    p = Path(path)
    if not p.exists():
        return str(p)
    stem, suf, parent, i = p.stem, p.suffix, p.parent, 2
    while (parent / f"{stem}_{i}{suf}").exists():
        i += 1
    return str(parent / f"{stem}_{i}{suf}")


def run_job(cfg):
    """کار اصلی: کپی PSD + ساخت JPEG با فتوشاپ."""
    items = cfg.get("items") or []
    out_dir = resolve_out_dir(cfg.get("outRoot", ""), cfg.get("folderName", "export"))
    do_jpeg = bool(cfg.get("doJpeg", True))
    do_copy = bool(cfg.get("doCopy", False))
    copy_sub = "PSD" if cfg.get("copyWhere", "sub") == "sub" else ""
    quality = int(cfg.get("quality", 12))
    to8 = bool(cfg.get("cvt8bit", True))
    torgb = bool(cfg.get("cvtRGB", False))
    date_suffix = cfg.get("dateSuffix", "")

    total = len(items) * ((1 if do_jpeg else 0) + (1 if do_copy else 0))
    jset(running=True, finished=False, total=total, done=0, ok=0, fail=0,
         current="", log=[], outDir=out_dir, error="", cancel=False)
    jlog(f"OUTPUT: {out_dir}")
    jlog(f"ITEMS : {len(items)}   JPEG={do_jpeg}  COPY={do_copy}")

    os.makedirs(out_dir, exist_ok=True)
    ps = None
    try:
        if do_jpeg and not DEMO:
            jset(current="در حال باز کردن فتوشاپ…")
            jlog("Starting Photoshop…")
            ps = Photoshop()
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
                    jlog(f"ERROR | copy {src} -> {e}")
                with JOB_LOCK:
                    JOB["done"] += 1

        # ---------- jpeg via photoshop ----------
        if do_jpeg:
            for it in items:
                if JOB["cancel"]:
                    break
                src = it["src"]
                base = Path(src).stem + (f"_{date_suffix}" if date_suffix else "")
                dst = unique_path(os.path.join(out_dir, base + ".jpg"))
                jset(current=f"فتوشاپ: {Path(src).name}")
                try:
                    if DEMO:
                        time.sleep(0.35)
                        Path(dst).write_bytes(b"demo")
                    else:
                        if not os.path.isfile(src):
                            raise FileNotFoundError(src)
                        ps.export_jpeg(src, dst, quality, to8, torgb)
                    with JOB_LOCK:
                        JOB["ok"] += 1
                    jlog(f"OK    | {dst}")
                except Exception as e:
                    with JOB_LOCK:
                        JOB["fail"] += 1
                    jlog(f"ERROR | {src} -> {e}")
                with JOB_LOCK:
                    JOB["done"] += 1

        # ---------- report ----------
        try:
            rep = Path(out_dir) / "_report.txt"
            with open(rep, "w", encoding="utf-8-sig") as f:
                f.write("\n".join(JOB["log"]))
                f.write(f"\n\nOK: {JOB['ok']}   FAILED: {JOB['fail']}\n")
        except Exception:
            pass

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
