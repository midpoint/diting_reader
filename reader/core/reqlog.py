"""请求日志中间件：每个 HTTP 请求一行，慢请求与失败升级告警。

为什么自己做而不是用 Django 自带的
----------------------------------
`django.request` 只在 4xx/5xx 时说话（且不告诉你**花了多久**），
runserver 的 `django.server` access log 有耗时但是零结构化字段、还没法按级别过滤。
本地工具真正想知道的是：**哪个页面变慢了**、**哪个接口在报错**、**刚才那次点击到底发生了什么**。

级别
----
正常请求   → `DEBUG`（文件里留全量，屏幕上看不见；`LOG_REQUESTS=1` 时升到 INFO）
慢请求     → `WARNING`（超过 `LOG_SLOW_MS`，**与开关无关**：卡顿必须能看见）
4xx        → `WARNING`
5xx / 抛异常 → `ERROR`（异常带 traceback）

★ 自动轮询端点（`/logs/rows`）默认不记：每几秒一条会把日志冲成流水，
  见 `settings.LOG_IGNORE_PATHS`。
"""
import logging
import time

from django.conf import settings

from core.logkit import fields

log = logging.getLogger("core.reqlog")


class RequestLogMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.ignore = tuple(getattr(settings, "LOG_IGNORE_PATHS", ()) or ())
        self.level = getattr(logging, getattr(settings, "LOG_REQUEST_LEVEL", "DEBUG"),
                             logging.DEBUG)
        self.slow_ms = int(getattr(settings, "LOG_SLOW_MS", 800))

    def __call__(self, request):
        path = request.path or "/"
        if self.ignore and path.startswith(self.ignore):
            return self.get_response(request)

        t0 = time.monotonic()
        try:
            response = self.get_response(request)
        except Exception:
            # ★ 未捕获异常：这里必须记全栈，否则 Django 只在 5xx 响应里给用户看，
            #   后台跑的时候没人看得到（这正是"只能看用起来有没有问题"的根源）。
            log.error("%s %s 抛异常（%.0fms 后）", request.method, path,
                      (time.monotonic() - t0) * 1000, exc_info=True,
                      extra=fields(ms=int((time.monotonic() - t0) * 1000)))
            raise

        ms = int((time.monotonic() - t0) * 1000)
        status = getattr(response, "status_code", 0)
        extra = fields(ms=ms, status=status)
        if status >= 500:
            log.error("%s %s → %s", request.method, path, status, extra=extra)
        elif status >= 400:
            log.warning("%s %s → %s", request.method, path, status, extra=extra)
        elif ms >= self.slow_ms:
            log.warning("%s %s → %s 慢（阈值 %sms）", request.method, path, status,
                        self.slow_ms, extra=extra)
        else:
            log.log(self.level, "%s %s → %s", request.method, path, status, extra=extra)
        return response
