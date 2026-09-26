# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec — PSD Label Exporter
حالت onedir (یک exe + چند فایل کنارش) تا اجرای برنامه سریع باشد.
ساخت دستی:  pyinstaller build.spec --noconfirm --clean
"""

block_cipher = None

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("psd-label-exporter.html", "."),
        ("bridge.js", "."),
        ("assets/icon.ico", "assets"),
    ],
    hiddenimports=[
        "win32com.client",
        "win32api",
        "pythoncom",
        "pywintypes",
        "tkinter",
        "tkinter.filedialog",
        "tkinter.messagebox",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "numpy", "PIL", "matplotlib", "pandas", "scipy",
        "pytest", "unittest", "pydoc_data", "sqlite3",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,          # ← onedir: کتابخانه‌ها بیرون از exe می‌مانند
    name="PSD-Label-Exporter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                  # بدون پنجرهٔ سیاه cmd
    disable_windowed_traceback=False,
    icon="assets/icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PSD-Label-Exporter",
)
