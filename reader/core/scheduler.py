"""APScheduler 注册（单进程：Web + 常驻任务同进程）。

★ 两个必须写对的坑：
  1. Django autoreload 下 AppConfig.ready() 会执行**两次**（父进程 + 子进程），
     不做守卫就会起两个 scheduler、任务跑两遍。用 RUN_MAIN / --noreload 判定。
  2. 每个 job 内部要 try/except：APScheduler 吞掉异常后 job 会静默不再触发。
"""
import logging
import os
import sys
import time
import traceback

from django.conf import settings

from core.logkit import brief, fields

log = logging.getLogger(__name__)

_scheduler = None


def _should_start() -> bool:
    """只在 Web 服务进程里起调度。

    ★ autoreload 下 ready() 跑两次：父进程（无 RUN_MAIN）必须跳过，否则任务跑两遍。
    ★ makemigrations / shell / triage_once 这类命令一律不起调度 —— 否则一条
      一次性命令就会在后台常驻一个 scheduler（实测踩到）。
    """
    if os.environ.get("DITING_WORKER") == "1":
        return True
    if "runserver" not in sys.argv:
        return False
    if os.environ.get("RUN_MAIN") == "true":
        return True
    return "--noreload" in sys.argv


def _safe(fn, name):
    """包一层：计时 + 结果摘要 + 异常兜底。

    ★ APScheduler 自己会把 job 抛出的异常吞掉（job 之后照跑但你看不到），
      所以这里的 try/except 是**唯一**的异常出口。
    ★ 计时与结果只记 DEBUG：triage 每 5 分钟一轮，无事发生时记 INFO 会变成
      288 行/天的噪音（"什么都没做"占绝大多数）。**真有动作的 INFO 由各 job
      自己记**（它才知道"做了事"是什么意思）。
    """
    def _wrapped(*args, **kwargs):
        t0 = time.monotonic()
        log.debug("[%s] 开始", name)
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            log.error("[%s] 任务异常：%s\n%s", name, exc, traceback.format_exc())
            return None
        ms = int((time.monotonic() - t0) * 1000)
        log.debug("[%s] 完成 in %sms %s", name, ms, brief(result),
                  extra=fields(src=name, ms=ms, ok=True))
        return result
    _wrapped.__name__ = getattr(fn, "__name__", name)
    return _wrapped


