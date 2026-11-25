"""一次性的「刷新一组源」后台任务（工具栏刷新 / 右键刷新共用）。

为什么要后台任务，而不是同步请求
--------------------------------
Miniflux 的 `PUT /v1/feeds/{id}/refresh` 是**同步**的，单源最坏 120 秒
（客户端 timeout 已从 30s 放宽，见 `miniflux.refresh_feed` 的注释）。
一个分类几十个源串起来能把一次请求拖到十几分钟 —— 浏览器和 dev server 都扛不住。
所以这里只做两件事：**启动** 和 **查状态**；真正干活在 daemon 线程里**串行**跑，
前端拿 `feeds` 先把树上对应的源点亮成 loading，再轮询状态。
`states` 会逐源从 pending → ok/failed，前端据此**单个源先完成就先熄灯**，
不必等整批跑完。

★ 为什么串行而不是并发：`get_datasource()` 是全局单例，内部共用一个
  `requests.Session`，并发调用同一个 Session 不是线程安全的；刷新本身也不需要并发
  （源站压力、WAF 都更怕并发）。串行的代价只是总时长，而 UI 是异步的，用户无感。

★ 状态放 `core.jobstate`（进程内，单进程）。dev server 重启会丢任务状态——
  可以接受：任务本身会跑完，只是这一次 UI 拿不到结果；前端轮询有超时兜底。
"""
import logging
import threading
import time

from core.datasource import get_datasource
from core.jobstate import JobState

log = logging.getLogger(__name__)

_job = JobState(
    total=0,          # 本批要刷的源数
    done=0,           # 已处理数
    ok=0,
    failed=0,
    feeds=[],         # 本批的 feed id（前端用它点亮树）
    titles={},        # {feed_id: title}，只进 errors 文案
    errors=[],        # [{feed_id, title, error}]
    # ★ 逐源状态：{feed_id: "pending" | "ok" | "failed"}。前端按它**逐个熄灯**，
    #   而不是等整批结束 —— 慢源（Twitter/Playwright）不该拖住已经刷完的源。
    states={},
    started_at=0.0,
    finished_at=0.0,
)


def _snapshot() -> dict:
    """锁内浅拷贝；list/dict 也拷一层，避免调用方拿到可变引用。"""
    s = _job.snapshot()
    return {**s, "feeds": list(s["feeds"]),
            "errors": [dict(e) for e in s["errors"]],
            "states": dict(s["states"])}


def start(feed_ids, titles=None):
    """启动一批刷新。已在跑则**不抢占**，返回 (False, 当前状态)。

    返回的 `feeds` 永远是「正在跑的这一批」——前端据此点亮树，
    而不是按自己请求的 scope 点亮（否则会和实际刷新集合不一致）。
    """
    ids = []
    for x in feed_ids:
        try:
            n = int(x)
        except (TypeError, ValueError):
            continue
        if n not in ids:
            ids.append(n)
    if not ids:
        return False, _snapshot()

    if _job.running:
        return False, _snapshot()
    _job.begin(
        total=len(ids), done=0, ok=0, failed=0,
        feeds=ids, titles=dict(titles or {}), errors=[],
        states={fid: "pending" for fid in ids},
        started_at=time.time(), finished_at=0.0,
    )
    snap = _snapshot()

    threading.Thread(target=_run, args=(ids,), name="feed-refresh", daemon=True).start()
    return True, snap


def _run(ids):
    try:
        ds = get_datasource()
        for fid in ids:
            t0 = time.monotonic()
            err = ""
            try:
                ds.refresh_feed(fid)
            except Exception as exc:  # noqa: BLE001  单源失败不能影响其余源
                err = f"{exc.__class__.__name__}: {exc}"[:200]
                log.warning("刷新 feed %s 失败（%.1fs）：%s",
                            fid, time.monotonic() - t0, exc)
            else:
                log.info("刷新 feed %s 完成（%.1fs）", fid, time.monotonic() - t0)

            def _mark(s, fid=fid, err=err):
                s["done"] += 1
                s["states"][fid] = "failed" if err else "ok"
                if err:
                    s["failed"] += 1
                    s["errors"].append({
                        "feed_id": fid,
                        "title": s["titles"].get(fid, ""),
                        "error": err,
                    })
                else:
                    s["ok"] += 1

            _job.mutate(_mark)
    except Exception as exc:  # noqa: BLE001  兜底：任务无论如何要复位，否则 UI 永远转圈
        log.error("刷新批次异常终止：%s", exc)
    finally:
        _job.finish(finished_at=time.time())
        s = _job.snapshot()
        log.info("刷新批次完成：%s 个源（已处理 %s），失败 %s 个",
                 s["total"], s["done"], s["failed"])


def status() -> dict:
    return _snapshot()
