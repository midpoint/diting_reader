"""快速刷新（M6）：只刷显式开启的源。

为什么需要它
------------
Miniflux 的调度有**相位亏**：`实际间隔 = ceil((interval+d)/tick)×tick`，再加上
`POLLING_LIMIT_PER_HOST` 在批次选择阶段对同 host 计数 —— 同一站点多个源同域，
源一多就互相排队。想让某条源「10 分钟内必有新内容」只能绕过这套机制。

走什么路
--------
`PUT /v1/feeds/{id}/refresh`：**同步执行、绕过 worker 池与 host 限额**。
★ Miniflux 的 `FORCE_REFRESH_INTERVAL` **只在网页 UI 生效，API 没有这个限制**
（已实测），所以节奏完全由这里控制 —— 也正因为没有服务端兜底，限速必须自己做。

风险与对策（设计风险 #8）
------------------------
部分源有 WAF 且 `crawler=false`（开 crawler 会把挑战页盖回正文，只能删源重建）。
默认 15 分钟、一次只开一条、失败不重试（见 FeedOverride.last_fast_at 注释）。
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from core.datasource import get_datasource
from core.models import FeedOverride

log = logging.getLogger(__name__)


def due_feeds(now=None):
    """到点该刷的 FeedOverride 列表。"""
    now = now or timezone.now()
    due = []
    for o in FeedOverride.objects.filter(fast_refresh=True):
        interval = max(1, o.fast_interval_min or settings.FAST_REFRESH_DEFAULT_MIN)
        if o.last_fast_at is None or (now - o.last_fast_at) >= timedelta(minutes=interval):
            due.append(o)
    return due


def tick():
    """调度入口：每 FAST_REFRESH_TICK_SEC 跑一次，逐个刷到点的源。"""
    if not settings.FAST_REFRESH_ENABLED:
        return {"due": 0, "ok": 0, "failed": 0, "skipped": "disabled"}

    due = due_feeds()
    if not due:
        # 每 FAST_REFRESH_TICK_SEC 一次，绝大多数轮次都是"没有到点的源"
        log.debug("快速刷新：本轮无到点的源")
        return {"due": 0, "ok": 0, "failed": 0}

    ds = get_datasource()
    ok, failed = 0, 0
    for o in due:
        # ★ 先落 last_fast_at 再刷新：失败也要等满一个周期，避免打爆有 WAF 的源
        o.last_fast_at = timezone.now()
        try:
            ds.refresh_feed(o.feed_id)
            o.last_error = ""
            ok += 1
            log.info("快速刷新 feed %s 成功（间隔 %s 分钟）", o.feed_id, o.fast_interval_min)
        except Exception as exc:  # noqa: BLE001  单源失败不能影响其他源
            o.last_error = f"{exc.__class__.__name__}: {exc}"[:200]
            failed += 1
            log.warning("快速刷新 feed %s 失败：%s", o.feed_id, exc)
        o.save(update_fields=["last_fast_at", "last_error"])
    return {"due": len(due), "ok": ok, "failed": failed}


def refresh_now(feed_id: int) -> dict:
    """手工立刻刷一次（不受间隔限制，用于「立即刷新」按钮）。"""
    ds = get_datasource()
    o, _ = FeedOverride.objects.get_or_create(feed_id=int(feed_id))
    o.last_fast_at = timezone.now()
    try:
        ds.refresh_feed(feed_id)
        o.last_error = ""
        result = {"ok": True}
    except Exception as exc:  # noqa: BLE001
        o.last_error = f"{exc.__class__.__name__}: {exc}"[:200]
        result = {"ok": False, "error": str(exc)}
    o.save(update_fields=["last_fast_at", "last_error"])
    return result


def state() -> dict:
    """给 UI 用：{feed_id: {on, minutes, last_fast_at, error}}。"""
    return {o.feed_id: {"on": o.fast_refresh, "minutes": o.fast_interval_min,
                        "last_fast_at": o.last_fast_at, "error": o.last_error}
            for o in FeedOverride.objects.filter(fast_refresh=True)}
