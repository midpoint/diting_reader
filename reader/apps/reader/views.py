"""阅读器视图：树 / 列表 / 工具栏（M1+M2，纯消费，零 LLM 成本）。"""
import logging
import re
import time
import urllib.parse

from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core import clicktrack, httpkit, promptspec
from core import refreshjob
from core import translate as translate_core
from core import triage as triage_core
from core import readergroups as rgroups
from core import treeorder
from core.datasource import get_datasource
from core.models import EntryFullFetch, EntryRating, FeedGroup, StarEvent

log = logging.getLogger(__name__)

# 级别筛选：★★ **多选**（`?levels=1,3`），不是「阈值 ≥N」。
#   旧写法 `?level=2`（= L2 及以上）已废弃但仍兼容，见 `_levels_arg()`。
#   理由：阈值只能表达"L2 及以上"，没法表达「只看 L0」「L1 和 L3」这类组合。
LEVEL_FILTERS = [
    (0, "L0", "丢弃（已标已读）"),
    (1, "L1", "普通（留未读）"),
    (2, "L2", "相关（未判定按此级）"),
    (3, "L3", "重点（候选，星标须手动）"),
]
LEVEL_ALL = (0, 1, 2, 3)

MARK_CHOICES = [
    (1, "一天前的文章"),
    (2, "两天前的文章"),
    (3, "三天前的文章"),
    (7, "一周前的文章"),
    (14, "两周前的文章"),
]


# ------------------------------------------------------------------ 参数解析
def _levels_arg(request) -> tuple:
    """级别多选：`?levels=1,3` → (1, 3)。

    ★ 语义约定（前后端一致，改一处必须改两处）：
      - 参数缺席 / 空串 / 四个都选  → `LEVEL_ALL`，即**不筛**（默认全看）
      - 解析不出任何合法级别（含全不勾）→ 同样退回 `LEVEL_ALL`
        理由：勾选框允许全部取消，若把"全不选"解释成"一条都不显示"，
        用户很容易以为列表坏了；退回全看 + 标签写「全部」更不容易误解。
      - ★ 兼容旧的阈值写法：`?level=2` 等价 `?levels=2,3`（旧的「L2+」）
    """
    raw = request.GET.get("levels")
    if raw is None:
        raw = request.POST.get("levels")
    if raw is None:
        legacy = request.GET.get("level")
        if legacy is None:
            legacy = request.POST.get("level")
        if legacy in (None, ""):
            return LEVEL_ALL
        try:
            lo = max(0, min(3, int(legacy)))
        except ValueError:
            return LEVEL_ALL
        return tuple(range(lo, 4))  # level=0 → 全部；level=2 → (2, 3)

    picked = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            v = int(part)
        except ValueError:
            continue
        if v in LEVEL_ALL and v not in picked:
            picked.append(v)
    if not picked:
        return LEVEL_ALL
    return tuple(sorted(picked))


def _level_label(levels) -> str:
    """下拉按钮上的文字：全选 → 「级别：全部」，否则列出级别号。"""
    if tuple(levels) == LEVEL_ALL:
        return "级别：全部"
    return "级别：" + "、".join(f"L{v}" for v in levels)


def _levels_value(levels) -> str:
    """hidden 输入的值：全选 → 空串（不写参数），否则 "1,3"。"""
    if tuple(levels) == LEVEL_ALL:
        return ""
    return ",".join(str(v) for v in levels)


def _filters(request) -> dict:
    sel = request.GET.get("sel") or request.POST.get("sel") or "all"
    status = request.GET.get("status") or request.POST.get("status") or "all"
    if status not in ("all", "unread"):
        status = "all"
    q = (request.GET.get("q") or request.POST.get("q") or "").strip()
    return {"sel": sel, "status": status, "q": q, "levels": _levels_arg(request)}


def _scope(sel: str):
    """`sel` → (范围类型, id)。`group:` 是**虚拟分组**（本地层，见 core/readergroups.py）。"""
    if sel == "starred":
        return "starred", None
    if sel.startswith("cat:"):
        try:
            return "cat", int(sel[4:])
        except ValueError:
            return "all", None
    if sel.startswith("feed:"):
        try:
            return "feed", int(sel[5:])
        except ValueError:
            return "all", None
    if sel.startswith("group:"):
        try:
            return "group", int(sel[6:])
        except ValueError:
            return "all", None
    return "all", None


