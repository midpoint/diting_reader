"""读/写仓库根目录的 `.env`（应用内设置页的唯一落盘出口）。

为什么单独一个模块：
  - FreshRSS / Miniflux / LLM 的**密钥只准存在 `.env`**（见 SECURITY.md 与
    datasource 文档）。设置页要能改它们，就必须有一个"保留注释、原子写、
    0600 权限"的写入器，而不是让视图直接 open(..., "w")。
  - `.env` 是手写文件：用户可能在旁边写注释、分组、留空行。整体重写会把这些
    全冲掉，所以这里按行合并：只替换目标 key 的第一行，删掉同名重复行，
    其余原样保留。

安全约定：
  - 不记日志内容（只记 key 名），不把值带进异常文案；
  - 新建文件与临时文件都是 0600（Windows 上 chmod 基本无效，但不会报错）。
"""
import logging
import os
import re
import tempfile
from pathlib import Path

from django.conf import settings

log = logging.getLogger(__name__)

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def env_path() -> Path:
    """与 config/settings.py 的 `_load_dotenv(BASE_DIR.parent / ".env")` 同一路径。"""
    return Path(settings.PROJECT_ROOT) / ".env"


def parse(text: str) -> dict:
    """与 settings._load_dotenv 相同的宽松解析（只用于取值，不用于保序回写）。"""
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def read_text() -> str:
    try:
        return env_path().read_text(encoding="utf-8")
    except OSError:
        return ""


def read_values() -> dict:
    return parse(read_text())


def _clean_value(key: str, value: str) -> str:
    """值里不能有换行（否则一行变两行）；引号会破坏解析，统一去掉首尾引号。"""
    v = str(value if value is not None else "")
    if "\n" in v or "\r" in v:
        raise ValueError(f"{key} 的值不能包含换行")
    return v.strip().strip('"').strip("'")


def merge(text: str, updates: dict, remove=()) -> str:
    """按行合并：保注释、保顺序、保未知键。返回新文本（不落盘）。"""
    remove = {str(k).strip() for k in remove}
    pending = {}
    for k, v in updates.items():
        k = str(k).strip()
        if not _KEY_RE.match(k):
            raise ValueError(f"非法配置名：{k}")
        pending[k] = _clean_value(k, v)

    lines = text.splitlines()
    out = []
    seen = set()
    for raw in lines:
        s = raw.strip()
        if not s or s.startswith("#") or "=" not in s:
            out.append(raw)
            continue
        key = s.partition("=")[0].strip()
        if key in remove:
            continue
        if key in pending:
            if key in seen:
                continue           # 同名行只保留第一条（用新值）
            out.append(f"{key}={pending[key]}")
            seen.add(key)
            continue
        out.append(raw)

    for key, val in pending.items():
        if key not in seen:
            out.append(f"{key}={val}")

    return "\n".join(out).rstrip("\n") + "\n"


def write_text(text: str) -> None:
    """原子写 + 0600。失败抛 OSError，由视图翻译成人话。"""
    path = env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_updates(updates: dict, remove=()) -> dict:
    """合并写入并返回写入后的完整键值表。"""
    new_text = merge(read_text(), updates, remove)
    write_text(new_text)
    log.info("应用内设置已写入 .env：%s%s",
             ",".join(sorted(updates)) or "-",
             f"（删除 {','.join(sorted(remove))}）" if remove else "")
    return parse(new_text)


# ------------------------------------------------------------------ 热应用
#: `.env` 名 → Django setting 名（只有名字不一致的才需要列在这里）。
_NAME_MAP = {
    "MINIFLUX_URL": "MINIFLUX_BASE_URL",
    "MINIFLUX_ADMIN_USERNAME": "MINIFLUX_USERNAME",
    "MINIFLUX_ADMIN_PASSWORD": "MINIFLUX_PASSWORD",
    "READER_ENTRY_WINDOW": "ENTRY_WINDOW",
    "READER_MULTI_FEED_PER_SOURCE": "MULTI_FEED_PER_SOURCE",
    "READER_TREE_CACHE_TTL": "TREE_CACHE_TTL",
    "READER_UNRATED_LEVEL": "UNRATED_LEVEL",
}
#: 改了也不能/不该在当前进程热生效的项（Django 启动期绑定）。
_RESTART_ONLY = {"DJANGO_DEBUG", "DJANGO_SECRET_KEY", "DJANGO_ALLOWED_HOSTS"}


def setting_name(env_key: str) -> str:
    """`.env` 名 → 当前 settings 里的属性名（不存在的键返回原名）。"""
    return _NAME_MAP.get(env_key, env_key)


def _cast_like(old, raw: str):
    """按 setting 当前类型转型，让热应用后的值与重启后一致。"""
    if isinstance(old, bool):
        return str(raw).strip().lower() in ("1", "true", "on", "yes")
    if isinstance(old, int):
        return int(float(raw))
    if isinstance(old, float):
        return float(raw)
    return str(raw)


def apply_to_settings(values: dict) -> set:
    """把键值表应用到当前进程的 `settings`，返回实际更新的 setting 名。

    ★ 只覆盖 settings 里**已经存在**的属性（拼错的键不会凭空造属性）。
    ★ 不处理 DJANGO_*（DEBUG/ALLOWED_HOSTS/SECRET_KEY 是启动期语义）。
    ★ 失败静默跳过：一个坏值不能拖垮整页保存；重启后以文件为准。
    """
    changed = set()
    for env_key, raw in values.items():
        if env_key in _RESTART_ONLY:
            continue
        attr = _NAME_MAP.get(env_key, env_key)
        if not hasattr(settings, attr):
            continue
        # 与 config/settings.py 的启动期规范化保持一致：热应用后的值与重启后一致。
        if env_key == "DATASOURCE_ENGINE":
            raw = str(raw).strip().lower()
        elif env_key.endswith("_URL"):
            raw = str(raw).strip().rstrip("/")
        try:
            val = _cast_like(getattr(settings, attr), raw)
        except (TypeError, ValueError):
            log.warning("设置热应用跳过：%s 的值无法按原类型解析", env_key)
            continue
        setattr(settings, attr, val)
        changed.add(attr)
    # settings.env() 的"进程环境优先"仍然成立；更新文件镜像，避免同进程里
    # 再次读 `_FILE_ENV`（例如某些延迟初始化路径）拿到旧值。
    if hasattr(settings, "_FILE_ENV") and isinstance(settings._FILE_ENV, dict):
        settings._FILE_ENV.update(values)
    return changed


def reset_datasource():
    """数据源单例重置：换引擎 / 换地址后下一次 get_datasource() 用新配置。"""
    from core.datasource import reset

    reset()
