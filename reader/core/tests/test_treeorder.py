"""树的手动顺序（拖动重排）：叠加规则、整层校验、换层 / 删除时的清理。

钉住四件容易改坏的事：
  1. **手动优先、没排过的保持默认顺序排末尾**（新项不用补 sort，也不会插到中间）；
  2. `cat.feeds` 一个元素都不动，渲染用的是另挂的 `cat.root_feeds`
     （顺序漏进 summary / 开销是这条约束要防的）；
  3. 重排必须**整层**对得上，项集不符就整笔拒（过期页面洗不掉别人的顺序）；
  4. 归属变了（换组 / 删源 / 删组 / 删分类）就丢掉那一项的位置，不留孤儿行。
"""
import json
from unittest import mock

from django.test import RequestFactory, TestCase

from apps.reader import treeviews
from core import readergroups as rgroups
from core import treeorder
from core.datasource.base import CategoryNode, FeedNode
from core.models import FeedGroup, FeedGroupMember, TreeOrder


def _feed(fid, cid, unread=0, gid=0):
    return FeedNode(id=fid, title=f"feed{fid}", site_url="", category_id=cid,
                    category_title=f"cat{cid}", unread=unread, group_id=gid)


def fake_tree():
    """cat1：grp10[11,12] + 根 13、14；cat2：grp20[21]。★ 组内与 cat.feeds 共用同一批引用。"""
    f11, f12, f13, f14, f21 = (_feed(11, 1, 3, 10), _feed(12, 1, 5, 10),
                               _feed(13, 1, 1), _feed(14, 1, 9), _feed(21, 2, 0, 20))
    g10 = rgroups.GroupView(id=10, title="g10", category_id=1, feeds=[f11, f12])
    g20 = rgroups.GroupView(id=20, title="g20", category_id=2, feeds=[f21])
    c1 = CategoryNode(id=1, title="cat1")
    c1.feeds = [f11, f12, f13, f14]
    c1.groups = [g10]
    c2 = CategoryNode(id=2, title="cat2")
    c2.feeds = [f21]
    c2.groups = [g20]
    return [c1, c2]


def ids(items):
    return [i.id for i in items]


class ApplyTest(TestCase):
    def test_no_ranks_keeps_default_order(self):
        out = treeorder.apply(fake_tree())
        self.assertEqual(ids(out), [1, 2])                       # 进来的顺序 = 默认序
        self.assertEqual(ids(out[0].groups), [10])
        self.assertEqual(ids(out[0].root_feeds), [13, 14])
        self.assertFalse(out[0].layer_ordered)
        self.assertFalse(out[0].groups[0].layer_ordered)
        self.assertFalse(out[0].root_feeds[0].layer_ordered)

    def test_cat_feeds_untouched(self):
        """★ `cat.feeds` 仍是全量（summary / 开销靠它），顺序也不被侧栏拖动影响。"""
        tree = fake_tree()
        before = ids(tree[0].feeds)
        out = treeorder.apply(tree)
        self.assertEqual(ids(out[0].feeds), before)
        self.assertEqual(ids(out[0].root_feeds), [13, 14])       # 只有未分组的进这一段

    def test_partial_rank_heads_and_tail_keeps_default(self):
        """只排过两项 ⇒ 它们在前面，其余**保持默认顺序**跟在后面。"""
        treeorder.set_order(treeorder.KIND_CAT, "", [2, 1], fake_tree())
        out = treeorder.apply(fake_tree())
        self.assertEqual(ids(out), [2, 1])

        TreeOrder.objects.all().delete()
        treeorder.set_order(treeorder.KIND_FEED, "cat:1", [14, 13],
                            treeorder.apply(fake_tree()))
        out = treeorder.apply(fake_tree())
        self.assertEqual(ids(out[0].root_feeds), [14, 13])
        self.assertTrue(out[0].root_feeds[0].layer_ordered)

    def test_three_layers_independent(self):
        """分类层 / 目录层 / 组内 feed 层各排各的，互不串台。"""
        tree = treeorder.apply(fake_tree())
        treeorder.set_order(treeorder.KIND_CAT, "", [2, 1], tree)
        treeorder.set_order(treeorder.KIND_GRP, "cat:1", [10], tree)
        treeorder.set_order(treeorder.KIND_FEED, "grp:10", [12, 11], tree)

        out = treeorder.apply(fake_tree())
        self.assertEqual(ids(out), [2, 1])                       # 分类层：cat2 排到了前面
        self.assertEqual(ids(out[1].groups), [10])               # cat1 的目录层
        self.assertEqual(ids(out[1].groups[0].feeds), [12, 11])   # 组内 feed 层
        self.assertEqual(ids(out[0].groups), [20])               # cat2 没被牵连
        self.assertTrue(out[1].layer_ordered)
        self.assertTrue(out[1].groups[0].layer_ordered)
        self.assertTrue(out[1].groups[0].feeds[0].layer_ordered)
        self.assertFalse(out[0].groups[0].feeds[0].layer_ordered)

    def test_apply_idempotent(self):
        treeorder.set_order(treeorder.KIND_CAT, "", [2, 1], fake_tree())
        once = ids(treeorder.apply(fake_tree()))
        twice = ids(treeorder.apply(treeorder.apply(fake_tree())))
        self.assertEqual(once, twice)


