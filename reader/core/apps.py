import logging
import os

import django
from django.apps import AppConfig
from django.conf import settings
from django.db.backends.signals import connection_created

from core.logkit import fields

log = logging.getLogger(__name__)
_banner_done = False


def _sqlite_pragmas(sender, connection, **kwargs):
    """SQLite 多线程写：WAL + busy_timeout（风险 #5）。"""
    if connection.vendor != "sqlite":
        return
    cursor = connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=20000")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def _startup_banner(scheduler_started: bool):
    """启动横幅：一行说清"现在是什么状态在跑"。

    ★ 这是排查任何问题时的**第一参照物** —— 没有它就得靠猜：
      服务几点起的？调度开没开？预算多少？日志写哪儿？
    ★ 只在 Web 进程（调度真起来了）记 INFO；`manage.py shell` / 迁移脚本
      这类一次性命令记 DEBUG，免得跑个脚本也刷一条横幅。
    """
    global _banner_done
    if _banner_done:
        return
    _banner_done = True

    if not scheduler_started:
        log.debug("启动（无调度）：pid=%s", os.getpid())
        return

    log.info("谛听阅读器启动 pid=%s django=%s debug=%s 日志=%s（控制台 %s / 文件 %s）",
             os.getpid(), django.get_version(), settings.DEBUG,
             settings.LOG_DIR / "reader.log", settings.LOG_LEVEL,
             settings.LOG_FILE_LEVEL,
             extra=fields(src="boot", pid=os.getpid()))
    # 数据源一行只报"当前真正在用的那个"，避免切到 greader 后仍显示 miniflux 地址
    _engine = getattr(settings, "DATASOURCE_ENGINE", "miniflux")
    _ds_url = (settings.GREADER_BASE_URL if _engine == "greader"
               else settings.MINIFLUX_BASE_URL)
    log.info("调度已开：分级每 %s 分钟｜快速刷新每 %s 秒（默认 %s 分钟）｜"
             "周期总结每天 %s:%02d 编排｜数据源=%s（%s）（启用开关在设置页，job 入口即时生效）",
             settings.TRIAGE_INTERVAL_MIN,
             settings.FAST_REFRESH_TICK_SEC if settings.FAST_REFRESH_ENABLED else "—",
             settings.FAST_REFRESH_DEFAULT_MIN,
             settings.SUMMARY_HOUR, settings.SUMMARY_MINUTE,
             _engine, _ds_url,
             extra=fields(src="boot"))
    log.info("预算（今日池）：分级 ¥%s｜总结 ¥%s｜条目窗口 %s 条｜分级回看 %s 天",
             settings.TRIAGE_BUDGET_CNY, settings.SUMMARY_BUDGET_CNY,
             settings.ENTRY_WINDOW, settings.TRIAGE_LOOKBACK_DAYS,
             extra=fields(src="boot"))
    # ★ 通知单独的可用性一行：装了没 / 点哪个程序 / 通知哪几类。
    #   "开关开着但 binary 不在"是最容易静默失效的一种（通知不报错、就是不响）。
    _notify_line = "未开启（NOTIFY_ENABLE=0）"
    if settings.NOTIFY_ENABLE:
        import shutil

        from core import notify as _notify

        backend = _notify._backend()
        # mac 看 binary 在不在；win 看 shim 在不在（winotify 是否 pip 装了由
        # shim 自己报，失败会记 warning，不在这里假装"已就绪"）。
        ready = (_notify._ready())
        label = (_notify._shim() if backend == "win" else settings.NOTIFY_BIN)
        if ready:
            kinds = " · ".join([n for n, on in (("L3 点名", settings.NOTIFY_L3),
                                                ("日报完成", settings.NOTIFY_DAILY),
                                                ("日报失败", settings.NOTIFY_DAILY_FAIL))
                               if on])
            _notify_line = f"已开启（{kinds or '两类都关'}）｜{backend}｜{label}"
        else:
            _notify_line = f"**开关开着但找不到 {label}**（通知不会响）"
    log.info("系统通知：%s", _notify_line,
             extra=fields(src="boot", notify=settings.NOTIFY_ENABLE))


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"
    label = "core"

    def ready(self):
        connection_created.connect(_sqlite_pragmas)
        # 常驻调度（内部自带 autoreload 守卫）
        started = None
        try:
            from core import scheduler
            started = scheduler.start()
        except Exception:  # noqa: BLE001
            log.error("scheduler 启动失败", exc_info=True)
        _startup_banner(started is not None)
