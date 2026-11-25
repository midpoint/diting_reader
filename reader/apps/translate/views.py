"""标题翻译的写操作与状态接口。

★ 只碰本地数据：开关写 `FeedTranslation`，译文写 `EntryTranslation`，
  **不写 Miniflux**（标题原文永远是 Miniflux 的真相）。
★ 响应约定与 `app.js::postForm` 一致：`{ok, detail}` / `{ok:false, error}`。
"""
import logging

from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST

from core import translate

log = logging.getLogger(__name__)


def _int_arg(request, *names):
    for name in names:
        raw = (request.POST.get(name) or request.GET.get(name) or "").strip()
        if raw:
            try:
                return int(raw)
            except ValueError:
                return None
    return None


@require_POST
def set_feed(request):
    """开启 / 关闭某个 feed 的标题翻译。"""
    feed_id = _int_arg(request, "feed", "feed_id")
    if not feed_id:
        return JsonResponse({"ok": False, "error": "feed 非法"}, status=400)
    action = (request.POST.get("action") or "").strip().lower()
    if action not in ("on", "off"):
        return JsonResponse({"ok": False, "error": "action 必须是 on / off"}, status=400)
    try:
        res = translate.set_enabled(feed_id, action == "on")
    except Exception as exc:  # noqa: BLE001  写开关失败要给前端原话，不能 500
        log.warning("标题翻译开关失败 feed=%s：%s", feed_id, exc)
        return JsonResponse({"ok": False, "error": f"保存失败：{exc}"}, status=500)
    if not res.get("ok"):
        return JsonResponse(res, status=400)
    return JsonResponse(res)


@require_POST
def retranslate(request):
    """文章右键：翻译 / 重新翻译单条标题。"""
    entry_id = _int_arg(request, "entry_id", "entry")
    if not entry_id:
        return JsonResponse({"ok": False, "error": "entry_id 非法"}, status=400)
    try:
        res = translate.retranslate_entry(entry_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("标题重译失败 entry=%s：%s", entry_id, exc)
        return JsonResponse({"ok": False, "error": f"翻译失败：{exc}"}, status=500)
    return JsonResponse(res, status=200 if res.get("ok") else 400)


@require_GET
def status(request):
    """前端轮询：任务状态 + 翻译池余量。"""
    return JsonResponse(translate.status())
