"""开销可视化（M5）。

两个页面：
  /costs                    总览 —— 卡片 / 按日或按小时堆叠柱 / Top feed / 分类分布 / 明细表 / 预算条
  /costs/feed/<id>          单 feed 下钻 —— 按日折线 + 逐次调用明细

图表全部用 CSS/内联 SVG 自绘，不引第三方 JS：离线可用、无 CDN 依赖，
风格也能跟阅读器保持一致。

时间范围（2026-10-20）：页头从「近 N 天」按钮升级为显式区间
（今天 / 昨天 / 近 7 天 / 近 30 天 / 本月 / 上月 / 自定义两个日期）。
区间解析在 `core.costreport.resolve_range`，视图只负责把 URL 拼好。
"""
import logging
import urllib.parse

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from core import costreport as cr
from core import treekit
from core.models import AppSetting

log = logging.getLogger(__name__)

SORTS = [("cost", "¥"), ("tokens", "tokens"), ("calls", "调用"),
         ("retries", "重试"), ("failures", "失败")]


def _tree_maps():
    """一次拿树 → (feed 标题表, 分类标题表, feed→分类)。

    ★ 开销页**不再渲染 feed 树**（树是阅读页专属），只要标题映射；
      Miniflux 挂了返回空映射（safe=True），页面仍能看公共项。
    """
    return treekit.title_maps(safe=True)


def _preset_links(base: str, active: str, keep: dict = None) -> list:
    keep = keep or {}
    out = []
    for key, label in cr.PRESETS:
        q = urllib.parse.urlencode({"preset": key, **keep})
        out.append({"key": key, "label": label, "url": f"{base}?{q}",
                    "active": active == key})
    return out


def _cards(rng: cr.Range) -> list:
    """汇总卡：**只保留当前区间这一张**。

    ★ 不再放固定窗口（今日/近7/近30）：卡片数量会随区间在 3/4 张之间跳，
      页面视觉不稳定；要看别的区间，点上面的预设即可（一次点击的成本很低）。
    """
    return [{"label": f"当前区间 · {rng.label}",
             "s": cr.summary_range(rng), "days": rng.days, "current": True}]


def index(request):
    rng = cr.resolve_range(request.GET, default=cr.DEFAULT_PRESET)
    sort = request.GET.get("sort") or "cost"
    direction = request.GET.get("dir") or "desc"
    feeds, cats, feed_to_cat = _tree_maps()

    chart = cr.by_hour(rng.start) if rng.single_day else cr.by_day_range(rng)
    range_qs = rng.query()          # preset（+ custom 的 start/end）
    keep = {"sort": sort, "dir": direction}

    ctx = {
        # ★ 卡片是固定窗口，不跟随页头区间（页头区间只管图表与明细）
        "cards": _cards(rng),
        "chart": chart,
        # 多日柱点击 → 当天单日视图；下钻页把 base 换成 /costs/feed/N
        "day_link_base": "/costs",
        "day_link_extra": "&" + urllib.parse.urlencode(keep),
        "tops": cr.top_feeds_range(rng, titles=feeds),
        "cats": cr.by_category_range(rng, cat_titles=cats, feed_to_cat=feed_to_cat),
        "table": cr.feed_table_range(rng, sort=sort, direction=direction,
                                     titles=feeds),
        "budget": cr.budget_status(),
        "sort": sort,
        "dir": direction,
        "sorts": SORTS,
        # 时间范围
        "range": rng,
        "range_label": rng.label,
        "range_qs": range_qs,
        "drill_qs": range_qs,
        "presets": _preset_links("/costs", rng.preset, keep),
        "range_action": "/costs",
        "range_hidden": keep,
        "clear_url": ("/costs?" + urllib.parse.urlencode(keep)
                      if not rng.is_default else ""),
    }
    return render(request, "costs/index.html", ctx)


def feed_detail(request, feed_id):
    # ★ 与总览一致：默认近 7 天（之前是 30 天，从总览点进来会跳变）
    rng = cr.resolve_range(request.GET, default=cr.DEFAULT_PRESET)
    feeds, _cats, _f2c = _tree_maps()
    data = cr.feed_detail_range(feed_id, rng)
    if feed_id in feeds:
        data["title"] = feeds[feed_id]
    elif feeds:
        data["title"] = f"feed:{feed_id}（已删除）"
    else:
        data["title"] = f"feed:{feed_id}"

    data.update({
        "range": rng,
        "range_label": rng.label,
        # ★ 单日 → 24 小时柱；多日 → 按日柱。两者都**必须带 feed_id**，
        #   否则画的是全部源的总览数据（2026-10-20 实测踩到）。
        "chart": (cr.by_hour(rng.start, feed_id=feed_id) if rng.single_day
                  else cr.by_day_range(rng, feed_id=feed_id)),
        # 下钻页的柱子点击 → 本源的当天视图
        "day_link_base": f"/costs/feed/{feed_id}",
        "day_link_extra": "",
        "presets": _preset_links(f"/costs/feed/{feed_id}", rng.preset),
        "range_action": f"/costs/feed/{feed_id}",
        "range_hidden": {},
        "clear_url": (f"/costs/feed/{feed_id}"
                      if not rng.is_default else ""),
        "back_url": "/costs?" + rng.query(),
    })
    return render(request, "costs/feed_detail.html", data)


@require_POST
def set_budget(request):
    """预算写 AppSetting。`pool` 指定改哪个池，缺省改日报（summary）池。

    ★ 池 → 键的映射**只许查** `cr.POOL_BUDGET_KEYS`，不要在这里手抄一张表：
      手抄的版本一旦漏项，未知池会被静默写进日报池的键 —— 返回 ok=true，
      但被改的池子纹丝不动（2026-10-29 周报池就这么"保存不了"过）。
      未知池直接 400，宁可报错也不要写错地方。
    """
    raw = (request.POST.get("budget_cny") or "").strip()
    try:
        value = float(raw)
        assert value >= 0
    except (ValueError, AssertionError):
        return JsonResponse({"ok": False, "error": "预算必须是非负数"}, status=400)
    pool = (request.POST.get("pool") or cr.POOL_SUMMARY).strip()
    key = cr.POOL_BUDGET_KEYS.get(pool)
    if key is None:
        return JsonResponse({"ok": False, "error": f"未知预算池：{pool}"}, status=400)
    old = cr.pool_budget_cny(pool)
    AppSetting.objects.update_or_create(key=key, defaults={"value": f"{value:g}"})
    # ★ 人改预算才走到这里（低频），记下旧值 → 新值：reqlog 只记路径，
    #   看不出改了哪个池、从多少改到多少。2026-11-17 的三连重跑就是
    #   「提预算 → 立刻重跑」，没有这条日志只能靠相邻请求的时间戳反推。
    log.info("预算变更 pool=%s ¥%g → ¥%g", pool, old, value)
    return JsonResponse({"ok": True, "limit_cny": value, "pool": pool,
                         "label": cr.POOL_LABELS.get(pool, pool)})