class SetOrderTest(TestCase):
    def test_dense_sorts_written(self):
        n = treeorder.set_order(treeorder.KIND_FEED, "cat:1", [14, 13], fake_tree())
        self.assertEqual(n, 2)
        self.assertEqual(treeorder.ranks(treeorder.KIND_FEED, "cat:1"), {14: 1, 13: 2})

    def test_second_write_replaces_whole_layer(self):
        treeorder.set_order(treeorder.KIND_FEED, "cat:1", [14, 13], fake_tree())
        treeorder.set_order(treeorder.KIND_FEED, "cat:1", [13, 14], fake_tree())
        self.assertEqual(TreeOrder.objects.filter(kind="feed").count(), 2)
        self.assertEqual(treeorder.ranks(treeorder.KIND_FEED, "cat:1"), {13: 1, 14: 2})

    def test_stale_layer_rejected_wholesale(self):
        """少一项 / 多一项 / 别层的项 —— 一律拒，且库里一个字节都不变。"""
        treeorder.set_order(treeorder.KIND_FEED, "cat:1", [14, 13], fake_tree())
        for bad in ([14], [13, 14, 99], [13, 13], []):
            with self.assertRaises(ValueError):
                treeorder.set_order(treeorder.KIND_FEED, "cat:1", bad, fake_tree())
        self.assertEqual(treeorder.ranks(treeorder.KIND_FEED, "cat:1"), {14: 1, 13: 2})

    def test_cat_scope_is_always_global(self):
        """分类只有一个全局列表：前端送什么都不认，一律归零（两处口径不会分叉）。"""
        treeorder.set_order(treeorder.KIND_CAT, "cat:1", [2, 1], fake_tree())
        self.assertEqual(TreeOrder.objects.filter(kind="cat", scope="").count(), 2)

    def test_unknown_scope_rejected(self):
        tree = treeorder.apply(fake_tree())
        with self.assertRaises(ValueError):
            treeorder.set_order(treeorder.KIND_GRP, "cat:99", [10], tree)
        with self.assertRaises(ValueError):
            treeorder.set_order(treeorder.KIND_FEED, "whatever:1", [11], tree)
        with self.assertRaises(ValueError):
            treeorder.set_order("weird", "", [1], tree)


class ResetTest(TestCase):
    def test_reset_clears_only_that_layer(self):
        tree = treeorder.apply(fake_tree())
        treeorder.set_order(treeorder.KIND_CAT, "", [2, 1], tree)
        treeorder.set_order(treeorder.KIND_FEED, "cat:1", [14, 13], tree)
        self.assertEqual(treeorder.reset(treeorder.KIND_FEED, "cat:1"), 2)
        self.assertEqual(TreeOrder.objects.filter(kind="cat").count(), 2)   # 分类层没被牵连

    def test_cat_reset_scope_is_never_per_category(self):
        """★ 分类只有全局一个列表：kind=cat 时 scope 一律归一（`norm_scope`），
          传 `cat:1` 也清的是整层 —— 别把它当成"只清某一个分类"。"""
        tree = treeorder.apply(fake_tree())
        treeorder.set_order(treeorder.KIND_CAT, "", [2, 1], tree)
        self.assertEqual(treeorder.reset(treeorder.KIND_CAT, "cat:1"), 2)
        self.assertEqual(TreeOrder.objects.filter(kind="cat").count(), 0)

    def test_reset_empty_layer_is_not_an_error(self):
        self.assertEqual(treeorder.reset(treeorder.KIND_GRP, "cat:1"), 0)


