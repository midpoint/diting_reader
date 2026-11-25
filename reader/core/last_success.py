"""从 feed 的 checked_at / next_check_at / parsing_error_count 推导「最后一次成功拉取」。

Miniflux 没有 last_success 字段（已实测：checked_at = 最后一次「尝试」，失败也写）。

    err == 0        -> checked_at 即最后成功（精确）
    err == N > 0    -> checked_at - N * (next_check_at - checked_at)（估算）
"""
from datetime import timedelta


def last_success(feed) -> tuple:
    """feed: 含 parsing_error_count / checked_at / next_check_at 的 dict。

    返回 (datetime|None, exact: bool)
    """
    checked = _dt(feed.get("checked_at"))
    if checked is None:
        return None, False

    err = int(feed.get("parsing_error_count") or 0)
    if err <= 0:
        return checked, True

    nxt = _dt(feed.get("next_check_at"))
    if nxt is None:
        return None, False

    interval = nxt - checked
    if interval <= timedelta(0):
        return None, False
    return checked - err * interval, False


def _dt(value):
    if not value:
        return None
    if hasattr(value, "tzinfo"):
        return value
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
