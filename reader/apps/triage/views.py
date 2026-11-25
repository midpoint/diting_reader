"""分级配置 / prompt 查看 / 手工调级（M4）。

只做两件 Miniflux 没有的事：配置「每个源怎么算重要」、把级别落到本地库。
Miniflux 仍是正文与已读的真相层。
"""
import logging
import threading

from django.conf import settings
from django.http import (HttpResponseBadRequest, HttpResponseRedirect,
                         JsonResponse)
from django.shortcuts import render
from django.views.decorators.http import require_POST

from core import promptbuilder, promptspec, triage
from core import treekit
from core.datasource import get_datasource
from core.models import (CategoryTriageConfig, EntryRating, FeedOverride,
                         FeedTriageConfig, TriagePrompt)

log = logging.getLogger(__name__)

LEVEL_CHOICES = [(0, "L0 忽略"), (1, "L1 低价值/普通"), (2, "L2 关注"), (3, "L3 重点")]


def _parse_scope(scope: str):
    kind, sid = ("all", None)
    if scope.startswith("feed:"):
        kind, sid = "feed", int(scope[5:])
    elif scope.startswith("cat:"):
        kind, sid = "cat", int(scope[4:])
    return kind, sid


def _title_of(kind, sid, tree=None):
    """scope 的显示名（分类 / feed 标题）。"""
    if kind == "cat":
        return treekit.category_title(sid, tree, default=f"cat:{sid}")
    return treekit.feed_title(sid, tree, default=f"feed:{sid}")


# ------------------------------------------------------------------ 配置弹窗
def config_form(request):
    """GET：渲染配置弹窗（分类 / feed 通用）。"""
    kind, sid = _parse_scope(request.GET.get("scope", ""))
    if kind == "all":
        return HttpResponseBadRequest("scope 必须是 cat:N 或 feed:N")

    if kind == "cat":
        row = CategoryTriageConfig.objects.filter(category_id=sid).first()
        inherited, inherited_from = None, None
    else:
        row = FeedTriageConfig.objects.filter(feed_id=sid).first()
        # 本源的「分类继承」提示：一次取树，拿到所属分类 id + 标题
        tree = treekit.tree()
        cat_id = treekit.cat_of(sid, tree)
        cat_row = (CategoryTriageConfig.objects.filter(category_id=cat_id).first()
                   if cat_id else None)
        inherited = cat_row.l2_text if cat_row else ""
        inherited_from = (f"分类「{treekit.category_title(cat_id, tree)}」"
                          if cat_id else None)

    prev = TriagePrompt.objects.filter(
        feed_id=sid).order_by("-version").first() if kind == "feed" else None

    return render(request, "triage/config_form.html", {
        "kind": kind, "sid": sid, "scope": f"{kind}:{sid}",
        "title": _title_of(kind, sid),
        "row": row,
        "inherited": inherited,
        "inherited_from": inherited_from,
        "level_choices": LEVEL_CHOICES,
        "has_prompt": bool(prev),
    })


@require_POST
def config_save(request):
    kind, sid = _parse_scope(request.POST.get("scope", ""))
    if kind == "all":
        return HttpResponseBadRequest("scope 非法")

    fields = {f"l{i}_text": (request.POST.get(f"l{i}_text") or "").strip()
              for i in range(4)}
    model = CategoryTriageConfig if kind == "cat" else FeedTriageConfig
    key = {"category_id": sid} if kind == "cat" else {"feed_id": sid}

    # ★ 只有 feed 有默认级别；分类不设（feed 留空 = 全局默认 L2）
    if kind == "feed":
        dl = request.POST.get("default_level")
        fields["default_level"] = int(dl) if dl not in (None, "", "inherit") else None
        # ★ 本源备注只属 feed（文本结构说明，不是判据），分类层没有这一项
        fields["note"] = (request.POST.get("note") or "").strip()

    model.objects.update_or_create(defaults=fields, **key)

    # ★ 配置变动 → 重建受影响 feed 的 prompt（不重判历史）。
    #   一次取树喂三处（cat_of / feed_title / feeds_of_category）——
    #   旧实现一个请求隐式拉 3-4 次树，全靠数据源 20s 缓存兜底。
    rebuilt = []
    tree = treekit.tree()
    if kind == "feed":
        row, _spec = promptbuilder.build(sid, category_id=treekit.cat_of(sid, tree),
                                         feed_title=treekit.feed_title(sid, tree))
        rebuilt = [sid] if row else []
    else:
        rebuilt = promptbuilder.rebuild_category(sid,
                                                 treekit.feeds_of_category(sid, tree))

    return render(request, "triage/config_saved.html", {
        "kind": kind, "sid": sid, "title": _title_of(kind, sid, tree),
        "rebuilt": rebuilt,
    })


