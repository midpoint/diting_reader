"""深链落在列表窗口外时的「补行」（2026-11-03）。

修的是这条静默失败：
    L3 通知点进来是 `/?sel=feed:<源>&open=<条目>#row-<条目>`，
    而 `openFromUrl()` 只在 `#row-<id>` **已经在 DOM 里**时才展开 —— 列表窗口
    只有最近 `ENTRY_WINDOW`(200) 条，旧文不在里面，于是它 `return` 一句了事：
    正文没展开、不报错、也不说为什么。人看到的就是"通知坏了"。

现在的契约（这一组守的就是它）：
  1. `/hx/row/<id>` 能把**任意**一条渲染成列表里那一行（含 `data-pinned`）；
  2. **不受当前级别筛选影响** —— 人是点名来的，不该被「当前只看 L1」挡掉；
  3. 只读：GET 不许有副作用（不许顺手标已读），标已读仍归展开那一步；
  4. 条目真的不在库里时，**给出可读的理由**（前端据此说"已被清理"，
     而不是笼统的"打不开"）。

★ 为什么不只测 JS：这条链路一半在服务端（行片段长什么样）。前端那一半在
  `test_appjs.py` 里守（openFromUrl 不许再写 `if (!row) return`）。
"""
from datetime import timedelta
from unittest import mock

from django.test import RequestFactory, TestCase
from django.utils import timezone

from apps.reader import views as rv


class _Entry:
    """模板真正会碰到的字段：少一个就是 500，而 500 在 htmx 里等于"什么都没发生"。"""

    def __init__(self, entry_id=9001, feed_id=12, unread=True):
        self.id = entry_id
        self.feed_id = feed_id
        self.unread = unread
        self.starred = False
        self.title = "一篇很久以前的文章"
        self.feed_title = "某个源"
        # 真实时间：`reldt` 与"超窗过期"都靠它，给 None 就是在测一个不存在的形状
        self.published_at = timezone.now() - timedelta(days=40)
        self.created_at = self.published_at
        self.url = "https://example.com/a"


class _DS:
    def __init__(self, entry=None, boom=False, tree=None):
        self.entry = entry
        self.boom = boom
        self.tree = tree or []
        self.mark_read_calls = []

    def list_tree(self):
        return self.tree

    def get_entry(self, _id):
        if self.boom:
            raise RuntimeError("miniflux 404")
        return self.entry

    def mark_read(self, ids):
        self.mark_read_calls.extend(ids)


class HxRowTest(TestCase):
    def _get(self, ds, query="", entry_id=9001):
        with mock.patch.object(rv, "get_datasource", return_value=ds):
            req = RequestFactory().get("/hx/row/%s?%s" % (entry_id, query))
            return rv.hx_row(req, entry_id)

    def test_renders_the_same_row_as_the_list(self):
        r = self._get(_DS(_Entry()), query="sel=feed:12")
        self.assertEqual(r.status_code, 200)
        body = r.content.decode("utf-8")
        self.assertIn('id="row-9001"', body)
        self.assertIn('hx-post="/hx/entry/9001', body)   # 展开仍走列表那条链路
        self.assertIn("一篇很久以前的文章", body)

    def test_row_is_marked_as_pinned(self):
        """插在列表顶部的行必须自报身份：否则会被当成"最新一条"读。"""
        body = self._get(_DS(_Entry()), query="sel=feed:12").content.decode("utf-8")
        self.assertIn("is-pinned", body)
        self.assertIn('data-pinned="1"', body)
        self.assertIn("单独打开", body)

    def test_local_refresh_row_has_no_pinned_marker(self):
        """★ 翻译后局部刷新拿了同一行，但 local=1 时不能冒出「单独打开」——
        它本来就在列表里，凭空贴个「不在当前列表」的标记是误导。
        ★ 深链不带 local，上面 test_row_is_marked_as_pinned 钉死行为不变。"""
        # 前端 trRefreshRow 发的就是 `/hx/row/<id>?local=1`（不带 sel）——
        # 测试形状必须与真实请求一致，否则漏掉「无 sel 时 500」的静默失败。
        body = self._get(_DS(_Entry()), query="local=1").content.decode("utf-8")
        self.assertIn('id="row-9001"', body)
        self.assertNotIn("is-pinned", body)
        self.assertNotIn('data-pinned="1"', body)
        self.assertNotIn("单独打开", body)

    def test_row_reports_read_state_for_context_menu(self):
        """★ 文章右键「标记为未读」按 data-unread 决定给不给入口：
        已读行给（可逆出口），未读行不给（空动作）。服务端渲染必须带真值，
        不能靠前端猜——展开后由 app.js 的 afterSwap 同步翻成 0。"""
        unread = self._get(_DS(_Entry(unread=True)), query="sel=feed:12")
        read = self._get(_DS(_Entry(unread=False)), query="sel=feed:12")
        self.assertIn('data-unread="1"', unread.content.decode("utf-8"))
        self.assertIn('data-unread="0"', read.content.decode("utf-8"))

    def test_row_carries_data_cfg_from_tree(self):
        """★ 行的 has_cfg 跟着树节点走（口径 = _annotate_cfg 已算好的 f.has_cfg），
        不是行自己另判一遍。★ 直接测 _decorate：hx_row 会经 _tree()→_annotate_cfg
        从真实 DB 重算判据、覆盖手设的 has_cfg，那是集成行为，不在这里造。"""
        from core.datasource.base import CategoryNode, FeedNode

        bare = FeedNode(id=12, title="某个源", site_url="", category_id=3,
                        category_title="分类")
        tree = [CategoryNode(id=3, title="分类", feeds=[bare])]
        filters = {"levels": rv.LEVEL_ALL}
        row0 = rv._decorate([_Entry()], filters, tree)[0]
        self.assertFalse(row0["has_cfg"])
        bare.has_cfg = True                 # 模拟 _annotate_cfg 打标（含分类继承）
        row1 = rv._decorate([_Entry()], filters, tree)[0]
        self.assertTrue(row1["has_cfg"])

    def test_level_filter_does_not_hide_a_named_entry(self):
        """★ 点名 ≠ 浏览：`?levels=3` 时未评级（默认 L2）的条目也必须出来。

        这条是「静默失败」的另一种形态 —— 行取回来了但被筛选丢掉，结果一样是
        "从通知点进来什么都没有"。变异验证：把 levels=LEVEL_ALL 改回
        filters["levels"]，这条立刻红（400 + 行不存在）。
        """
        r = self._get(_DS(_Entry()), query="sel=feed:12&levels=3")
        self.assertEqual(r.status_code, 200)
        self.assertIn('id="row-9001"', r.content.decode("utf-8"))

    def test_get_has_no_side_effects(self):
        """取行不能顺手标已读：展开才该标。"""
        ds = _DS(_Entry(unread=True))
        self.assertEqual(self._get(ds, query="sel=feed:12").status_code, 200)
        self.assertEqual(ds.mark_read_calls, [])

    def test_missing_entry_gives_a_readable_reason(self):
        r = self._get(_DS(None), query="sel=feed:12")
        self.assertEqual(r.status_code, 400)
        self.assertIn("条目不存在", r.content.decode("utf-8"))

    def test_datasource_error_does_not_500(self):
        """Miniflux 挂了要变成"这条没了"的 400，不是 500。

        500 在 htmx 里等于不 swap ⇒ 面板/正文一动不动 ⇒ 又是一次静默失败。
        """
        r = self._get(_DS(_Entry(), boom=True), query="sel=feed:12")
        self.assertEqual(r.status_code, 400)
