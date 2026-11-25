#!/usr/bin/env python3
"""构建 Windows 便携包：内嵌官方 Python + 依赖 + reader 源码，产出 zip 与 sha256。

设计目标：
  - **Windows runner 与 Linux/macOS 都能跑**（pip 用 --platform win_amd64 交叉取
    wheel，所以不需要真的在 Windows 上执行）；
  - 依赖版本固定，构建可复现；只装纯 wheel（`--only-binary=:all:`），避免 CI 上
    因为没有 C 编译器而随机失败；
  - 包里**绝不包含** `.env`、本地数据库、日志、缓存。

用法：
    python scripts/build_windows_portable.py --version 0.1.0
产物：
    dist/diting-reader-windows-0.1.0.zip
    dist/diting-reader-windows-0.1.0.zip.sha256
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# GitHub Actions 的 Windows runner 控制台编码不一定是 UTF-8；本脚本会打印中文，
# 编码失败会让整个构建在无关紧要的 print 上挂掉。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
PY_VERSION = "3.12.10"
PY_URL = (f"https://www.python.org/ftp/python/{PY_VERSION}/"
          f"python-{PY_VERSION}-embed-amd64.zip")
# ★ 与 reader/requirements.txt 对齐并钉死版本；winotify 显式列出（Linux 主机上
#   环境标记不会选中它，但它是 Windows 包必需的）。
DEPS = [
    "Django==6.1.3",
    "requests==2.34.2",
    "APScheduler==3.11.3",
    "pypinyin==0.55.0",
    "winotify==1.1.0",
    "tzdata==2026.6",
]
APP_NAME = "diting-windows"

SKIP_DIRS = {"data", "__pycache__", ".git", ".venv", "venv", "staticfiles"}
SKIP_SUFFIX = {".pyc", ".pyo", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".log"}


def log(msg: str):
    print(f"[build] {msg}", flush=True)


def retry(fn, what: str, times: int = 3):
    last = None
    for i in range(1, times + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"{what} 失败（第 {i}/{times} 次）：{e}")
            if i < times:
                time.sleep(2 * i)
    raise RuntimeError(f"{what} 失败：{last}")


def download(url: str, dst: Path):
    if dst.exists() and dst.stat().st_size > 0:
        log(f"复用已下载的 {dst.name}")
        return

    def _get():
        log(f"下载 {url}")
        with urllib.request.urlopen(url, timeout=120) as r, open(dst, "wb") as f:
            shutil.copyfileobj(r, f)
        if dst.stat().st_size < 1_000_000:
            raise RuntimeError("文件过小，可能下载不完整")

    retry(_get, "下载 Python embeddable")


def copy_tree(src: Path, dst: Path):
    """复制源码，跳过运行时产物。"""
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.is_dir():
            continue
        if path.suffix.lower() in SKIP_SUFFIX:
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def patch_pth(python_dir: Path):
    """让嵌入式 Python 找到 site-packages 并执行 site（否则 .pth 不生效）。"""
    pths = list(python_dir.glob("python*._pth"))
    if not pths:
        raise RuntimeError("找不到 python*._pth，Python 包结构异常")
    pth = pths[0]
    lines = ["python312.zip", ".", r"Lib\site-packages", "import site"]
    pth.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"已改写 {pth.name}")


def install_deps(site_packages: Path, cache_dir: Path):
    site_packages.mkdir(parents=True, exist_ok=True)
    # ★ 在 Windows runner 上按本机解释器直接取 wheel；在 Linux/macOS 上才需要
    #   显式 --platform/--abi 交叉下载。两条路都只接受预编译 wheel。
    extra = []
    if sys.platform != "win32":
        extra = [
            "--platform", "win_amd64", "--python-version", "3.12",
            "--implementation", "cp", "--abi", "cp312",
        ]
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--disable-pip-version-check", "--no-input",
        "--target", str(site_packages),
        *extra,
        "--only-binary=:all:",
        "--upgrade",
        "--cache-dir", str(cache_dir),
        *DEPS,
    ]
    log(f"安装 Windows 依赖（host={sys.platform}）：" + " ".join(DEPS))
    retry(lambda: subprocess.run(cmd, check=True), "pip install", times=2)


def write_launcher(dst: Path):
    src = ROOT / "packaging" / "windows" / "启动谛听.cmd"
    text = src.read_text(encoding="utf-8").replace("\r\n", "\n")
    dst.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))   # CRLF、无 BOM


def make_zip(src_dir: Path, out: Path):
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=9) as zf:
        for path in sorted(src_dir.rglob("*")):
            if path.is_file():
                arc = Path(APP_NAME) / path.relative_to(src_dir)
                zf.write(path, arc.as_posix())
    log(f"已生成 {out}（{out.stat().st_size / 1024 / 1024:.1f} MB）")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default=os.environ.get("GITHUB_REF_NAME", "dev"))
    ap.add_argument("--out-dir", default=str(ROOT / "dist"))
    ap.add_argument("--keep-build", action="store_true")
    args = ap.parse_args()
    version = args.version.lstrip("v") or "dev"

    # ★ 必需文件先查：曾出现 .env.example 被 .gitignore 掉、CI checkout 里没有，
    #   构建"成功"但 ZIP 里缺首次运行模板。宁可在这一步失败。
    for rel in (".env.example", "reader/manage.py",
                "packaging/windows/启动谛听.cmd", "README-Windows.md"):
        if not (ROOT / rel).exists():
            raise SystemExit(f"缺少必需文件：{rel}（是否被 .gitignore 排除？）")

    build_root = ROOT / "build" / APP_NAME
    if build_root.exists():
        shutil.rmtree(build_root)
    build_root.mkdir(parents=True)

    cache = ROOT / "build" / "_cache"
    cache.mkdir(parents=True, exist_ok=True)
    py_zip = cache / f"python-{PY_VERSION}-embed-amd64.zip"
    download(PY_URL, py_zip)

    python_dir = build_root / "python"
    log("解压 Python embeddable")
    with zipfile.ZipFile(py_zip) as zf:
        zf.extractall(python_dir)
    patch_pth(python_dir)
    install_deps(python_dir / "Lib" / "site-packages", cache)

    log("复制 reader 源码")
    copy_tree(ROOT / "reader", build_root / "reader")
    # 开发脚本不是运行必需，且部分会被 Windows Defender 误报；不放进便携包
    shutil.rmtree(build_root / "reader" / "scripts", ignore_errors=True)

    for name in ("Meta", "docs"):
        if (ROOT / name).exists():
            copy_tree(ROOT / name, build_root / name)
    for name in (".env.example", "LICENSE", "SECURITY.md", "README-Windows.md"):
        src = ROOT / name
        if src.exists():
            shutil.copy2(src, build_root / name)
    write_launcher(build_root / "启动谛听.cmd")

    out_dir = Path(args.out_dir)
    zip_path = out_dir / f"diting-reader-windows-{version}.zip"
    make_zip(build_root, zip_path)
    digest = sha256(zip_path)
    (zip_path.with_suffix(".zip.sha256")).write_text(
        f"{digest}  {zip_path.name}\n", encoding="utf-8")
    log(f"SHA256 {digest}")

    if not args.keep_build:
        shutil.rmtree(build_root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