# ------------------------------------------------------------------ prompt 查看
def prompt_view(request):
    feed_id = int(request.GET.get("feed") or 0)
    if not feed_id:
        return HttpResponseBadRequest("缺 feed")
    rows = list(TriagePrompt.objects.filter(feed_id=feed_id).order_by("-version"))
    active = next((r for r in rows if r.status == "active"), None)
    tree = treekit.tree()
    spec = promptspec.build_spec(feed_id, category_id=treekit.cat_of(feed_id, tree),
                                 feed_title=treekit.feed_title(feed_id, tree))
    levels = []
    for i in range(4):
        seg = spec.get(f"l{i}")
        levels.append({
            "label": promptspec.LEVEL_ZH.get(i, f"L{i}"),
            "text": (seg or {}).get("text", ""),
            "parts": [{"src": "分类" if p["from"] == "category" else "本源",
                       "from": p["from"]} for p in (seg or {}).get("parts", [])],
        })
    template = ("triage/prompt_modal.html"
                if request.headers.get("HX-Request") == "true"
                else "triage/prompt.html")
    return render(request, template, {
        "feed_id": feed_id,
        "title": _title_of("feed", feed_id, tree),
        "active": active,
        "versions": rows,
        "spec": spec,
        "spec_levels": levels,
        "base_system": promptspec.BASE_SYSTEM,
        "base_rules": promptspec.BASE_RULES,
        "level_zh": promptspec.LEVEL_ZH,
    })


@require_POST
def prompt_rebuild(request):
    feed_id = int(request.POST.get("feed") or 0)
    if not feed_id:
        return HttpResponseBadRequest("缺 feed")
    tree = treekit.tree()
    row, _ = promptbuilder.build(feed_id, category_id=treekit.cat_of(feed_id, tree),
                                 feed_title=treekit.feed_title(feed_id, tree),
                                 force=True)
    return JsonResponse({"ok": bool(row), "version": row.version if row else None,
                         "built_by": row.built_by if row else None,
                         "validated": row.validated if row else None})


# ------------------------------------------------------------------ 手工调级
@require_POST
def set_level(request):
    """单条或批量改级别。★ 改到 L0 会标已读（不可逆）。

    ★★ 改到 L3 **不加星**：加星是读者的手动确认（列表页 ★ 按钮），
      分级调级只表达「这条按 L3 处置（候选 + 通知语义）」，
      两个动作必须分开，否则「加星率」不再是可信信号。
    """
    ids = [int(x) for x in (request.POST.get("entry_ids") or "").split(",") if x.strip()]
    try:
        level = int(request.POST.get("level"))
    except (TypeError, ValueError):
        return HttpResponseBadRequest("level 非法")
    if not ids or level not in (0, 1, 2, 3):
        return HttpResponseBadRequest("参数非法")

    ds = get_datasource()
    note = (request.POST.get("note") or "手工调整").strip()[:200]
    done, marked = 0, 0
    for eid in ids:
        row = EntryRating.objects.filter(entry_id=eid).first()
        prev = row.level if row else None
        EntryRating.objects.update_or_create(
            entry_id=eid,
            defaults={
                "feed_id": row.feed_id if row else 0,
                "category_id": row.category_id if row else None,
                "level": level, "reason": "manual", "manual": True,
                "manual_note": note, "prev_level": prev,
                "votes": (row.votes if row else []) + [{"manual": level, "why": note}],
            },
        )
        done += 1
        try:
            if level == 0:
                marked += ds.mark_read([eid]) or 0
        except Exception as exc:  # noqa: BLE001
            log.warning("手工调级处置失败 entry=%s：%s", eid, exc)

    return JsonResponse({"ok": True, "updated": done, "level": level,
                         "marked_read": marked})