def start():
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    if not settings.ENABLE_SCHEDULER:
        log.info("调度未启用（ENABLE_SCHEDULER=0）")
        return None
    if not _should_start():
        return None

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    from core import triage

    _scheduler = BackgroundScheduler(timezone=settings.TIME_ZONE,
                                     job_defaults={"coalesce": True,
                                                   "max_instances": 1,
                                                   "misfire_grace_time": 3600})
    # ★ triage/translate/summary 三个 job **无条件注册**，总开关在各 job 入口判
    #   sk.effective("*_enabled")——这样设置页的启用开关即时生效，不用重启。
    #   restart 级参数（interval、cron 时刻）仍按 django settings 注册，设置页
    #   如实标注「重启后生效」。fastrefresh 不在设置页 v1 范围，
    #   维持 settings 条件注册。
    _scheduler.add_job(
        _safe(triage.run_once, "triage"),
        trigger="interval", minutes=settings.TRIAGE_INTERVAL_MIN,
        id="triage", replace_existing=True,
    )
    log.info("已注册 triage：每 %s 分钟（provider=%s model=%s；总开关在 job 入口判）",
             settings.TRIAGE_INTERVAL_MIN, settings.LLM_PROVIDER,
             llm_model_name())
    if settings.FAST_REFRESH_ENABLED:
        from core import fastrefresh

        # ★ 用一个固定的短 tick + 每个 feed 自己的 last_fast_at 做节流，
        #   而不是给每个 feed 注册独立 job —— 开关配置变动后无需重建 job。
        _scheduler.add_job(
            _safe(fastrefresh.tick, "fastrefresh"),
            trigger="interval", seconds=settings.FAST_REFRESH_TICK_SEC,
            id="fastrefresh", replace_existing=True,
        )
        log.info("已注册 fastrefresh：每 %s 秒检查一次（默认间隔 %s 分钟，当前开启 %s 条）",
                 settings.FAST_REFRESH_TICK_SEC, settings.FAST_REFRESH_DEFAULT_MIN,
                 _fast_count())
    from datetime import datetime, timedelta

    from core import translate

    # ★ 与 fastrefresh 同款：固定 tick + 每个 feed 自己的 last_scan_at 节流。
    # ★ 首跳错开半个周期：triage 与 translate 同为 5 分钟 interval，若在同一
    #   毫秒附近注册就会相位锁死 —— translate 每轮都撞在 triage 上，让路
    #   变成饿死（2026-10-25 实证：连续 12 小时没扫过）。错开 150 秒让两者
    #   永不同时触发；tick 内还有「等 triage 再试一次」兜底（唤醒合流场景）。
    offset = settings.TRANSLATE_TICK_SEC // 2
    _scheduler.add_job(
        _safe(translate.tick, "translate"),
        trigger="interval", seconds=settings.TRANSLATE_TICK_SEC,
        next_run_time=datetime.now(_scheduler.timezone)
        + timedelta(seconds=offset),
        id="translate", replace_existing=True,
    )
    log.info("已注册标题翻译：每 %s 秒扫描（首跳错开 %s 秒避让 triage；已开启 %s 个源，池预算 ¥%s）",
             settings.TRANSLATE_TICK_SEC, offset, _translate_count(),
             settings.TRANSLATE_BUDGET_CNY)
    _register_summary(CronTrigger)
    _register_weekly(CronTrigger)
    _scheduler.start()
    return _scheduler


def _register_summary(CronTrigger):
    """周期总结（M7）：日报 + Guard + 启动补跑。

    三个 job 分工：
      · daily      07:30 编排（misfire_grace_time=4h → 合盖唤醒后还能补一次）
      · guard      每 30 分钟兜底：过点还没 done 就跑今天（catch_up=False，允许刷新）
      · catch_up   启动后 15 秒扫最近 N 天，补睡过去的那几天（历史日期跳过刷新）
    """
    from core import summary
    from datetime import datetime, timedelta

    # ★ 启动即清残留（2026-10-17 事故）：进程死在 time.sleep 里（机器睡眠 +
    #   强杀），SummaryRun 永远停在 waiting。reset_stale() 的默认阈值是
    #   SUMMARY_STALE_MIN=90 分钟 —— 意味着重启后还要干等 90 分钟才自愈。
    #   但本服务是**单进程**设计（--noreload，无第二个 runner）：启动这一刻，
    #   任何 IN_FLIGHT 行都必然是死进程残留 ⇒ max_age_min=0 全部重置。
    #   随后 guard 首跳（下面 next_run_time=+15s）立刻把今天的日报补上。
    n_stale = summary.reset_stale(max_age_min=0)
    if n_stale:
        log.warning("启动清残留：%s 条 in-flight 总结已重置为 pending", n_stale)

    _scheduler.add_job(
        _safe(summary.run, "summary"),
        CronTrigger(hour=settings.SUMMARY_HOUR, minute=settings.SUMMARY_MINUTE,
                    timezone=settings.TIME_ZONE),
        id="summary_daily", replace_existing=True,
        misfire_grace_time=4 * 3600, coalesce=True, max_instances=1,
    )
    _scheduler.add_job(
        _safe(summary.guard_tick, "summary_guard"),
        trigger="interval", minutes=settings.SUMMARY_GUARD_MIN,
        # ★ 首跳 +15s：配合上面的启动清残留，重启后 15 秒内今日日报就补跑，
        #   不用再等第一个 30 分钟 interval 到点（那是本次事故"看起来没恢复"的一半）。
        next_run_time=datetime.now(_scheduler.timezone) + timedelta(seconds=15),
        id="summary_guard", replace_existing=True,
    )
    # ★ 补跑是**周期性**的（每 Guard 间隔一轮），不是"启动只跑一次"：
    #   一轮最多补 SUMMARY_MAX_CATCHUP 天，剩下的下一轮继续。首次延 15 秒，
    #   别和 Web 抢启动那几秒。
    from datetime import datetime, timedelta

    _scheduler.add_job(
        _safe(lambda: summary.catch_up_scan(), "summary_catchup"),
        trigger="interval", minutes=settings.SUMMARY_GUARD_MIN,
        next_run_time=datetime.now(_scheduler.timezone) + timedelta(seconds=15),
        id="summary_catchup", replace_existing=True,
    )
    log.info("已注册周期总结：每天 %s:%02d 编排｜每 %s 分钟 Guard + 补跑（扫最近 %s 天，"
             "每轮最多 %s 天，单日重试上限 %s 次）",
             settings.SUMMARY_HOUR, settings.SUMMARY_MINUTE, settings.SUMMARY_GUARD_MIN,
             settings.SUMMARY_LOOKBACK_DAYS, settings.SUMMARY_MAX_CATCHUP,
             settings.SUMMARY_MAX_ATTEMPT)