def _query_without(filters: dict, *skip) -> str:
    """把当前筛选拼回查询串（树链接 / 刷新按钮 / mark-form 复用）。

    ★ 级别全选时不写 `levels=` —— 让默认态 URL 干净（与旧版 `level=0` 不同）。
    """
    parts = []
    for key in ("sel", "status", "q", "levels"):
        if key in skip:
            continue
        val = filters.get(key)
        if key == "levels":
            val = _levels_value(val)  # 全选 → ""，下面统一跳过
            if not val:
                continue
        elif val in (None, ""):
            continue
        parts.append((key, str(val)))
    return urllib.parse.urlencode(parts)


# ------------------------------------------------------------------ 数据
def _tree():
    """当前树 + 虚拟分组 + 手动顺序 + 翻译标记。

    ★ 顺序有意义：先 annotate 分级/翻译标记、再 decorate 分组、最后 apply 顺序。
      分组里装的是**同一批 FeedNode 的引用**，所以标记自动可见，不需要第二遍。
    ★ `treeorder.apply` 返回**新的**顶层列表（不就地改数据源缓存那份），
      侧栏的手动顺序因此不会漏到 summary / 开销那边。
    """
    return treeorder.apply(
        rgroups.decorate(_annotate_translate(_annotate_cfg(get_datasource().list_tree()))))


def tree_view():
    """带分组与手动顺序的树 —— 写接口校验「这一层现在有哪些项」就用它。

    ★ 与侧栏渲染**同一份口径**（同一个 decorate + apply），所以拖动请求送来的顺序
      要么整层对上、要么明确报错，不会出现"按前端旧 DOM 洗掉别人刚改的顺序"。
    ★ 取不到数据源（Miniflux 挂了）直接抛：写操作必须知道自己拿不到树。
    """
    return treeorder.apply(rgroups.decorate(get_datasource().list_tree()))


def _annotate_translate(tree):
    """给 feed 挂 `translate` 状态（开关/已译数/上次失败）。取不到不拖垮树。"""
    try:
        states = translate_core.feed_state_map()
    except Exception as exc:  # noqa: BLE001  建表前/异常时不该拖垮整个树
        log.warning("读取标题翻译状态失败：%s", exc)
        return tree
    for cat in tree:
        for f in cat.feeds:
            f.translate = states.get(f.id)
    return tree


def _annotate_cfg(tree):
    """给每个 feed 打上「分级判据已配置」标记（本源优先，其次分类继承）。

    ★ 口径来自 promptspec.criteria_index（与 build_spec.has_any 同一份实现）——
      视图层不再自己摸 l{i}_text 字段，两边永远不会各判各的。
    """
    try:
        cats, feeds = promptspec.criteria_index()
    except Exception as exc:  # noqa: BLE001  建表前/异常时不该拖垮整个树
        log.warning("读取分级配置标记失败：%s", exc)
        return tree
    for cat in tree:
        for f in cat.feeds:
            if f.id in feeds:
                f.has_cfg, f.cfg_from = True, "本源"
            elif cat.id in cats:
                f.has_cfg, f.cfg_from = True, f"分类「{cat.title}」"
            else:
                f.has_cfg, f.cfg_from = False, ""
    return tree


def _fast_map():
    """{feed_id: {on, minutes, last_fast_at, error}} —— 树上给快速刷新的源打标。"""
    try:
        from core import fastrefresh
        return fastrefresh.state()
    except Exception as exc:  # noqa: BLE001  建表前/异常时不该拖垮列表
        log.warning("读取快速刷新状态失败：%s", exc)
        return {}


def _title_for(sel: str, tree) -> str:
    kind, sid = _scope(sel)
    if kind == "all":
        return "全部文章"
    if kind == "starred":
        return "星标文章"
    if kind == "group":
        g = FeedGroup.objects.filter(id=sid).first()
        return g.title if g else "全部文章"
    for cat in tree:
        if kind == "cat" and cat.id == sid:
            return cat.title
        if kind == "feed":
            for f in cat.feeds:
                if f.id == sid:
                    return f.title
    return "全部文章"


