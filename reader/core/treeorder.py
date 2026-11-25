"""侧栏树的**手动顺序**（拖动重排）：读、写、清理都收在这里。

需求来源（2026-11-24）：分类、目录、feed 三层都要能拖动重排。
默认顺序本来就有（`miniflux.list_tree()` 按名称 / 未读优先，目录按 `FeedGroup.sort`
即创建序），这里只叠加"人手动排过的那部分"。

★★ 三条约束（改这个模块前先读）

1. **只排顺序，不改归属**。Miniflux 仍是分类 / feed 的真相层，跨分类移动照旧拒绝
   （代价见 `core/readergroups.py` 模块头）。
2. **不污染共享树对象**。`apply()` 返回**新的**顶层列表，`cat.feeds` 一个元素都不动
   —— 那个 list 是数据源缓存 `mf:tree` 里的对象，改了顺序会漏到 summary / 开销那边。
   渲染「分类根下裸着的那一段」另挂 `cat.root_feeds`。
3. **手动优先，没排过的排末尾**（`_merge`）。没行的项保持它进来的默认顺序跟在后面 ⇒
   新建 / 外部改归属进来的项自动落到该层末尾，不用补 sort；「恢复默认顺序」= 删掉本层
   的行，一句 SQL，不需要额外存"默认长什么样"。

★ 一层的键是 `(kind, scope)`：`cat`→空串（分类只有全局一个列表）、
  `grp`→`cat:<id>`、`feed`→`cat:<id>`（分类根）或 `grp:<id>`（目录内）。
"""
import logging

from django.db import transaction

from core.models import TreeOrder

log = logging.getLogger(__name__)

KIND_CAT = "cat"
KIND_GRP = "grp"
KIND_FEED = "feed"
KINDS = (KIND_CAT, KIND_GRP, KIND_FEED)

MAX_IDS = 500          # 一层的项数上限（防构造出来的超长请求）


# ------------------------------------------------------------------ scope 编码
def cat_scope(category_id) -> str:
    return f"cat:{int(category_id)}"


def grp_scope(group_id) -> str:
    return f"grp:{int(group_id)}"


def feed_scope(category_id, group_id=None) -> str:
    """feed 所在兄弟列表的键：在目录里就是目录，否则是分类根。"""
    return grp_scope(group_id) if group_id else cat_scope(category_id)


def norm_scope(kind, scope) -> str:
    """`kind="cat"` 的层永远是全局那一个 ⇒ scope 一律归零，前端传什么都不认。"""
    return "" if kind == KIND_CAT else (scope or "").strip()


# ------------------------------------------------------------------ 读取
def ranks(kind, scope) -> dict:
    """某层的 `{ref_id: sort}`。"""
    return dict(TreeOrder.objects.filter(kind=kind, scope=norm_scope(kind, scope))
                .values_list("ref_id", "sort"))


def _index() -> dict:
    """`{(kind, scope): {ref_id: sort}}` —— 一次取全表（手动顺序的行本来就少）。"""
    idx = {}
    for kind, scope, ref_id, sort in TreeOrder.objects.values_list(
            "kind", "scope", "ref_id", "sort"):
        idx.setdefault((kind, scope), {})[ref_id] = sort
    return idx


def root_feeds(cat):
    """分类根下裸着的 feed（未分组的）。★ `cat.feeds` 是全量，这里只是视图切片。"""
    return [f for f in cat.feeds if not f.group_id]


def _merge(items, rank_map, get_id):
    """手动排过的按 `(sort, id)` 在前，没排过的**保持进来的顺序**跟在后面。"""
    head = [n for n in items if get_id(n) in rank_map]
    head.sort(key=lambda n: (rank_map[get_id(n)], get_id(n)))
    return head + [n for n in items if get_id(n) not in rank_map]


def apply(tree):
    """给**已 decorate 的树**按手动顺序重排，并补上渲染要用的视图字段。

    写在这些字段上（都由视图层负责，数据源不填）：
      - 返回值：分类按手动顺序排好的**新列表**
      - `cat.root_feeds`：分类根下那段 feed 的展示顺序（`cat.feeds` 不动）
      - `cat.groups` / `grp.feeds`：目录顺序 / 组内 feed 顺序
      - `layer_ordered`：本行所在兄弟层有没有手动顺序（右键「恢复默认顺序」只在这
        一行真的排过时才给，否则是个假动作）
    ★ 幂等：每次都从库里整体重读，重复调用结果一致。
    """
    idx = _index()
    cat_ranks = idx.get((KIND_CAT, ""), {})
    out = _merge(tree, cat_ranks, lambda c: c.id)

    for cat in out:
        cat.layer_ordered = bool(cat_ranks)
        grp_ranks = idx.get((KIND_GRP, cat_scope(cat.id)), {})
        cat.groups = _merge(cat.groups, grp_ranks, lambda g: g.id)
        for g in cat.groups:
            g.layer_ordered = bool(grp_ranks)
            feed_ranks = idx.get((KIND_FEED, grp_scope(g.id)), {})
            g.feeds = _merge(g.feeds, feed_ranks, lambda f: f.id)
            _mark(g.feeds, feed_ranks)
        root_ranks = idx.get((KIND_FEED, cat_scope(cat.id)), {})
        cat.root_feeds = _merge(root_feeds(cat), root_ranks, lambda f: f.id)
        _mark(cat.root_feeds, root_ranks)
    return out


