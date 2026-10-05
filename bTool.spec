# -*- mode: python ; coding: utf-8 -*-
import os

import esptool
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = []
hiddenimports += collect_submodules('esptool')

# ⚠ esptool 的 stub_flasher 必须打进去 —— 烧录时它要被下载进芯片。
#   原先这里是**写死的绝对路径** (C:\Users\yutin\...\Python310\...\stub_flasher),
#   换台机器直接 "Unable to find ... stub_flasher" 打包失败。
#   改成从**当前解释器**现算, 跟 Python 版本和用户名都无关。
_stub_flasher = os.path.join(os.path.dirname(esptool.__file__),
                             'targets', 'stub_flasher')
if not os.path.isdir(_stub_flasher):
    raise SystemExit('找不到 esptool 的 stub_flasher: %s\n'
                     '先 pip install -U esptool 再打包。' % _stub_flasher)


a = Analysis(
    ['btool.py'],
    pathex=[],
    binaries=[],
    datas=[(_stub_flasher, 'esptool/targets/stub_flasher')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['IPython', 'matplotlib', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6'],
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
    name='bTool',
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
    icon=['bTool.ico'],
)
