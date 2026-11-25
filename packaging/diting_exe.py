#!/usr/bin/env python3
"""谛听阅读器 · Windows 单文件 exe 的启动器（PyInstaller 入口）。

设计要点：
  - exe 里带的是**源码 + 依赖**，不是编译后的 Django 应用；启动时把包内的
    `reader/` 加到 sys.path，与源码运行走同一条代码路径。
  - 数据、`.env`、`Meta/` 必须落在 **exe 旁边**：设置 `DITING_HOME` 让
    config/settings.py 把 PROJECT_ROOT / DATA_DIR 锚定过去。PyInstaller 的
    临时解压目录进程退出就没了，绝不能往那里写。
  - 首次运行：生成 `.env`（从模板）、把 `Meta/*.example.md` 复制成正式文件，
    然后打开「设置 → 数据源」。以后直接打开阅读页。
  - 端口占用时不再启动第二个实例，直接打开已有页面。
"""
import os
import shutil
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_NAME = "谛听阅读器"
DEFAULT_PORT = 8099
HOST = "127.0.0.1"


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _bundle_root() -> Path:
    """PyInstaller 解压目录（源码运行时 = 仓库根）。"""
    if _frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def _home_dir() -> Path:
    """可写目录：优先 exe 所在目录；源码测试时 = 仓库根。

    ★ 放在 Program Files / 只读盘时 exe 目录不可写，回退到
      %LOCALAPPDATA%\\DitingReader —— 不能因为放错位置就起不来。
    """
    if _frozen():
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent.parent
    try:
        base.mkdir(parents=True, exist_ok=True)
        probe = base / ".diting-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return base
    except OSError:
        fallback = Path(os.environ.get("LOCALAPPDATA")
                        or Path.home()) / "DitingReader"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def _sample_env() -> str:
    return (
        "# 谛听阅读器配置（可在应用「设置 → 数据源 / AI 模型」里修改）\n"
        "DATASOURCE_ENGINE=greader\n"
        "GREADER_BASE_URL=\n"
        "GREADER_USERNAME=\n"
        "GREADER_PASSWORD=\n"
        "MINIFLUX_URL=http://localhost:8080\n"
        "MINIFLUX_ADMIN_USERNAME=\n"
        "MINIFLUX_ADMIN_PASSWORD=\n"
        "LLM_PROVIDER=deepseek\n"
        "DEEPSEEK_API_KEY=\n"
        "DEEPSEEK_BASE_URL=https://api.deepseek.com/v1\n"
        "DEEPSEEK_MODEL=deepseek-chat\n"
        "SUMMARY_FRESH_DEADLINE_MIN=0\n"
    )


def prepare_home(home: Path, bundle: Path) -> bool:
    """建目录、种 .env / Meta；返回是否首次运行。"""
    home.mkdir(parents=True, exist_ok=True)
    first = not (home / ".env").exists()
    if first:
        tpl = bundle / ".env.example"
        try:
            text = tpl.read_text(encoding="utf-8") if tpl.exists() else _sample_env()
        except OSError:
            text = _sample_env()
        (home / ".env").write_text(text, encoding="utf-8")
        try:
            os.chmod(home / ".env", 0o600)
        except OSError:
            pass

    meta = home / "Meta"
    meta.mkdir(parents=True, exist_ok=True)
    bmeta = bundle / "Meta"
    if bmeta.exists():
        for ex in bmeta.glob("*.example.md"):
            real = meta / ex.name.replace(".example.md", ".md")
            if not real.exists():
                try:
                    shutil.copyfile(ex, real)
                except OSError:
                    pass
    return first


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((HOST, port)) == 0


def open_browser_later(url: str, delay: float = 1.5):
    def _run():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001  打不开浏览器不该影响服务
            pass
    threading.Thread(target=_run, daemon=True).start()


def pause_if_interactive(msg: str = "\n按回车退出……"):
    try:
        if sys.stdin and sys.stdin.isatty():
            input(msg)
    except (EOFError, OSError):
        pass


def main() -> int:
    argv = list(sys.argv[1:])
    port = int(os.environ.get("DITING_PORT") or DEFAULT_PORT)
    if "--port" in argv:
        i = argv.index("--port")
        port = int(argv[i + 1])

    bundle = _bundle_root()
    home = _home_dir()
    first = prepare_home(home, bundle)

    url = f"http://{HOST}:{port}"

    # 单实例：已经有一个在跑，就只把页面打开
    if port_open(port):
        print(f"{APP_NAME} 已经在运行：{url}")
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
        return 0

    os.environ["DITING_HOME"] = str(home)
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
    os.environ.setdefault("PYTHONUTF8", "1")
    reader = bundle / "reader"
    if not reader.exists():
        print(f"[错误] 安装包不完整：找不到 {reader}")
        pause_if_interactive()
        return 1
    sys.path.insert(0, str(reader))

    print(f"{APP_NAME} 启动中：{url}")
    print(f"数据目录：{home}")
    print("停止：按 Ctrl+C，或关闭本窗口。\n")

    try:
        import django
        from django.core.management import call_command, execute_from_command_line
        django.setup()
        # 每次启动都跑一遍迁移：升级 exe 后旧库自动补表，幂等且很快
        call_command("migrate", interactive=False, verbosity=0)
    except Exception as exc:  # noqa: BLE001
        print(f"\n[错误] 数据库初始化失败：{exc}")
        import traceback
        traceback.print_exc()
        pause_if_interactive()
        return 1

    target = f"{url}/prefs?g=datasource" if first else url
    open_browser_later(target)
    try:
        execute_from_command_line(
            ["manage.py", "runserver", f"{HOST}:{port}", "--noreload"])
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