def _mark(feeds, rank_map):
    for f in feeds:
        f.layer_ordered = bool(rank_map)


# ------------------------------------------------------------------ 一层的当前内容
def layer(kind, scope, tree) -> list:
    """该层**当前**的项 id（服务端真相）—— 重排请求必须与它完全一致才落库。

    找不到这一层 → ValueError（分类 / 目录已被删，或 scope 拼错）。
    """
    scope = norm_scope(kind, scope)
    if kind == KIND_CAT:
        return [c.id for c in tree]

    if kind == KIND_GRP:
        if not scope.startswith("cat:"):
            raise ValueError("目录层的 scope 应为 cat:<id>")
        cid = int(scope.split(":", 1)[1])
        cat = next((c for c in tree if c.id == cid), None)
        if cat is None:
            raise ValueError("这个分类不在当前树里（可能已被删除）")
        return [g.id for g in cat.groups]

    if kind == KIND_FEED:
        if scope.startswith("grp:"):
            gid = int(scope.split(":", 1)[1])
            for cat in tree:
                g = next((x for x in cat.groups if x.id == gid), None)
                if g is not None:
                    return [f.id for f in g.feeds]
            raise ValueError("这个目录不存在（可能已被删除）")
        if scope.startswith("cat:"):
            cid = int(scope.split(":", 1)[1])
            cat = next((c for c in tree if c.id == cid), None)
            if cat is None:
                raise ValueError("这个分类不在当前树里（可能已被删除）")
            return [f.id for f in root_feeds(cat)]
        raise ValueError("feed 层的 scope 应为 cat:<id> 或 grp:<id>")

    raise ValueError(f"未知的层类型：{kind}")


# ------------------------------------------------------------------ 写入
def set_order(kind, scope, ids, tree) -> int:
    """把一层的顺序整体落库，返回写入项数。

    ★ 前端送的是**整层**（拖动后 DOM 里的全部兄弟），所以要求与 `layer()` 完全一致：
      少一项 / 多一项 / 有重复都拒。否则一个过期的页面能把别人刚改过的顺序洗掉。
    ★ 先删后插、sort 稠密 1..n：行数本来就少，换来的是"插到中间"不用维护空洞，
      也不用管 sort 用完。
    """
    if kind not in KINDS:
        raise ValueError(f"未知的层类型：{kind}")
    scope = norm_scope(kind, scope)
    want = [int(i) for i in ids]
    if not want:
        raise ValueError("顺序是空的")
    if len(want) > MAX_IDS:
        raise ValueError("顺序过长")
    if len(set(want)) != len(want):
        raise ValueError("顺序里有重复项")

    cur = set(layer(kind, scope, tree))
    if cur != set(want):
        raise ValueError("这一层的内容已经变了（新增 / 删除 / 换过归属），请刷新页面重试")

    rows = [TreeOrder(kind=kind, scope=scope, ref_id=rid, sort=i + 1)
            for i, rid in enumerate(want)]
    with transaction.atomic():
        TreeOrder.objects.filter(kind=kind, scope=scope).delete()
        TreeOrder.objects.bulk_create(rows)
    log.info("重排 %s %s：%s 项", kind, scope or "-", len(rows))
    return len(rows)


def reset(kind, scope) -> int:
    """删掉一层的手动顺序 ⇒ 回到默认规则（分类按名称、目录按创建序、feed 未读优先）。"""
    if kind not in KINDS:
        raise ValueError(f"未知的层类型：{kind}")
    n = TreeOrder.objects.filter(kind=kind, scope=norm_scope(kind, scope)).delete()[0]
    log.info("恢复默认顺序 %s %s：%s 项", kind, norm_scope(kind, scope) or "-", n)
    return n


# ------------------------------------------------------------------ 清理
def drop_ref(kind, ref_id) -> int:
    """某一项换了层 / 没了：清掉它在**所有层**里的手动顺序。

    ★ 归属在 Miniflux 侧，本地不可能知道它"该去哪一层"，所以宁可删掉这一项的顺序
      （它在新层里按默认规则排末尾）也不要把错的位置留下。
    """
    return TreeOrder.objects.filter(kind=kind, ref_id=int(ref_id)).delete()[0]


def drop_scope(kind, scope) -> int:
    """整层没了（分类 / 目录被删）：清掉这一层的手动顺序。"""
    return TreeOrder.objects.filter(kind=kind, scope=norm_scope(kind, scope)).delete()[0]
