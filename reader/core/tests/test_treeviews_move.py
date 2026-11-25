"""树写操作的行为契约：拖动移动（feed_move）。

从 verify_groups 的「源码里没有 draggable」断言迁来 —— 那条钉的是
「P0 不做拖动」的旧决策，拖动落地后变成永久假红。这里测**行为**：
同分类内移动放行；跨分类一律 409（前端预检之外的后端硬闸）。
"""
import json
from unittest import mock

from django.test import RequestFactory, TestCase

from apps.reader import treeviews
from core.datasource.base import CategoryNode, FeedNode


def fake_tree():
    """feed 11/12 在分类 1；feed 21 在分类 2。"""
    def node(cid, fids):
        c = CategoryNode(id=cid, title=f"cat{cid}")
        c.feeds = [FeedNode(id=f, title=f"feed{f}", site_url="",
                            category_id=cid, category_title=c.title) for f in fids]
        return c

    return [node(1, [11, 12]), node(2, [21])]


class FeedMoveTest(TestCase):
    def post(self, **fields):
        req = RequestFactory().post("/tree/feed/move", fields)
        return treeviews.feed_move(req)

    def test_move_into_group_same_category_ok(self):
        with mock.patch.object(treeviews.treekit, "tree", return_value=fake_tree()), \
                mock.patch.object(treeviews.rgroups, "move_feed", return_value=object()) as mv:
            resp = self.post(feed_id="11", target="group:5")
        data = json.loads(resp.content)
        self.assertTrue(data["ok"], data)
        # 移动用的树来自 treekit（同一份现状），不自己拉树
        mv.assert_called_once_with(11, 5, tree=mock.ANY)

    def test_move_out_to_category_root_same_category_ok(self):
        with mock.patch.object(treeviews.treekit, "tree", return_value=fake_tree()), \
                mock.patch.object(treeviews.rgroups, "move_feed", return_value=None) as mv:
            resp = self.post(feed_id="11", target="cat:1")
        self.assertTrue(json.loads(resp.content)["ok"])
        mv.assert_called_once_with(11, None, tree=mock.ANY)

    def test_cross_category_move_rejected_409(self):
        # feed 11 在分类 1，拖到分类 2 的根 → 409（清零解析计数/改判据继承的风险动作）
        with mock.patch.object(treeviews.treekit, "tree", return_value=fake_tree()), \
                mock.patch.object(treeviews.rgroups, "move_feed") as mv:
            resp = self.post(feed_id="11", target="cat:2")
        self.assertEqual(resp.status_code, 409)
        self.assertIn("跨分类移动暂未开放", json.loads(resp.content)["error"])
        mv.assert_not_called()

    def test_feed_not_in_tree_rejected(self):
        with mock.patch.object(treeviews.treekit, "tree", return_value=fake_tree()):
            resp = self.post(feed_id="999", target="cat:1")
        self.assertEqual(resp.status_code, 400)

    def test_bad_target_rejected(self):
        with mock.patch.object(treeviews.treekit, "tree", return_value=fake_tree()):
            resp = self.post(feed_id="11", target="whatever:1")
        self.assertEqual(resp.status_code, 400)
