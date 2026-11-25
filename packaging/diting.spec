# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包描述：单文件 exe（源码 + 依赖 + 模板/静态文件）。

★ 为什么把 `reader/` 整棵树作为 data 带上，而不是让 PyInstaller 分析 import：
  Django 的 app、migrations、模板、静态文件都是**运行时按路径/字符串**加载的，
  静态分析看不到。把源码原样放进 exe，启动器把它加进 sys.path，行为与源码
  运行完全一致（同一个 settings、同一套模板）。
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent

SKIP_DIRS = {"data", "__pycache__", ".git", ".venv", "venv", "staticfiles", "scripts"}
SKIP_SUFFIX = {".pyc", ".pyo", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".log"}

datas = []


def add_tree(src: Path, dest: str):
    if not src.exists():
        return
    for p in src.rglob("*"):
        if p.is_dir():
            continue
        rel = p.relative_to(src)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if p.suffix.lower() in SKIP_SUFFIX:
            continue
        # datas 的第二个元素是目标目录
        datas.append((str(p), str(Path(dest) / rel.parent)))


add_tree(ROOT / "reader", "reader")
add_tree(ROOT / "Meta", "Meta")
for name in (".env.example", "LICENSE", "SECURITY.md", "README-Windows.md"):
    p = ROOT / name
    if p.exists():
        datas.append((str(p), "."))

hiddenimports = []
for pkg in ("django", "requests", "urllib3", "apscheduler", "pypinyin"):
    hiddenimports += collect_submodules(pkg)
if sys.platform == "win32":
    # winotify 只在 Windows 上装；Linux 本地冒烟构建时跳过
    hiddenimports += collect_submodules("winotify")
hiddenimports += ["sqlite3", "tzdata"]

a = Analysis(
    [str(ROOT / "packaging" / "diting_exe.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "PIL", "numpy", "matplotlib", "scipy", "pandas"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="谛听阅读器",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