def _load_entries(filters: dict, tree=None, nocache: bool = False):
    """取窗口 → 级别筛选（级别来自 SQLite，未评级按 UNRATED_LEVEL）。

    ★ 级别是**多选集合**：`eff not in filters["levels"]` 就跳过。
      旧的阈值语义（`eff < level`）无法表达「只看 L0」「L1+L3」这类组合。
    ★ 同时算出每条的分级**状态**（llm / manual / fail / default / pending / expired）
      与 hover 依据。级别数字本身分不出"判出来的 L2"和"没跑/失败的 L2"。
    ★ `tree` 由调用方传入（同一请求内只取一次树）：分组范围要靠它判断成员的
      有效性，另取一次既慢又可能拿到"不一致的树"。
    """
    ds = get_datasource()
    if nocache:
        from django.core.cache import cache

        cache.delete("mf:feeds")
        cache.delete("mf:categories")
        cache.delete("mf:tree")

    kind, sid = _scope(filters["sel"])
    if kind == "starred":
        # ★ 星标 = Miniflux 的 bookmark；status / 搜索 / 级别筛选照常叠加
        entries = ds.list_entries(
            starred=True,
            status=filters["status"],
            search=filters["q"],
            limit=settings.ENTRY_WINDOW,
        )
    elif kind == "group":
        # ★ 虚拟分组 = 多个 feed，Miniflux 的 /v1/entries 只吃单个 feed_id
        #   （见 datasource.list_entries_multi 的说明）→ 逐源拉取后在内存归并。
        fids = rgroups.group_feed_ids(sid, tree=tree)
        entries = ds.list_entries_multi(fids, status=filters["status"],
                                        search=filters["q"],
                                        limit=settings.ENTRY_WINDOW) if fids else []
    else:
        entries = ds.list_entries(
            feed_id=sid if kind == "feed" else None,
            category_id=sid if kind == "cat" else None,
            status=filters["status"],
            search=filters["q"],
            limit=settings.ENTRY_WINDOW,
        )

    return _decorate(entries, filters, tree)


def _decorate(entries, filters: dict, tree=None):
    """把条目补成模板要的行（评级/状态/译文/翻译开关），并做级别筛选。

    ★ 抽出来是因为**又多了一个调用点**：深链落在列表窗口外时，
      `/hx/row/<id>` 要把**同一条**渲染成**同一行**。抄一份就等于第二套判据：
      列表里显示 L2、单独取出来显示 L1，两边都对不上还没人会发现。
    ★ 级别筛选也留在这里（而不是留在 `_load_entries`）：单条调用方显式传
      `levels=LEVEL_ALL` —— 人是点名来的，不该被"当前只看 L1"挡掉。
    """
    ids = [e.id for e in entries]
    ratings = {}
    if ids:
        ratings = {r.entry_id: r for r in EntryRating.objects.filter(entry_id__in=ids)}
    # ★ 译文与分级一样：一次 IN 查询，不在模板里逐条查（避免 N+1）。
    #   只返回**已开启翻译的 feed** 下的有效译文；关闭后标题回到原文。
    translations = translate_core.translations_for(ids)
    # 哪些 feed 开着翻译（给文章右键菜单决定是否显示「翻译标题」）
    translate_feeds = {f.id for c in tree for f in c.feeds
                       if getattr(f, "translate", None)} if tree else set()
    # ★ 分级判据标记：与树节点 data-cfg 同一口径（_annotate_cfg 已把分类继承
    #   算进 f.has_cfg）。文章行据此决定右键是否显示「立即判定」——后端对无判据
    #   源走零成本默认路径，给了入口也是假动作（2026-11-20）。
    cfg_feeds = {f.id for c in tree for f in c.feeds
                 if getattr(f, "has_cfg", False)} if tree else set()

    now = timezone.now()
    rows = []
    for e in entries:
        rating = ratings.get(e.id)
        eff = rating.level if rating else settings.UNRATED_LEVEL
        if eff not in filters["levels"]:
            continue
        published = e.published_at or e.created_at
        state = triage_core.entry_rate_state(rating, published, now=now)
        rows.append({"entry": e, "rating": rating, "level": eff,
                     "state": state, "state_meta": triage_core.rate_meta(state),
                     "badge_css": triage_core.badge_css(state, eff),
                     "basis": triage_core.rating_basis(rating, state, eff),
                     "rated": state in triage_core.RATED_STATES,
                     "translation": translations.get(e.id),
                     "feed_translate": e.feed_id in translate_feeds,
                     "has_cfg": e.feed_id in cfg_feeds,
                     "published": published})
    return rows


