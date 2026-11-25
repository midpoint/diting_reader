"""S3/S2/S1 产物缓存：输入指纹不变就复用，7 天只是上限。

★ 为什么是文件而不是 db 表：日报正文本来就落盘，摘要缓存也是派生内容；
  仓库硬约束是「本地表只存判定/状态/哈希」，所以缓存正文不进 db。
★ 为什么用输入指纹而不是日期：同一天重跑可能因为新文章、分级调整而输入变化；
  指纹变了就必须重跑，不能拿旧输出凑。TTL 只负责最终清理。
★ key 只放输入，不放生成参数：温度、max_tokens/预算变化不影响已有产物的有效性。
★ S0 全局合成不缓存：它依赖异常段、读者画像和全量分类结果，
  每次都重新合成更安全；S3/S2/S1 命中后 S0 只花一次调用。
"""
import hashlib
import json
import logging
import os
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

log = logging.getLogger(__name__)


def _root() -> Path:
    return Path(settings.SUMMARY_CACHE_DIR)


def fingerprint(payload: dict) -> str:
    """稳定指纹：sort_keys + ensure_ascii=False，避免 dict 顺序造成假 miss。"""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _path(layer: str, scope, payload: dict) -> Path:
    return _root() / f"{layer}-{scope}-{fingerprint(payload)[:32]}.json"


def get(layer: str, scope, payload: dict):
    """命中返回 text；未命中/过期/开关关掉返回 None。"""
    if not settings.SUMMARY_CACHE_ENABLED:
        return None
    p = _path(layer, scope, payload)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:  # noqa: BLE001  坏文件等于没命中
        log.warning("读总结缓存失败 %s：%s", p, exc)
        return None
    try:
        expires = timezone.datetime.fromisoformat(data["expires_at"])
    except (KeyError, TypeError, ValueError):
        return None
    try:
        expired = expires <= timezone.now()
    except TypeError:          # 老格式/坏时区：当成过期，别让缓存拖垮日报
        expired = True
    if expired:
        try:
            p.unlink()
        except OSError:
            pass
        return None
    text = data.get("text") or ""
    return text or None


def put(layer: str, scope, payload: dict, text: str, *, meta: dict = None) -> None:
    """原子写缓存；空文本不写。meta 只用于排查（模型/条目数等）。"""
    if not settings.SUMMARY_CACHE_ENABLED or not text:
        return
    now = timezone.now()
    expires = now + timedelta(days=max(1, int(settings.SUMMARY_CACHE_TTL_DAYS)))
    data = {
        "layer": layer,
        "scope": scope,
        "fingerprint": fingerprint(payload),
        "text": text,
        "created_at": now.isoformat(),
        "expires_at": expires.isoformat(),
        "meta": meta or {},
    }
    p = _path(layer, scope, payload)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except OSError as exc:  # noqa: BLE001  缓存写失败只该拖慢，不该拖垮日报
        log.warning("写总结缓存失败 %s：%s", p, exc)


def purge_expired() -> int:
    """删过期/损坏的缓存文件；在每次 run 开始时调用，成本可忽略。"""
    root = _root()
    if not root.is_dir():
        return 0
    now = timezone.now()
    n = 0
    for p in root.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            expires = timezone.datetime.fromisoformat(data["expires_at"])
            expired = expires <= now
        except (OSError, ValueError, KeyError, TypeError):
            expired = True
        if not expired:
            continue
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    if n:
        log.info("清理过期总结缓存 %s 个", n)
    return n
