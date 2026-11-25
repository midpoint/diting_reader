"""DataSource 抽象层：只暴露阅读器语义，不泄漏 Miniflux 概念。"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class FeedNode:
    id: int
    title: str
    site_url: str
    category_id: int
    category_title: str
    unread: int = 0
    ok: bool = True
    error_msg: str = ""
    last_success: Optional[datetime] = None
    icon_url: str = ""
    # ★ 分级判据是否已配置（自己写的，或从分类继承的）—— 树上给一个 L 标记。
    #   由视图层 annotate（数据源不该知道分级配置），默认 False = 走零成本默认路径。
    has_cfg: bool = False
    cfg_from: str = ""
    # ★ 所属虚拟分组 id（0 = 未分组）。同样由视图层挂（readergroups.decorate），
    #   数据源不知道分组这回事。模板里用它决定"哪些 feed 还要在分类根下裸着渲染"。
    group_id: int = 0
    # ★ 本行所在的兄弟列表（同组内 / 同分类根下）有没有被手动排过序。
    #   同样由视图层挂（`core/treeorder.apply`），右键「恢复默认顺序」只在这为真时给。
    layer_ordered: bool = False

    @property
    def has_error(self) -> bool:
        return not self.ok


@dataclass
class CategoryNode:
    id: int
    title: str
    feeds: list = field(default_factory=list)
    # ★ 虚拟分组层（阅读页树专用，见 core/readergroups.py）。
    #   **数据源永远不填这个字段** —— 由视图层 `readergroups.decorate()` 挂上去。
    #   默认空 list，所以模板里 `{% for g in cat.groups %}` 永远安全。
    #   ★★ `feeds` 必须保持**全量**（分组只是「叠加索引」不是「替换树」），
    #      否则下面的 unread / 以及 summary·costs 的统计会漏数。
    groups: list = field(default_factory=list)
    # ★ 分类根下**未分组**的那段 feed 的展示顺序，由 `core/treeorder.apply()` 挂。
    #   模板渲染这一段用它而不是 `feeds` —— `feeds` 是全量统计口径，顺序不该被
    #   侧栏拖动影响到（summary / costs 读的是它）。默认空 list，模板里永远安全。
    root_feeds: list = field(default_factory=list)
    # ★ 分类这一层（分类之间）有没有被手动排过序，同上（`treeorder.apply`）。
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


@dataclass
class Entry:
    id: int
    feed_id: int
    feed_title: str
    title: str
    url: str
    published_at: Optional[datetime]
    created_at: Optional[datetime]
    status: str = "unread"
    starred: bool = False
    author: str = ""
    reading_time: int = 0
    content: str = ""

    @property
    def unread(self) -> bool:
        return self.status != "read"


class DataSource:
    """阅读器数据源接口。未来换自建 RSS engine 时只需实现同样的方法。"""

    def list_tree(self) -> list:
        """返回 CategoryNode 列表（每个含下属 FeedNode）。"""
        raise NotImplementedError

    def list_entries(self, *, feed_id=None, category_id=None, status="all",
                     search="", limit=200, offset=0, starred=None,
                     published_after=None, published_before=None) -> list:
        """按发布时间窗取条目。

        ★ `published_after` / `published_before` 是 **published_at 口径**
          （不是 created_at）—— 统计与总结一律用它，混用会差数倍（已有教训）。
          实现方若底层不支持服务端过滤，就在返回前自己按 published_at 截。
        """
        raise NotImplementedError

    def get_entry(self, entry_id) -> Optional[Entry]:
        raise NotImplementedError

    def list_entries_multi(self, feed_ids, *, status="all", search="", limit=200) -> list:
        """**多 feed 归并**取条目（虚拟分组视图用）。

        ★ 存在的理由：Miniflux 的 `/v1/entries` 只支持**单个** `feed_id`
          （2.3.3 `entry_handlers.go` 用 `QueryInt64Param`，重复传参只取第一个），
          分组（多源）没有服务端原生支持 → 只能逐 feed 拉取后在内存归并。
        ★ 口径提醒：结果是"每源最近 N 条里取全局最近 limit 条"，
          **不是**"全局最近 limit 条"。低频源可能因此只进来很少几条。
        """
        raise NotImplementedError

    def counters(self) -> dict:
        """{feed_id: {'reads': n, 'unreads': n}}"""
        raise NotImplementedError

    def health(self, feed_id) -> dict:
        """{'ok': bool, 'last_success': datetime|None, 'error_msg': str}"""
        raise NotImplementedError

    def mark_read(self, entry_ids) -> int:
        raise NotImplementedError

    def mark_unread(self, entry_ids) -> int:
        """反向写回（把已读条目退回未读）。与 mark_read 同源同端点，只是 status 不同。

        ★ 为什么要显式列进接口：谛听点开即标已读，但没有反向入口时「误点开」是
          不可逆的——只能去 Miniflux 网页改。实现方必须真写回真相层并使缓存失效，
          不许只在本地改个显示状态。
        """
        raise NotImplementedError

    def mark_scope_read(self, *, feed_id=None, category_id=None) -> int:
        raise NotImplementedError

    def mark_read_before(self, *, days, feed_id=None, category_id=None) -> int:
        raise NotImplementedError

    def iter_unread(self, *, feed_id=None, category_id=None):
        """分页遍历全部未读条目（/v1/entries 单次 limit 上限 1000，不能一次取完）。"""
        raise NotImplementedError

    def toggle_star(self, entry_id, starred: bool) -> bool:
        raise NotImplementedError

    def refresh_feed(self, feed_id) -> bool:
        raise NotImplementedError

    def fetch_content(self, entry_id, *, timeout=120) -> bool:
        """抓取原文全文（Miniflux 的 fetch original content）。写操作。

        ★ 同步抓源站，慢站可能几十秒；只读实现可继续抛 NotImplementedError。
        """
        raise NotImplementedError

    # 写操作（Miniflux 是真相层；只读实现可继续抛 NotImplementedError）
    def create_category(self, title: str) -> dict:
        raise NotImplementedError

    def rename_category(self, category_id, title: str) -> dict:
        raise NotImplementedError

    def create_feed(self, feed_url: str, category_id) -> dict:
        raise NotImplementedError

    def delete_category(self, category_id) -> bool:
        raise NotImplementedError

    def icon(self, feed_id):
        """返回 (content_type, bytes) 或 None。"""
        raise NotImplementedError
