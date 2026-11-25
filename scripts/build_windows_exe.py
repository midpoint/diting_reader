#!/usr/bin/env python3
"""构建单文件 exe：PyInstaller + packaging/diting.spec。

用法：
    python scripts/build_windows_exe.py --version 0.2.0
产物：
    dist/diting-reader-windows-0.2.0.exe
    dist/diting-reader-windows-0.2.0.exe.sha256

★ 必须在目标平台上构建（PyInstaller 不支持交叉编译）：
  Windows 版在 GitHub Actions 的 windows-latest 上产出；
  Linux/macOS 上跑同一条命令只会得到对应平台的二进制，用于冒烟验证启动逻辑。
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "diting.spec"
EXE_NAME = "谛听阅读器"


def log(msg: str):
    print(f"[exe] {msg}", flush=True)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default=os.environ.get("GITHUB_REF_NAME", "dev"))
    ap.add_argument("--out-dir", default=str(ROOT / "dist"))
    ap.add_argument("--skip-clean", action="store_true")
    args = ap.parse_args()
    version = args.version.lstrip("v") or "dev"

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        log("缺少 PyInstaller：先执行 python -m pip install 'pyinstaller>=6.10,<7'")
        return 2

    out_dir = Path(args.out_dir)
    work_dir = ROOT / "build" / "pyinstaller"
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--distpath", str(ROOT / "build" / "exe-dist"),
        "--workpath", str(work_dir),
    ]
    if not args.skip_clean:
        cmd.append("--clean")
    cmd.append(str(SPEC))
    log("PyInstaller 开始构建（单文件，首次约 1–3 分钟）")
    subprocess.run(cmd, check=True, cwd=str(ROOT))

    produced = ROOT / "build" / "exe-dist" / (
        EXE_NAME + (".exe" if sys.platform == "win32" else ""))
    if not produced.exists():
        # 某些平台会保留 .exe 后缀，两种都试
        alt = produced.with_name(EXE_NAME + ".exe")
        if alt.exists():
            produced = alt
    if not produced.exists():
        log(f"构建失败：找不到 {produced}")
        return 1

    target = out_dir / f"diting-reader-windows-{version}.exe"
    shutil.copy2(produced, target)
    digest = sha256(target)
    target.with_suffix(".exe.sha256").write_text(
        f"{digest}  {target.name}\n", encoding="utf-8")
    log(f"已生成 {target}（{target.stat().st_size / 1024 / 1024:.1f} MB）")
    log(f"SHA256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