@require_POST
def rejudge(request):
    """重判某个 feed 的历史条目（删掉评级，下一轮 TriageWorker 会重跑）。"""
    feed_id = int(request.POST.get("feed") or 0)
    if not feed_id:
        return HttpResponseBadRequest("缺 feed")
    n, _ = EntryRating.objects.filter(feed_id=feed_id).delete()
    return JsonResponse({"ok": True, "deleted": n, "feed_id": feed_id})


# ------------------------------------------------------------------ 快速刷新（M6）
def _fast_payload(feed_id):
    from core import fastrefresh

    row = FeedOverride.objects.filter(feed_id=feed_id).first()
    on = bool(row and row.fast_refresh)
    return {"ok": True, "feed_id": feed_id, "on": on,
            "minutes": (row.fast_interval_min if row else settings.FAST_REFRESH_DEFAULT_MIN),
            "last_fast_at": (row.last_fast_at.isoformat() if row and row.last_fast_at else None),
            "error": (row.last_error if row else ""),
            "jobs": fastrefresh.state()}


@require_POST
def fast_refresh(request):
    """开 / 关 feed 的快速刷新，或立刻刷新一次。

    action = on | off | now
    ★ 走 PUT /v1/feeds/{id}/refresh（同步、绕过 worker 池与 host 限额）；
      已在 core/fastrefresh.py 里做限速，UI 只是配置入口。
    """
    from core import fastrefresh

    feed_id = int(request.POST.get("feed") or 0)
    if not feed_id:
        return HttpResponseBadRequest("缺 feed")
    action = (request.POST.get("action") or "on").strip()

    if action == "now":
        return JsonResponse(fastrefresh.refresh_now(feed_id))

    minutes = int(request.POST.get("minutes") or settings.FAST_REFRESH_DEFAULT_MIN)
    minutes = max(2, min(minutes, 24 * 60))
    on = action == "on"
    FeedOverride.objects.update_or_create(
        feed_id=feed_id,
        defaults={"fast_refresh": on, "fast_interval_min": minutes},
    )
    return JsonResponse(_fast_payload(feed_id))


# ------------------------------------------------------------------ 链路状态（JSON）
def status(request):
    """`/triage/status`：分级链路的机器可读总览（待判/失败/覆盖/预算/最近调用）。

    原来这里还渲染过一个 `/triage` 页面，2026-10-15 下线了 —— 列表页的徽标
    （彩色=判过 / 红=失败兜底 / 灰=没判定，hover 出依据）已经把"心里有数"
    这件事放在了看文章的地方，再单独养一个报表页只会堆信息。
    """
    return JsonResponse(triage.overview())


@require_POST
def rate_now(request):
    """立即判定 / 重新判定单条（文章右键菜单）。

    mode=re 会先删掉已有评级（含人工调整）—— 所以前端要二次确认。
    同步执行：单条最多 TRIAGE_REPEAT 次调用（自适应），失败也返回 JSON。
    """
    entry_id = request.POST.get("entry_id") or ""
    if not entry_id.isdigit():
        return JsonResponse({"ok": False, "error": "entry_id 非法"}, status=400)
    force = request.POST.get("mode") == "re"
    res = triage.rate_single(int(entry_id), force=force)
    return JsonResponse(res, status=200 if res.get("ok") else 502)


@require_POST
def run_now(request):
    """手动跑一轮分级（后台线程；与调度器之间由 triage 的进程内锁互斥）。"""
    if triage.is_running():
        return _back(request, "running=1")

    def _work():
        from django.db import close_old_connections

        close_old_connections()
        try:
            triage.run_once()
        except Exception as exc:  # noqa: BLE001
            log.error("手动分级崩溃：%s", exc)
        finally:
            close_old_connections()

    threading.Thread(target=_work, name="triage-manual", daemon=True).start()
    return _back(request, "ran=1")


@require_POST
def rejudge_failed(request):
    """清掉 fail_open 的评级 → 下一轮重新判（default / llm / manual 不动）。"""
    n = triage.unrate_failed(feed_id=request.POST.get("feed") or None,
                            limit=request.POST.get("limit") or None)
    return _back(request, f"rejudged={n}")


def _back(request, query: str):
    """POST 完回列表页（HTMX/JSON 调用方拿 JSON，别让浏览器停在裸 JSON 上）。"""
    if request.headers.get("HX-Request") == "true":
        return JsonResponse({"ok": True, "query": query})
    return HttpResponseRedirect(f"/?{query}")