def _load_one(entry_id: int, filters: dict, tree=None):
    """单条 → 行（供 `hx_row` 用）。取不到条目返回 None。"""
    ds = get_datasource()
    try:
        entry = ds.get_entry(entry_id)
    except Exception as exc:  # noqa: BLE001  Miniflux 对不存在的 id 抛 404
        log.warning("深链取单条失败 entry=%s: %s", entry_id, exc)
        return None
    if entry is None:
        return None
    rows = _decorate([entry], dict(filters, levels=LEVEL_ALL), tree)
    return rows[0] if rows else None


def _sel_exists(kind: str, sid, tree) -> bool:
    """当前选中的范围是否还存在。

    ★ 删除分类/源之后，旧 URL（`?sel=cat:12`）还留在地址栏/书签里；
      直接拿去查 Miniflux 会 400/500。这里统一退回「全部文章」，页面不炸。
    """
    if kind in ("all", "starred"):
        return True
    if kind == "cat":
        return any(c.id == sid for c in tree)
    if kind == "feed":
        return any(f.id == sid for c in tree for f in c.feeds)
    if kind == "group":
        return FeedGroup.objects.filter(id=sid).exists()
    return False


def _tree_context(request, filters) -> dict:
    """渲染树所需的最小上下文 —— **不含条目列表**。

    树端点（/hx/tree、详情的 OOB 刷新）只需要树 + 筛选；此前统一走
    `_entries_context`，每次都白付一次条目 API + EntryRating/译文 IN 查询。
    """
    tree = _tree()
    kind, sid = _scope(filters["sel"])
    if not _sel_exists(kind, sid, tree):
        # ★ 选中的分类/源/分组已不存在 → 退回全部文章（URL 不回写，下次点击树会自然修正）
        filters = dict(filters, sel="all")
        kind, sid = "all", None
    return {
        "tree": tree,
        "filters": filters,
        "qs": _query_without(filters, "sel"),
        "sel_kind": kind,
        "sel_id": sid,
        "total_unread": sum(c.unread for c in tree),
        "fast_feeds": _fast_map(),
    }


def _entries_context(request, filters, nocache=False) -> dict:
    ctx = _tree_context(request, filters)
    rows = _load_entries(ctx["filters"], tree=ctx["tree"], nocache=nocache)
    ctx.update({
        "rows": rows,
        "show_tree": True,
        "title": _title_for(ctx["filters"]["sel"], ctx["tree"]),
        "single_feed": ctx["sel_kind"] == "feed",
        # ★ 多源视图（分组）的口径与"全部文章"不同：每源取 N 条再归并，
        #   列表底部要写出来，否则会被误读成"某源的文章不见了"。
        "multi_source": ctx["sel_kind"] == "group",
        "per_source": settings.MULTI_FEED_PER_SOURCE,
        "level_filters": LEVEL_FILTERS,
        "level_label": _level_label(ctx["filters"]["levels"]),
        "level_all": LEVEL_ALL,
        # ★ 全选 → 空串（URL 干净、也不写进树链接的 qs），否则 "1,3"
        "levels_value": _levels_value(ctx["filters"]["levels"]),
        "mark_choices": MARK_CHOICES,
        "window": settings.ENTRY_WINDOW,
    })
    return ctx


# ------------------------------------------------------------------ 视图
def index(request):
    # 深链点击记录（utm 机制的记录端）：L3 通知 / 日报内引用的链接都带 src= 落到这。
    # 只在带 src 且带 open=（真的定位到某一条）时记；写失败不影响页面打开。
    src = request.GET.get("src") or ""
    open_id = request.GET.get("open") or ""
    if src and open_id.isdigit():
        m = re.match(r"feed:(\d+)", request.GET.get("sel") or "")
        clicktrack.record(src, int(open_id), int(m.group(1)) if m else 0)
    filters = _filters(request)
    ctx = _entries_context(request, filters)
    return render(request, "reader/index.html", ctx)