class CleanupTest(TestCase):
    """顺序行跟着归属走：换层 / 删除都不许留下孤儿。"""

    def setUp(self):
        self.tree = fake_tree()
        FeedGroup.objects.create(id=10, category_id=1, title="g10", sort=1)
        FeedGroup.objects.create(id=20, category_id=2, title="g20", sort=1)
        FeedGroupMember.objects.create(feed_id=11, group_id=10)
        FeedGroupMember.objects.create(feed_id=12, group_id=10)
        FeedGroupMember.objects.create(feed_id=21, group_id=20)

    def _rank(self, kind, scope, ref):
        return TreeOrder.objects.filter(kind=kind, scope=scope, ref_id=ref).exists()

    def test_move_feed_drops_its_position_only(self):
        treeorder.set_order(treeorder.KIND_FEED, "grp:10", [12, 11], self.tree)
        rgroups.move_feed(11, None, tree=self.tree)             # 移出分组到分类根
        self.assertFalse(self._rank("feed", "grp:10", 11))
        self.assertTrue(self._rank("feed", "grp:10", 12))       # 同组别人的位置不动

    def test_move_feed_into_same_group_keeps_position(self):
        treeorder.set_order(treeorder.KIND_FEED, "grp:10", [12, 11], self.tree)
        rgroups.move_feed(11, 10, tree=self.tree)               # 本来就在这组 ⇒ 什么都不动
        self.assertTrue(self._rank("feed", "grp:10", 11))

    def test_delete_group_drops_group_and_its_feed_layer(self):
        treeorder.set_order(treeorder.KIND_GRP, "cat:1", [10], self.tree)
        treeorder.set_order(treeorder.KIND_FEED, "grp:10", [12, 11], self.tree)
        rgroups.delete_group(10)
        self.assertEqual(TreeOrder.objects.count(), 0)

    def test_delete_category_local_drops_all_its_layers(self):
        tree = treeorder.apply(fake_tree())
        treeorder.set_order(treeorder.KIND_CAT, "", [2, 1], tree)
        treeorder.set_order(treeorder.KIND_GRP, "cat:1", [10], tree)
        treeorder.set_order(treeorder.KIND_FEED, "cat:1", [14, 13], tree)
        treeorder.set_order(treeorder.KIND_FEED, "grp:10", [12, 11], tree)
        rgroups.delete_category_local(1)
        self.assertEqual(treeorder.ranks(treeorder.KIND_CAT, ""), {2: 1})   # cat2 还在
        self.assertEqual(TreeOrder.objects.filter(scope__contains="1").count(), 0)

    def test_delete_feed_local_drops_only_that_feed(self):
        tree = treeorder.apply(fake_tree())
        treeorder.set_order(treeorder.KIND_FEED, "cat:1", [13, 14], tree)
        rgroups.delete_feed_local(13)
        self.assertFalse(self._rank("feed", "cat:1", 13))
        self.assertTrue(self._rank("feed", "cat:1", 14))


class ReorderEndpointTest(TestCase):
    def post(self, url="/tree/reorder", **fields):
        return treeviews.tree_reorder(RequestFactory().post(url, fields))

    def test_reorder_ok(self):
        with mock.patch.object(treeviews, "tree_view",
                               side_effect=lambda: treeorder.apply(fake_tree())):
            resp = self.post(kind="feed", scope="cat:1", ids="14,13")
        data = json.loads(resp.content)
        self.assertTrue(data["ok"], data)
        self.assertEqual(treeorder.ranks(treeorder.KIND_FEED, "cat:1"), {14: 1, 13: 2})

    def test_stale_layer_is_409(self):
        with mock.patch.object(treeviews, "tree_view",
                               side_effect=lambda: treeorder.apply(fake_tree())):
            resp = self.post(kind="feed", scope="cat:1", ids="14,99")
        self.assertEqual(resp.status_code, 409)
        self.assertIn("已经变了", json.loads(resp.content)["error"])
        self.assertEqual(TreeOrder.objects.count(), 0)

    def test_bad_inputs_rejected(self):
        with mock.patch.object(treeviews, "tree_view",
                               side_effect=lambda: treeorder.apply(fake_tree())):
            self.assertEqual(self.post(kind="cat", ids="").status_code, 400)
            self.assertEqual(self.post(kind="weird", ids="1,2").status_code, 400)
            self.assertEqual(self.post(kind="cat", ids="1,x").status_code, 400)

    def test_reset_endpoint(self):
        treeorder.set_order(treeorder.KIND_CAT, "", [2, 1], treeorder.apply(fake_tree()))
        with mock.patch.object(treeviews, "tree_view",
                               side_effect=lambda: treeorder.apply(fake_tree())):
            resp = treeviews.tree_order_reset(RequestFactory().post(
                "/tree/order/reset", {"kind": "cat"}))
        self.assertTrue(json.loads(resp.content)["ok"])
        self.assertEqual(TreeOrder.objects.count(), 0)
