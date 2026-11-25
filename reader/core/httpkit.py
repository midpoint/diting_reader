"""htmx 端点的小工具：统一 JSON 响应与参数解析。

此前 `_ok` / `_err` / `_int_param` 在 treeviews 与 groups 视图里各抄一份
（语义还略有分叉：一个缺参返回 None，一个抛 ValueError）。

★ 响应形状是 `app.js::postForm` 的既有契约：
    成功 `{ok: true, detail, ...}`；失败 `{ok: false, error}`（非 2xx）。
"""
from django.http import JsonResponse


def ok(detail: str = "", **extra):
    return JsonResponse({"ok": True, "detail": detail, **extra})


def err(msg, status: int = 400):
    return JsonResponse({"ok": False, "error": str(msg)[:300]}, status=status)


def int_param(request, key):
    """POST 参数 → int。缺席 / 非数字 → None（要不要当错误由调用方决定）。"""
    raw = (request.POST.get(key) or "").strip()
    return int(raw) if raw.isdigit() else None


def require_int(request, key) -> int:
    """POST 参数 → int；缺席 / 非数字抛 ValueError（给「缺参数」当错误的端点用）。"""
    raw = (request.POST.get(key) or "").strip()
    if not raw:
        raise ValueError(f"缺少参数 {key}")
    return int(raw)
