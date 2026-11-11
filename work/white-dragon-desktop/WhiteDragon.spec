# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['../../outputs/white-dragon-desktop/desktop.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['cv2', 'numpy', 'PySide6.QtQml', 'PySide6.QtQuick'],
    noarchive=False,
    optimize=0,
)
# System ICU is required by this Qt build. Do not bundle a different ICU from PATH.
a.binaries = [b for b in a.binaries if b[0].replace('\\', '/').split('/')[-1].lower() not in {'icuuc.dll', 'icudt78.dll'}]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='WhiteDragon',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='WhiteDragon',
)
