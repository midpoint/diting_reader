"""分类与 feed 之间的**虚拟分组层**（阅读页树专用）。

需求来源（2026-10-16）：AI 分类下 26 个源，树一屏显示不全 → 在「分类」和「feed」
之间加一层可折叠的分组；分组可汇总未读、可点开看组内文章、feed 可在组间移动。

★★ 三条硬约束（改这个模块前先读）

1. **纯视图层**。分组不进分级判据（判据的载体是分类 / feed）、不进周期总结、
   不进开销统计（这也是当初没直接"把 AI 拆成三个真分类"的原因 —— 分类有语义，
   进总结分节与开销分类分布）。
   实现上刻意做成「**叠加索引**」而不是「替换树」：
     - `CategoryNode.feeds` 保持**全量不变**（`summary.py` / `costreport.py` 只读它）
     - 另挂 `CategoryNode.groups`，组内装的是同一批 `FeedNode` 的**引用**
   ⇒ 分类未读数（`sum(f.unread for f in self.feeds)`）天然正确，两个下游模块零改动。

2. **本期不跨分类移动**。feed 换分组只允许在**同一分类内**。跨分类 = 改 Miniflux
   的归属，会连带三件事：① 清零 `parsing_error_count`（`updateFeedHandler` 里
   那句 `ResetErrorCounter()`，会把"正在解析失败的源"洗白）② 该源全部历史条目改属
   新分类（entries 表没有 category 列）③ 判据继承立刻改变（`triage.py` 按当前分类
   实时派生）。那是独立的一次改动，不该混在"看树方便"里。

3. **成员有效性以"feed 当前分类"为准**：`group.category_id == feed 现在所在分类`。
   不符的成员**不渲染**（视为未分组）—— 这样在 Miniflux 原生 UI 里直接改分类，
   也不会在本地点出幽灵分组；残留用 `cleanup_orphans()` 清。
"""
import logging
from dataclasses import dataclass, field

from django.db import transaction

from core import groupbackup
from core import treeorder
from core.models import FeedGroup, FeedGroupMember

log = logging.getLogger(__name__)

MAX_TITLE = 32          # UI 侧 prompt 也按这个限（库里 64，留余量）


@dataclass
class GroupView:
    """树上的分组节点。★ 由 `decorate()` 现造，**不落库、不可跨请求复用**。"""

    id: int
    title: str
    category_id: int
    feeds: list = field(default_factory=list)
    # ★ 目录这一层（本分类内的目录之间）有没有被手动排过序，由 `treeorder.apply` 挂。
    layer_ordered: bool = False

    @property
    def unread(self) -> int:
        return sum(f.unread for f in self.feeds)

    @property
    def has_error(self) -> bool:
        return any(f.has_error for f in self.feeds)

    @property
    def error_feeds(self) -> list:
        return [f for f in self.feeds if f.has_error]


# ------------------------------------------------------------------ 读取
def feed_index(tree):
    """从**当前树**推导 `({feed_id: FeedNode}, {feed_id: category_id})`。

    ★ 一切有效性判断都以这个为准（树 = Miniflux 的现状），而不是本地 member 表。
    """
    nodes, cats = {}, {}
    for cat in tree:
        for f in cat.feeds:
            nodes[f.id] = f
            cats[f.id] = cat.id
    return nodes, cats


def decorate(tree):
    """给树挂上分组（就地写 `cat.groups` / `feed.group_id`，返回同一棵树）。
    - **幂等**：每次都整体重挂，重复调用结果一致；
    - `cat.feeds` **一个元素都不增删**（这条是给总结 / 开销的回归保证）。
    ★ 这里只管**默认**顺序（目录按创建序、组内 feed 按未读优先）。拖动重排出来的
      手动顺序由 `core/treeorder.apply()` 在这一层之上叠加，两者别混在一起改。
    """
    nodes, cats = feed_index(tree)
    buckets = {cat.id: {} for cat in tree}
    for g in FeedGroup.objects.all().order_by("sort", "id"):
        if g.category_id in buckets:
            buckets[g.category_id][g.id] = GroupView(g.id, g.title, g.category_id)

    if nodes:
        for m in FeedGroupMember.objects.filter(feed_id__in=list(nodes)):
            bucket = buckets.get(cats.get(m.feed_id))
            gv = bucket.get(m.group_id) if bucket else None
            if gv is None:
                continue                      # 组不存在 / 跨分类残留 → 不渲染
            node = nodes[m.feed_id]
            node.group_id = m.group_id
            gv.feeds.append(node)

    for cat in tree:
        gvs = buckets.get(cat.id, {})
        for gv in gvs.values():
            gv.feeds.sort(key=lambda x: (-x.unread, (x.title or "").lower()))
        cat.groups = list(gvs.values())       # 已按 (sort, id) 有序（queryset 排过）
        # ★ 「分类根下裸着的那一段」在这里就切出来（默认顺序）：这样
        #   **decorate 过的树永远渲染得出来**，`treeorder.apply()` 忘了调只是
        #   没有手动顺序，不会让一排源凭空消失。
        cat.root_feeds = treeorder.root_feeds(cat)
    return tree


