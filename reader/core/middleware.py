"""数据源不可用时的用户可见错误（不把 500 技术页/堆栈丢给人看）。

为什么放在中间件
----------------
数据源异常会在任意入口冒出来：首屏、树、条目详情、日报、分级页。
逐个视图加 try/except 既容易漏，也会让业务代码到处长一样的兜底。
这里统一接住 `GReaderError`（其他数据源的异常保持原样，行为不变）：

  · 普通页面 → 502 + 一页人话（含具体端点/HTTP 状态/下一步怎么做）
  · JSON / 写接口 → 502 + `{"ok": false, "error": "..."}`
  · HTMX 片段请求 → **200 + 错误横幅**：htmx 默认不交换 4xx/5xx 响应，
    返回 502 用户反而什么也看不到。

★ 只接 GReaderError。Miniflux 的异常类型/行为完全不动。
"""
import logging

from django.http import HttpResponse, JsonResponse
from django.utils.html import escape

from core.datasource.greader import GReaderError
from core.logkit import fields

log = logging.getLogger("core.datasource")

# 习惯返回 JSON 的路径前缀（树/写接口/日报/分级/设置等）
_JSON_PREFIXES = (
    "/hx/mark", "/hx/star", "/hx/refresh", "/hx/fetch-full",
    "/tree/", "/summary/", "/triage/", "/costs/", "/prefs/",
    "/wiki/", "/groups/", "/translate/", "/logs/",
)


class DataSourceErrorMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        """Django 会先把视图异常转成 500；这里在转换前接住 GReaderError。

        ★ 不能只在 `__call__` 里 try/except：Django 给每个 middleware 的
          get_response 都套了 convert_exception_to_response，异常到不了外层。
        """
        if not isinstance(exception, GReaderError):
            return None
        log.error("数据源不可用 %s %s：%s", request.method, request.path, exception,
                  extra=fields(src="datasource",
                               status=getattr(exception, "status", None)))
        log.debug("数据源异常堆栈", exc_info=True)
        return _error_response(request, exception)


def _error_response(request, exc: GReaderError):
    message = str(exc)
    if request.headers.get("HX-Request") == "true":
        # 200 是刻意的：htmx 不交换非 2xx 响应，502 会让错误彻底不可见。
        return HttpResponse(
            f'<div class="error-banner" role="alert">'
            f'<strong>数据源不可用</strong>：{escape(message)}'
            f'</div>',
            content_type="text/html; charset=utf-8")

    if _wants_json(request):
        return JsonResponse({"ok": False, "error": message}, status=502,
                            json_dumps_params={"ensure_ascii": False})

    hint = "检查 `.env` 里的 GREADER_BASE_URL / GREADER_USERNAME / GREADER_PASSWORD，以及 FreshRSS 是否在运行。"
    body = (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>数据源不可用</title><style>"
        "body{font:15px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "max-width:760px;margin:10vh auto;padding:0 24px;color:#222}"
        ".box{border:1px solid #e5c07b;background:#fff8e6;border-radius:10px;padding:18px 20px}"
        "h1{font-size:18px;margin:0 0 10px}p{margin:8px 0}"
        "code{background:#f3f3f3;padding:1px 5px;border-radius:4px}"
        "a{color:#0b63ce}</style></head><body>"
        "<div class=\"box\"><h1>数据源不可用</h1>"
        f"<p>{escape(message)}</p>"
        f"<p class=\"hint\">{escape(hint)}</p>"
        "<p><a href=\"/\">重试</a></p></div></body></html>"
    )
    return HttpResponse(body, status=502, content_type="text/html; charset=utf-8")


def _wants_json(request) -> bool:
    path = request.path or ""
    if path.startswith(_JSON_PREFIXES):
        return True
    accept = (request.headers.get("Accept") or "").lower()
    return "application/json" in accept
