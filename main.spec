# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files

# sv-ttk ships its theme as .tcl + .png data files that must be bundled, or the
# packaged app launches with the default (unthemed) look.
sv_ttk_datas = collect_data_files('sv_ttk')

# services.json / appsettings.json are deliberately NOT bundled: the app creates them in
# %APPDATA%\OnCodes on first run (see config_manager.config_dir), so the .exe can be copied
# anywhere without carrying config alongside it.
a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('icon.ico', '.')] + sv_ttk_datas,
    hiddenimports=['pystray._win32'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='OnCodesDevServiceManager',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
