# -*- mode: python ; coding: utf-8 -*-
# =============================================================================
# D:\程序\C盘清理工具\cleaner.spec
# -----------------------------------------------------------------------------
# 用途: PyInstaller 打包配置,把 cleaner.py 打包为单文件 GUI 可执行程序
# 生成命令: pyinstaller --clean --noconfirm cleaner.spec
# 由 build.bat 调用,通常不需要手动运行
#
# 设计要点:
#   - name='C_Cleaner'    : 保持 exe 文件名不变,避免用户路径/快捷方式失效
#   - onefile=True        : 单文件发布,便于用户分发
#   - noconsole=True      : GUI 程序,不显示控制台窗口
#   - uac_admin=True      : 申请管理员权限(清理系统目录必需)
#   - icon='app.ico'      : 应用图标
#   - datas=[]            : 无外部资源(所有路径都来自 Windows 系统)
#   - hiddenimports=[...] : 函数内动态 import,AST 分析不到,必须显式列出
#   - upx=True            : 启用 UPX 压缩(若不可用,pyinstaller 会自动跳过)
#
# 更新历史:
#   2026-09-06  适配 core-refactor 新增模块: base64, logging, logging.handlers,
#                dataclasses, enum
#   2026-09-07  v3.3.2 修复: 加 collect_data_files('tkinter') 自动收集 init.tcl/tk.tcl,
#                excludes=['tests','unittest','pytest'] 防止测试代码污染 exe 名空间
# =============================================================================

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# 收集 tkinter 子模块(子模块不会自动包含)
tkinter_submodules = collect_submodules('tkinter')

# 收集所有 stdlib 动态 import,确保 pyinstaller 不漏
hiddenimports = [
    # ---- 函数内动态 import (cleaner.py 实际出现位置) ----
    'fnmatch',                            # cleaner.py:1916
    'traceback',                          # cleaner.py:3502/3510/4366
    'collections',                        # 顶层依赖
    'collections.defaultdict',            # cleaner.py:4322
    'json',                               # cleaner.py:4521/4533 (顶层也有,双保险)
    # ---- tkinter 相关 (cleaner.py:2766-2767) ----
    'tkinter',
    'tkinter.ttk',
    'tkinter.messagebox',
    'tkinter.scrolledtext',
    'tkinter.filedialog',
    *tkinter_submodules,
    # ---- core-refactor 新增顶层模块 (cleaner.py:28-30, 92-93) ----
    'base64',                             # EncodedCommand 注入防御
    'logging',                            # 统一日志
    'logging.handlers',                   # RotatingFileHandler
    'dataclasses',                        # CleanTarget
    'enum',                               # 枚举基类
    # ---- stdlib 顶层 (虽然会自动发现,显式列出便于审计) ----
    'os', 'sys', 'shutil', 'ctypes',
    'threading', 'subprocess', 'time',
    're', 'gc', 'hashlib', 'queue',
    'pathlib',
    'datetime',
    'concurrent.futures',
]

a = Analysis(
    ['cleaner.py'],
    pathex=[],
    binaries=[],
    datas=collect_data_files('tkinter'),  # 自动收集 init.tcl / tk.tcl / tcl8 / tk8(v3.3.2 修复 Tcl_InitError)
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # v3.3.2 修复:防止测试代码 + mock 框架污染 exe 主进程命名空间
        # (之前打包误把 tests/ 一并纳入,触发 Tcl_InitError 的误导性 traceback)
        'tests',
        'unittest',
        'pytest',
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
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='C_Cleaner',           # 保持 exe 文件名不变
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,                   # UPX 压缩 (系统无 UPX 时 pyinstaller 自动跳过)
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,              # noconsole
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # [UAC] Windows 专属: 申请管理员权限
    uac_admin=True,
    uac_uiaccess=False,
    # [ICON] 应用图标
    icon='app.ico',
)