def hx_entries(request):
    filters = _filters(request)
    nocache = request.GET.get("nocache") == "1"
    ctx = _entries_context(request, filters, nocache=nocache)
    # 直接刷新页面（非 HTMX 请求，例如 push-url 后按 F5）→ 返回整页
    if request.headers.get("HX-Request") != "true":
        return render(request, "reader/index.html", ctx)
    return render(request, "reader/_entries.html", ctx)


def hx_tree(request):
    return render(request, "reader/_tree.html", _tree_context(request, _filters(request)))


def hx_row(request, entry_id):
    """单条的行片段：深链落在**列表窗口外**时，前端用它把那一行补进列表。

    ★ 为什么需要（2026-11-03）：`openFromUrl()` 只在 `#row-<id>` 已存在时才
      展开，而列表窗口只有 `ENTRY_WINDOW` 条。L3 通知点名一条旧文时，旧实现
      是**静默 return** —— 人从通知点进来，看到的是一张普通列表，正文没开，
      也没有任何「为什么没开」的提示。静默失败最贵：它不报错，只是没发生。
    ★ 单条**不受当前级别筛选影响**：点名 ≠ 浏览，人被通知带来是为了看那一条，
      不该被「当前只看 L1」挡掉（挡掉就是这个 bug 的另一种形态）。
      但仍带当前筛选的 qs，展开后的标已读/刷树与列表里点开走同一条链路。
    """
    filters = _filters(request)
    tree = _tree()
    row = _load_one(entry_id, filters, tree)
    if row is None:
        return HttpResponseBadRequest("条目不存在（可能已被清理）")
    # ★ local=1：行本来就在列表里，只是翻译后局部刷新——不能带 pinned=True
    #   （那会凭空多出「单独打开」标记 + is-pinned class）。深链不带此参数，
    #   行为逐字不变。
    local = request.GET.get("local") == "1"
    return render(request, "reader/_row.html", {
        "row": row,
        "qs": _query_without(filters, "sel"),
        "single_feed": _scope(filters["sel"])[0] == "feed",
        "pinned": not local,
        # 徽标文案里要写「只显示最近 N 条」，N 与列表用的是同一个设置
        "window": settings.ENTRY_WINDOW,
    })


@require_POST
def hx_entry_detail(request, entry_id):
    """展开一条的正文（POST：GET 不带「标已读」的写副作用，预取/爬虫/重放不会误触发）。

    ★ 只取 entry + translation + 树上下文（模板里树是 OOB 刷新，展开即标已读
      后未读数要变）—— 不再构建整页的条目列表，那是纯浪费。
    """
    ds = get_datasource()
    try:
        entry = ds.get_entry(entry_id)
    except Exception as exc:  # noqa: BLE001  Miniflux 对不存在的 id 抛 404
        return HttpResponseBadRequest(f"条目不存在：{exc}")
    if entry is None:
        return HttpResponseBadRequest("条目不存在")
    if entry.unread:
        try:
            ds.mark_read([entry.id])
            entry.status = "read"
        except Exception as exc:  # 写回失败也要能看内容
            log.warning("mark_read 失败 entry=%s: %s", entry_id, exc)

    ctx = _tree_context(request, _filters(request))
    ctx["entry"] = entry
    # 只有该 feed 开着翻译才展示；关闭后详情标题也回原文
    ctx["translation"] = translate_core.translation_for_entry(entry.id, entry.feed_id)
    # ★ 「知识库里讲这个的页」（2026-11-16 晚）：本地判据、零模型、只读。
    #   放在 `annotate_html` **之前**——那一步会把 `<mark>` 塞进正文，虽然剥标签
    #   后不影响判断，但让判据吃原始文本更干净（也不会受划线位置影响）。
    #   失败不拖垮详情：正文是主任务，关联是增值。
    ctx["wiki_relate"] = []
    try:
        from core import wiki_relate
        ctx["wiki_relate"] = wiki_relate.relate(entry.content or "", entry.title or "")
    except Exception as exc:  # noqa: BLE001
        log.warning("相关知识库页计算失败 entry=%s: %s", entry_id, exc)
    # ★ 划线/批注组件（wiki 阶段 1）：锚定 (entry, entry_id)，软引用。
    #   失败不拖垮详情（marks 是增值，正文是主任务）。
    #   marks_undisplayed：正文里**落不了位**的标记（锚点失配 / 无锚点）。
    #   底部列表只显示这些；能落位的靠 hover 就地改/删，不需要列表占位。
    ctx["marks_undisplayed"] = []
    try:
        from core import marks
        ctx["marks"] = marks.list_marks("entry", str(entry.id))
        # ★ 正文 inline 高亮回填（选中处原地高亮）
        ctx["entry"].content = marks.annotate_html(
            entry.content, ctx["marks"], ctx["marks_undisplayed"])
    except Exception as exc:  # noqa: BLE001
        log.warning("读取划线/批注失败 entry=%s: %s", entry_id, exc)
        ctx["marks"] = {"highlights": [], "annotations": [], "err": ""}
    return render(request, "reader/_entry_detail.html", ctx)


