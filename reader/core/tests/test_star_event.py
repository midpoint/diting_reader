"""加星时间戳的记账契约（2026-10-29）。

为什么单独一个测试文件：`hx_star` 曾经**只切换星标、不写 `StarEvent`**，
而周报的「本周 star」段完全依赖这张表 —— 结果是 78 篇已加星的文章里，
本地只记到 32 篇，漏掉的 46 篇因为发布时间在窗口外，**永远进不了任何一期周报**。
这类"代码齐全、就是没接线"的 bug 靠看源码看不出来，必须真调一次视图。
"""
from unittest import mock

from django.test import RequestFactory, TestCase

from apps.reader import views as rv
from core.models import StarEvent


class _Entry:
    def __init__(self, starred=False, feed_id=12, entry_id=9001):
        self.id = entry_id
        self.feed_id = feed_id
        self.starred = starred


class _DS:
    """最小数据源桩：只实现 hx_star 用到的两个方法。"""

    def __init__(self, entry, boom=False):
        self.entry = entry
        self.boom = boom

    def get_entry(self, _id):
        return self.entry

    def toggle_star(self, _id, want):
        if self.boom:
            raise RuntimeError("miniflux 502")
        self.entry.starred = want


class StarEventWiringTest(TestCase):
    def _call(self, ds, entry_id=9001):
        with mock.patch.object(rv, "get_datasource", return_value=ds):
            req = RequestFactory().post(f"/hx/star/{entry_id}")
            return rv.hx_star(req, entry_id)

    def test_star_writes_event(self):
        entry = _Entry(starred=False)
        self.assertEqual(self._call(_DS(entry)).status_code, 200)
        ev = StarEvent.objects.get(entry_id=9001)
        self.assertTrue(ev.starred)
        self.assertEqual(ev.source, "reader")
        self.assertEqual(ev.feed_id, 12)

    def test_unstar_also_recorded(self):
        entry = _Entry(starred=True)
        self._call(_DS(entry))
        ev = StarEvent.objects.get(entry_id=9001)
        self.assertFalse(ev.starred)      # 时间线要完整：取消也是事件
        self.assertEqual(ev.source, "reader")

    def test_toggle_failure_records_nothing(self):
        entry = _Entry(starred=False)
        self._call(_DS(entry, boom=True))
        self.assertFalse(StarEvent.objects.filter(entry_id=9001).exists())

    def test_record_failure_does_not_break_the_click(self):
        """记账失败不该让星标操作看起来失败（星标本身已经生效）。"""
        entry = _Entry(starred=False)
        with mock.patch.object(rv.StarEvent.objects, "create",
                               side_effect=RuntimeError("db locked")):
            resp = self._call(_DS(entry))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(entry.starred)