def _register_weekly(CronTrigger):
    """周报（M9）：周日中午编排 + 每 guard 间隔兜底一次。

    与日报同款但更松：周报的输入是**已经落盘的 7 份日报文件**，不需要等 feed
    新鲜度，所以没有"补救刷新 / 等待"那一堆逻辑。

    ★ 为什么还要一个 guard：cron 那一跳可能撞上"当天日报还没跑完"或"进程刚重启"，
      guard 负责在重试窗口内把它补上（只读文件，零开销）。
    ★ 缺**历史**日报 ⇒ 立即 fail + 通知（历史日报不会自己出现，等也没用）；
      缺**当天**日报 ⇒ 窗口内静默重试，窗口结束才判死。
    """
    from datetime import datetime, timedelta

    from core import weekly
    n = weekly.reset_stale()
    if n:
        log.warning("启动清残留：%s 条 in-flight 周报已重置为 failed", n)

    _scheduler.add_job(
        _safe(weekly.run, "weekly"),
        CronTrigger(day_of_week=settings.WEEKLY_WEEKDAY,
                    hour=settings.WEEKLY_HOUR, minute=settings.WEEKLY_MINUTE,
                    timezone=settings.TIME_ZONE),
        id="weekly", replace_existing=True,
        misfire_grace_time=4 * 3600, coalesce=True, max_instances=1,
    )
    _scheduler.add_job(
        _safe(weekly.guard_tick, "weekly_guard"),
        trigger="interval", minutes=settings.SUMMARY_GUARD_MIN,
        # ★ 首跳 +45s：错开 summary_guard 的 +15s，别在启动那几秒和日报抢
        next_run_time=datetime.now(_scheduler.timezone) + timedelta(seconds=45),
        id="weekly_guard", replace_existing=True,
    )
    log.info("已注册周报：每周 %s %s:%02d 编排｜每 %s 分钟兜底"
             "（缺历史日报立即失败并通知，缺当天日报最多等 %s 小时）",
             "一二三四五六日"[int(settings.WEEKLY_WEEKDAY) % 7],
             settings.WEEKLY_HOUR, settings.WEEKLY_MINUTE,
             settings.SUMMARY_GUARD_MIN, settings.WEEKLY_RETRY_HOURS)


def _fast_count() -> int:
    try:
        from core.models import FeedOverride
        return FeedOverride.objects.filter(fast_refresh=True).count()
    except Exception:  # noqa: BLE001  建表前启动也要能起来
        return 0


def _translate_count() -> int:
    try:
        from core.models import FeedTranslation
        return FeedTranslation.objects.filter(enabled=True).count()
    except Exception:  # noqa: BLE001  建表前启动也要能起来
        return 0


def llm_model_name() -> str:
    from core import llm
    return llm.provider_cfg()["model"]


def shutdown():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