def group_of(feed_id, tree=None):
    """feed 当前所在的**有效**分组 id（0 = 未分组）。"""
    tree = tree if tree is not None else _tree()
    nodes, cats = feed_index(tree)
    cat_id = cats.get(int(feed_id))
    if cat_id is None:
        return 0
    m = FeedGroupMember.objects.filter(feed_id=int(feed_id)).first()
    if m is None:
        return 0
    g = FeedGroup.objects.filter(id=m.group_id, category_id=cat_id).first()
    return g.id if g else 0


def group_feed_ids(group_id, tree=None):
    """组内**有效**的 feed id（已过滤跨分类残留与已删除的源）。

    ★ 这是「成员集合」口径（`FeedGroupMember.sort` = 入组序），给刷组 / 取标题用。
      **树上的展示顺序不是它** —— 那一份在 `core/treeorder.apply()`（手动优先 +
      未读优先兜底），别把两边当成同一个顺序。
    """
    tree = tree if tree is not None else _tree()
    _nodes, cats = feed_index(tree)
    g = FeedGroup.objects.filter(id=int(group_id)).first()
    if g is None:
        return []
    return [i for i in FeedGroupMember.objects.filter(group_id=g.id)
            .order_by("sort", "id").values_list("feed_id", flat=True)
            if cats.get(i) == g.category_id]


def group_summary() -> list:
    """给脚本 / 排查用的只读快照：`[{id, category_id, title, members}, ...]`。"""
    counts = {}
    for fid, gid in FeedGroupMember.objects.values_list("feed_id", "group_id"):
        counts[gid] = counts.get(gid, 0) + 1
    return [{"id": g.id, "category_id": g.category_id, "title": g.title,
             "sort": g.sort, "members": counts.get(g.id, 0)}
            for g in FeedGroup.objects.all().order_by("category_id", "sort", "id")]


# ------------------------------------------------------------------ 写入
def _clean_title(title) -> str:
    """折叠空白 + 截断。空串由调用方当错误处理（不在这里抛）。"""
    return " ".join(str(title or "").split())[:MAX_TITLE]


def _next_sort(category_id) -> int:
    last = FeedGroup.objects.filter(category_id=int(category_id)) \
        .order_by("-sort").values_list("sort", flat=True).first()
    return int(last or 0) + 1


def create_group(category_id, title):
    t = _clean_title(title)
    if not t:
        raise ValueError("分组名不能为空")
    cat_id = int(category_id)
    return FeedGroup.objects.create(category_id=cat_id, title=t, sort=_next_sort(cat_id))


def rename_group(group_id, title):
    t = _clean_title(title)
    if not t:
        raise ValueError("分组名不能为空")
    n = FeedGroup.objects.filter(id=int(group_id)).update(title=t)
    if not n:
        raise ValueError("分组不存在")
    return t


def delete_group(group_id):
    """删**组**不删 feed：组内成员关系一并清掉 ⇒ 这些 feed 回到分类根。

    ★ 动手前留一份快照（分组数据没有第二真相层，删错了造不回来）。
    """
    with transaction.atomic():
        groupbackup.snapshot(f"delete_group:{group_id}")
        FeedGroupMember.objects.filter(group_id=int(group_id)).delete()
        # 顺序也是这个目录的数据：目录没了 / 组里的源回到分类根，这些行都成孤儿
        treeorder.drop_ref(treeorder.KIND_GRP, group_id)
        treeorder.drop_scope(treeorder.KIND_FEED, treeorder.grp_scope(group_id))
        return FeedGroup.objects.filter(id=int(group_id)).delete()[0]


