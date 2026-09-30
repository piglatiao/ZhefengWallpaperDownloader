# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.building.build_main import Analysis, PYZ, EXE


a = Analysis(
    ['get_wallpapers.py'],
    pathex=['.'],
    binaries=[],
    datas=[('browser', 'browser')],
    hiddenimports=[
        'Crypto.Cipher.AES',
        'Crypto.Util.Padding',
        'websocket',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='get_wallpapers',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