def _full_fetch_state(entry_id):
    """读"这篇抓过全文没有"。表还没 migrate 时降级为 None（不拖垮抓取本身），
    但记 warning —— 静默跳过会让"重复抓取提醒"悄悄消失。"""
    try:
        return EntryFullFetch.objects.filter(entry_id=entry_id).first()
    except Exception as exc:  # noqa: BLE001  未 migrate / 库损坏
        log.warning("全文抓取状态读取失败 entry=%s: %s", entry_id, exc)
        return None


@require_POST
def hx_fetch_full(request, entry_id):
    """抓取该条目原文全文（Miniflux 的 fetch original content）。

    ★ 覆盖前二次确认（与日报重跑同一契约 need_confirm + confirm=1）：
      ① 抓过全文 → 提示"重复抓取会用源站当前正文覆盖"；
      ② 有划线/批注 → marks.rerun_warning 提示锚点会失效（记录不丢，但掉进
         「未落位」列表）。两类合并成一条 msg，前端只弹一次框。
      ★ 后端权威：confirm=1 是契约的一部分，前端拦不住的改包请求同样要确认。
        msg 由 marks 一处生成，视图只决定"拦不拦 + 拼上重复抓取的提示"。
    ★ 正文不落本地：Miniflux 是真相层，fetch 成功后下次 get_entry 即全文。
    """
    confirmed = request.POST.get("confirm") == "1"
    prev = _full_fetch_state(entry_id)

    if not confirmed:
        parts = []
        if prev is not None:
            parts.append("这篇文章之前抓过全文（%s）。" %
                         timezone.localtime(prev.fetched_at).strftime("%Y-%m-%d %H:%M"))
            parts.append("重复抓取会用源站当前正文覆盖，可能要几十秒。")
        from core import marks
        warn = marks.rerun_warning("entry", str(entry_id), what="这篇文章")
        if warn["msg"]:
            parts.append(warn["msg"])
        if parts:
            return JsonResponse({
                "ok": False,
                "need_confirm": True,
                "msg": "\n\n".join(parts),
                # 老前端把 ok=false 当 error toast，给人话别留"未知原因"
                "error": "抓取全文需要确认（重复抓取 / 划线锚点）",
            })

    ds = get_datasource()
    before_len = 0
    try:
        old = ds.get_entry(entry_id)
        before_len = len((old.content or "") if old else "")
    except Exception as exc:  # noqa: BLE001  读不到就只记 0，不拖垮抓取
        log.warning("抓取全文前读长度失败 entry=%s: %s", entry_id, exc)

    t0 = time.monotonic()
    try:
        ds.fetch_content(entry_id)
    except Exception as exc:  # noqa: BLE001
        from .treeviews import _upstream_error
        return httpkit.err("抓取全文失败：" + _upstream_error(exc), 502)
    fetch_ms = int((time.monotonic() - t0) * 1000)

    after_len = before_len
    try:
        new = ds.get_entry(entry_id)
        if new is not None:
            after_len = len((new.content or ""))
    except Exception as exc:  # noqa: BLE001
        log.warning("抓取全文后读长度失败 entry=%s: %s", entry_id, exc)

    try:
        EntryFullFetch.objects.update_or_create(
            entry_id=entry_id,
            defaults={"content_len_before": before_len,
                      "content_len_after": after_len, "fetch_ms": fetch_ms})
    except Exception as exc:  # noqa: BLE001  状态记账失败不吞掉抓取成功
        log.warning("全文抓取状态落库失败 entry=%s: %s", entry_id, exc)

    return httpkit.ok("已抓取全文", entry_id=entry_id, before=before_len,
                      after=after_len, fetch_ms=fetch_ms)