def delete_category_local(category_id) -> int:
    """分类在 Miniflux 侧已删之后，清掉它的**全部本地数据**（虚拟分组 + 分类判据）。

    ★ 域规则收在分组层而不是调用方：分类一旦删除，其分组与判据都是孤儿
      （判据继承 / 分组渲染都以「feed 当前分类」为准）。将来分类下面再加
      本地表（如分类级翻译配置），只需要改这里，不用挨个改调用方。
    ★ 快照由 delete_group 内部留；返回清掉的分组数。异常抛给调用方 ——
      Miniflux 侧删除已完成，清不掉只该 warning 提示，不该报成失败。
    """
    from core.models import CategoryTriageConfig

    cid = int(category_id)
    gids = list(FeedGroup.objects.filter(category_id=cid)
                .values_list("id", flat=True))
    for gid in gids:
        delete_group(gid)
    CategoryTriageConfig.objects.filter(category_id=cid).delete()
    # 分类自己的排序 + 它下面「目录层」「分类根 feed 层」的排序（组内那层已由
    # delete_group 清掉）—— 不留孤儿行，否则将来复用 id 时会莫名继承旧位置。
    treeorder.drop_ref(treeorder.KIND_CAT, cid)
    treeorder.drop_scope(treeorder.KIND_GRP, treeorder.cat_scope(cid))
    treeorder.drop_scope(treeorder.KIND_FEED, treeorder.cat_scope(cid))
    return len(gids)


def delete_feed_local(feed_id) -> dict:
    """feed 在 Miniflux 侧已删之后，清掉它的**全部本地数据**。

    ★ 对称于 `delete_category_local`：域规则收在分组层，调用方（treeviews）
      只管"删成功了就调一下"。将来再加 feed 级本地表，只改这里。
    ★ **故意保留的两类**：
      - `TriagePrompt` —— 它是审计留档（"全留做审计，不可人工编辑"），
        源没了也不该抹掉"当时拿什么 prompt 判的"；
      - `LlmUsage` —— 成本总账，删了成本页会对不上（钱是真花过的）。
    ★ 异常抛给调用方：Miniflux 侧已删成功，本地清不掉只该 warning，
      不该把整个删除报成失败。

    返回 {"表名": 删除条数}。
    """
    from core.models import (EntryRating, EntryTranslation, FeedGroupMember,
                             FeedOverride, FeedTriageConfig, FeedTranslation)

    fid = int(feed_id)
    plan = {
        "triage_config": FeedTriageConfig.objects.filter(feed_id=fid),
        "group_member": FeedGroupMember.objects.filter(feed_id=fid),
        "override": FeedOverride.objects.filter(feed_id=fid),
        "translation": FeedTranslation.objects.filter(feed_id=fid),
        "entry_rating": EntryRating.objects.filter(feed_id=fid),
        "entry_translation": EntryTranslation.objects.filter(feed_id=fid),
    }
    # ★ 先快照再删：FeedGroupMember 是本地唯一的归属数据，删了就没了
    if any(qs.exists() for qs in plan.values()):
        groupbackup.snapshot(f"delete_feed:{fid}")

    out = {}
    for name, qs in plan.items():
        n = qs.count()
        if n:
            qs.delete()
        out[name] = n
    # 拖动排出来的位置：只删**这一个 feed** 的行，同层其它项的位置原样保留
    # （所以不必进上面的快照 —— 那快照护的是"归属"，不是顺序）。
    out["tree_order"] = treeorder.drop_ref(treeorder.KIND_FEED, fid)
    log.info("删除 feed %s 的本地数据：%s", fid, out)
    return out


def move_feed(feed_id, group_id, tree=None):
    """把 feed 移入分组；`group_id` 为空/0 = **移出到分类根**。

    ★ 只允许**同分类内**移动。跨分类（改 Miniflux 归属）本期不支持，
      传错一律拒绝而不是"顺手"做掉 —— 那个操作会清零解析错误计数（见模块头）。
    ★ 换层 ⇒ 丢位置：手动顺序记的是「在这一层里排第几」，换到别的层之后那个数字
      没有意义，所以删掉（它在新层按默认规则排末尾）。本来就在同一层的调用
      什么都不写，顺序自然留着。
    """
    tree = tree if tree is not None else _tree()
    _nodes, cats = feed_index(tree)
    fid = int(feed_id)
    cat_id = cats.get(fid)
    if cat_id is None:
        raise ValueError(f"feed {fid} 不在当前树里（可能已被删除）")

    if group_id in (None, "", 0, "0"):
        if not FeedGroupMember.objects.filter(feed_id=fid).exists():
            return None                          # 本来就在分类根：归属、顺序都不动
        groupbackup.snapshot(f"move_feed_out:{fid}")
        FeedGroupMember.objects.filter(feed_id=fid).delete()
        # 换层 ⇒ 它在旧层里的位置没有意义了（新层按默认规则排末尾）
        treeorder.drop_ref(treeorder.KIND_FEED, fid)
        return None

    g = FeedGroup.objects.filter(id=int(group_id)).first()
    if g is None:
        raise ValueError("分组不存在")
    if g.category_id != cat_id:
        raise ValueError("只能在本分类内移动（跨分类移动本期不支持）")
    current = FeedGroupMember.objects.filter(feed_id=fid).values_list(
        "group_id", flat=True).first()
    if current == g.id:
        return g                                 # 本来就在这一层：归属、顺序都不动
    FeedGroupMember.objects.update_or_create(feed_id=fid, defaults={"group_id": g.id})
    treeorder.drop_ref(treeorder.KIND_FEED, fid)
    return g


