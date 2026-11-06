# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把加菲猫的工作室打成单个 exe。

入口 start.py（它在进程内调 app.main()，端口占用检测、首次生成 config
这些优点都保留；冻结时 start.py 会跳过“换解释器重起”）。
单文件、带控制台：启动器要打印提示、首次运行还要问问题，关掉控制台就看不见了。
数据文件：web/ 前端、config.example.json（首次运行生成 config 用）。
五个办公库是函数内动态 import 的，静态分析看不见，必须写进 hiddenimports。
用法：python tools/build_release.py（工作流里也是这么调的）
"""
block_cipher = None

a = Analysis(
    ['start.py'],
    pathex=[],
    binaries=[],
    datas=[('web', 'web'), ('config.example.json', '.')],
    hiddenimports=['docx', 'openpyxl', 'pptx', 'pypdf', 'PIL'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='LocalAgent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
