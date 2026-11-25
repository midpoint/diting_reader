"""树索引的共享工具：一次取树，喂饱所有视图。

此前「在树里找 feed 的分类 / 标题」被 4 个 app 各写一遍（triage 视图、
reader 视图、treeviews、costs 视图），一个 config_save 请求隐式拉 3-4 次树
（全靠数据源 20s 缓存兜底）。规则收敛在这里：

  · `feed_index()` —— {feed_id: FeedNode} + {feed_id: category_id}；
  · `title_maps()` —— feed 标题表 / 分类标题表 / feed→分类（开销页等只要名字的）；
  · `cat_of` / `feed_title` / `category_title` —— 单点查找（传入 tree 不再取）。

★ 想要「安全失败」（Miniflux 挂了返回空映射而不是抛）用 `title_maps(safe=True)`；
  写操作路径（treeviews / groups）**不要**用 safe 版 —— 取不到树就该拒绝写。
"""
from core.datasource import get_datasource
from typing import Optional


def tree():
    """当前树（数据源层自带 20s 缓存；同请求内请传 tree 复用，别反复调）。"""
    return get_datasource().list_tree()


def feed_index(tree=None):
    """({feed_id: FeedNode}, {feed_id: category_id})。`tree` 缺省时取当前树。"""
    if tree is None:
        tree = get_datasource().list_tree()
    nodes, cats = {}, {}
    for cat in tree:
        for f in cat.feeds:
            nodes[f.id] = f
            cats[f.id] = cat.id
    return nodes, cats


def title_maps(tree=None, *, safe: bool = False):
    """(feed 标题表, 分类标题表, feed→分类)。

    safe=True：取树失败返回三个空 dict（开销页这类只读展示不该被打挂）；
    默认抛给调用方（写操作必须知道自己拿不到树）。
    """
    if tree is None:
        try:
            tree = get_datasource().list_tree()
        except Exception:
            if not safe:
                raise
            tree = []
    feeds, cats, feed_to_cat = {}, {}, {}
    for cat in tree:
        cats[cat.id] = cat.title
        for f in cat.feeds:
            feeds[f.id] = f.title
            feed_to_cat[f.id] = cat.id
    return feeds, cats, feed_to_cat


def cat_of(feed_id, tree=None) -> Optional[int]:
    """feed 当前所属分类 id；不在树里 → None（树 = Miniflux 现状，唯一判据）。"""
    _nodes, cats = feed_index(tree)
    return cats.get(int(feed_id))


def feed_title(feed_id, tree=None, default=None) -> str:
    """feed 标题；不在树里 → default（缺省 `feed:<id>`）。"""
    nodes, _cats = feed_index(tree)
    fid = int(feed_id)
    return nodes[fid].title if fid in nodes else (default or f"feed:{fid}")


def category_title(category_id, tree=None, default="") -> str:
    for cat in (tree if tree is not None else get_datasource().list_tree()):
        if cat.id == int(category_id):
            return cat.title
    return default


def feeds_of_category(category_id, tree=None) -> list:
    """某分类下的全部 FeedNode（分类配置变更后重建 prompt 用）。"""
    if tree is None:
        tree = get_datasource().list_tree()
    for cat in tree:
        if cat.id == int(category_id):
            return list(cat.feeds)
    return []
