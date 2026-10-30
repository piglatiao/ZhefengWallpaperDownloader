# -*- mode: python ; coding: utf-8 -*-
"""代理池批量下载器的 PyInstaller 打包配置。"""

from PyInstaller.building.build_main import Analysis, PYZ, EXE


a = Analysis(
    ['batch_download.py'],
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
    name='batch_download',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