def cleanup_orphans(tree=None, *, scope_group_ids=None, purge_missing=False) -> int:
    """清无效成员：组已删 / feed 已不在该组的分类里（含源被删、分类被外部改过）。

    ★★ 安全契约（2026-10-16 数据事故后重写，改这个函数前必读）
      三类"可疑成员"必须区别对待：

      1. **组不存在**（`group_id not in gcat`）→ 一定删。
         本地信息自洽，判定不依赖 Miniflux。
      2. **feed 在树里、但分类与组不符** → 一定删。这是真正的跨分类残留。
      3. **feed 不在树里**（`cats.get(feed_id) is None`）→ **默认保留**。
         因为"不在树里"有两种完全不同的成因：① 源真被删了；② 树不完整/取不到
         （Miniflux 抽风、只拿到部分分类）。**单次判断无法区分，就不能定罪。**
         要清这类残留，必须显式传 `purge_missing=True`（运维手动维护时用）。

      另：树里**一个 feed 都没有** → 判定为"取数失败"，直接返回 0 并告警。
      否则空树会让"所有成员"都落进第 3 类而被一次性删光。

    ★ `scope_group_ids`：只处理这些组下的成员。**测试脚本必须传它**，
      否则测试用的"假树"会把真实成员全判成孤立（这正是事故的成因）。

    返回清理条数。★ 永远只删**成员关系**，不碰 feed 本身。
    """
    tree = tree if tree is not None else _tree()
    nodes, cats = feed_index(tree)
    if not cats:
        log.warning("cleanup_orphans：树里没有任何 feed，判定为取数失败，拒绝清理"
                    "（空树会把全部成员误判为孤儿）")
        return 0

    gcat = dict(FeedGroup.objects.values_list("id", "category_id"))
    qs = FeedGroupMember.objects.all()
    if scope_group_ids is not None:
        qs = qs.filter(group_id__in=[int(x) for x in scope_group_ids])

    dead, kept_missing = [], 0
    for m in qs:
        if m.group_id not in gcat:
            dead.append(m.id)                                   # 1. 组没了
        elif cats.get(m.feed_id) != gcat[m.group_id]:
            if cats.get(m.feed_id) is None and not purge_missing:
                kept_missing += 1                               # 3. 拿不准 → 留着
            else:
                dead.append(m.id)                               # 2. 真跨分类残留
    if dead:
        groupbackup.snapshot(f"cleanup_orphans:{len(dead)}")
        FeedGroupMember.objects.filter(id__in=dead).delete()
        log.info("cleanup_orphans：清理 %d 条失效归属（范围=%s）",
                 len(dead), "全部" if scope_group_ids is None else f"{len(scope_group_ids)} 个组")
    if kept_missing:
        log.info("cleanup_orphans：保留 %d 条『feed 不在当前树里』的成员"
                 "（可能是树不完整，需 purge_missing=True 才清）", kept_missing)
    return len(dead)


def orphan_count(tree=None, *, scope_group_ids=None) -> int:
    """**只读**统计孤儿数（口径与 `cleanup_orphans` 的 1、2 类一致）。"""
    tree = tree if tree is not None else _tree()
    _nodes, cats = feed_index(tree)
    gcat = dict(FeedGroup.objects.values_list("id", "category_id"))
    qs = FeedGroupMember.objects.all()
    if scope_group_ids is not None:
        qs = qs.filter(group_id__in=[int(x) for x in scope_group_ids])
    return sum(1 for m in qs
               if m.group_id not in gcat
               or (cats.get(m.feed_id) is not None
                   and cats.get(m.feed_id) != gcat[m.group_id]))


def _tree():
    from core.datasource import get_datasource

    return get_datasource().list_tree()