def hx_mark_read(request):
    ds = get_datasource()
    filters = _filters(request)
    tree = _tree()
    try:
        days = int(request.POST.get("days", "0"))
    except ValueError:
        return HttpResponseBadRequest("days 非法")

    kind, sid = _scope(filters["sel"])
    feed_id = sid if kind == "feed" else None
    category_id = sid if kind == "cat" else None
    # ★ 分组范围必须**逐源**处理：Miniflux 没有"按 feed 集合标记"的端点。
    #   ★★ 尤其注意别让它退化：分组时 feed_id/category_id 都是 None，
    #      若照旧走下去就变成"标记全部已读"——那是不可逆的误操作，这里显式拦住。
    group_feeds = rgroups.group_feed_ids(sid, tree=tree) if kind == "group" else []

    try:
        if kind == "starred":
            # ★ 星标范围只标记「星标里的未读」；绝不能因为 feed/category 都是 None
            #   就退化成「全部已读」。
            if days <= 0:
                ds.mark_read([e.id for e in ds.iter_unread(starred=True)])
            else:
                ds.mark_read_before(days=days, starred=True)
        elif kind == "group":
            for fid in group_feeds:
                if days <= 0:
                    ds.mark_scope_read(feed_id=fid)
                else:
                    ds.mark_read_before(days=days, feed_id=fid)
        elif days <= 0:
            # 全量：必须分页遍历（/v1/entries 单次 limit 上限 1000）
            ids = [e.id for e in ds.iter_unread(feed_id=feed_id, category_id=category_id)]
            ds.mark_read(ids)
        else:
            ds.mark_read_before(days=days, feed_id=feed_id, category_id=category_id)
    except Exception as exc:
        log.warning("mark_read 失败: %s", exc)
        return HttpResponseBadRequest(f"标记已读失败：{exc}")

    ctx = _entries_context(request, filters)
    return render(request, "reader/_entries.html", ctx)


@require_POST
def hx_mark_unread(request, entry_id):
    """把一篇**已读**条目退回未读（文章右键菜单）。

    ★ 谛听点开即标已读，误点开原本只能去 Miniflux 网页改 —— 这个端点是可逆出口。
      Miniflux 是已读状态的真相层，所以只写它，不在本地另存一份"假未读"。
    ★ 返回 JSON（不是整页 HTML）：app.js 拿到 ok 后自己 reload，失败时把上游人话
      弹给用户，绝不静默成功（写回失败但界面像成功 = 最贵的错法）。
    """
    ds = get_datasource()
    try:
        # 不存在的 id 要显式报错，不能当成"标记成功"（有些数据源返回 None、
        # Miniflux 直接抛 404，两种形状都要接住）。
        if ds.get_entry(entry_id) is None:
            return httpkit.err("条目不存在")
        ds.mark_unread([entry_id])
    except Exception as exc:  # noqa: BLE001  Miniflux 对不存在的 id 抛 404
        log.warning("mark_unread 失败 entry=%s: %s", entry_id, exc)
        return httpkit.err("标记为未读失败：%s" % exc)
    return httpkit.ok("已标记为未读", entry_id=entry_id)


def _record_star(entry, starred: bool) -> None:
    """记一条加星事件（本地时间戳）。**失败绝不影响星标本身。**

    ★ 为什么必须记：Miniflux 的 `entries` 只有 `starred` 布尔、**没有加星时间**
      （`bookmarks` 表不存在，`changed_at` 是正文变更时间）。周报的「本周 star」
      段完全依赖这张表，不记就只能靠 `inferred` 按**发布时间**补记 ——
      而发布时间落在周报窗口外的老文章（本周给两周前的文章点星）**永远补不上**。
      实测（2026-10-29）：Miniflux 已加星 78 篇，本地只记到 32 篇，漏 46 篇。
    """
    try:
        StarEvent.objects.create(
            entry_id=int(entry.id), feed_id=int(entry.feed_id or 0),
            starred=bool(starred), ts=timezone.now(), source="reader")
    except Exception as exc:  # noqa: BLE001  记账失败不该让星标操作报错
        log.warning("加星事件未记录 entry=%s：%s", getattr(entry, "id", "?"), exc)


def hx_star(request, entry_id):
    """切换星标。⚠ Miniflux 的 toggle-star 是切换语义，必须以当前状态为准。"""
    ds = get_datasource()
    entry = ds.get_entry(entry_id)
    if entry is None:
        return HttpResponseBadRequest("条目不存在")
    want = not entry.starred
    try:
        ds.toggle_star(entry_id, want)
        starred = want
    except Exception as exc:
        log.warning("toggle_star 失败 entry=%s: %s", entry_id, exc)
        starred = entry.starred
    else:
        _record_star(entry, starred)      # 只有真正改成 `want` 才记账
    return render(request, "reader/_star.html", {"entry_id": entry_id, "starred": starred})


def icon(request, feed_id):
    got = get_datasource().icon(feed_id)
    if not got:
        return HttpResponse(status=404)
    ctype, blob = got
    resp = HttpResponse(blob, content_type=ctype)
    resp["Cache-Control"] = "public, max-age=86400"
    return resp


# ------------------------------------------------------------------ 立即刷新（工具栏 / 右键）
# ★ 单次刷新的源上限：防止误点「全部文章」把几十个源串成一条长队列。
#   超出部分不静默截断，而是把 `skipped` 返回给前端。
REFRESH_MAX = 60


def _tree_feeds(tree):
    return [f for cat in tree for f in cat.feeds]


def _feeds_in_scope(kind, sid, tree) -> list:
    """当前 `sel` 覆盖的 feed id（顺序与树一致）。"""
    if kind == "feed":
        return [f.id for f in _tree_feeds(tree) if f.id == sid]
    if kind == "cat":
        return [f.id for cat in tree if cat.id == sid for f in cat.feeds]
    if kind == "group":
        # ★ 分组必须走 readergroups 的有效性口径（跨分类残留不算）
        return rgroups.group_feed_ids(sid, tree=tree)
    return [f.id for f in _tree_feeds(tree)]


@require_POST
def hx_refresh(request):
    """启动一批源刷新，**立刻返回**；真正的刷新在后台线程里串行跑。

    参数：
      sel    —— 当前列表范围；`feeds` 为空时按它推导要刷的源
      feeds  —— 逗号分隔的 feed id（前端传「当前列表里出现的源」）。
                只接受当前树里存在的 id：Miniflux 对不存在的 feed 会 404，
                在这里过滤掉比让它抛异常干净。

    返回的 `feeds` 是**正在跑的这一批**（可能是上一次请求启动的任务）。
    """
    filters = _filters(request)
    try:
        tree = _tree()
    except Exception as exc:  # noqa: BLE001  Miniflux 不可用时给 JSON，别让前端吃 500
        log.warning("刷新前取订阅树失败：%s", exc)
        return JsonResponse({"ok": False, "error": f"取订阅树失败：{exc}"}, status=502)
    feeds = _tree_feeds(tree)
    known = {f.id for f in feeds}
    titles = {f.id: (f.title or "") for f in feeds}

    ids = []
    raw = (request.POST.get("feeds") or "").strip()
    for part in raw.split(","):
        part = part.strip()
        if not part.isdigit():
            continue
        fid = int(part)
        if fid in known and fid not in ids:
            ids.append(fid)

    if not ids:
        kind, sid = _scope(filters["sel"])
        ids = [i for i in _feeds_in_scope(kind, sid, tree) if i in known]

    if not ids:
        return JsonResponse({"ok": False, "error": "当前范围没有可刷新的源"}, status=400)

    skipped = max(0, len(ids) - REFRESH_MAX)
    ids = ids[:REFRESH_MAX]
    started, state = refreshjob.start(ids, titles)
    return JsonResponse({
        "ok": True,
        "started": started,          # False = 已有批次在跑，前端跟随它
        "total": state["total"],
        "feeds": state["feeds"],
        "skipped": skipped,
    })


def hx_refresh_status(request):
    """刷新批次状态（前端轮询）。"""
    st = refreshjob.status()
    st.pop("titles", None)          # 内部用于拼错误文案，不必下发
    return JsonResponse(st)

