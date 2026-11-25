"""域服务层：reportstore（报告存取）/ treekit（树索引）/ 分组级联清理。

这三块都是「收敛重复实现」的产物（见各模块头），测试钉住两件事：
  · 行为正确（原子写、文件为准、级联清干净）；
  · 口径与被收敛的旧实现一致（criteria_index ↔ build_spec.has_any）。
"""
import tempfile
from datetime import date
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from core import readergroups, reportstore, treekit
from core import promptspec
from core.datasource.base import CategoryNode, FeedNode
from core.models import (CategoryTriageConfig, FeedGroup, FeedGroupMember,
                         FeedTriageConfig)


class ReportStoreTest(SimpleTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._dir = Path(self._tmp.name)

    def test_write_read_roundtrip(self):
        with override_settings(REPORT_DIR=self._dir):
            day = date(2026, 10, 20)
            p = reportstore.write(day, "# 标题\n正文")
            self.assertEqual(p, reportstore.path(day))
            self.assertEqual(reportstore.read(day), "# 标题\n正文")

    def test_write_is_atomic_no_tmp_left(self):
        with override_settings(REPORT_DIR=self._dir):
            reportstore.write(date(2026, 10, 21), "x")
            leftovers = list(self._dir.glob("*.tmp"))
            self.assertEqual(leftovers, [])

    def test_read_missing_or_empty_is_none(self):
        with override_settings(REPORT_DIR=self._dir):
            self.assertIsNone(reportstore.read(date(2026, 1, 1)))
            (self._dir / "daily-2026-01-02.md").write_text("", encoding="utf-8")
            self.assertIsNone(reportstore.read(date(2026, 1, 2)))   # 空文件 = 没报告

    GOOD = "# 谛听日报 · 2026-11-02\n\n## 一、数量统计\n\n合并型摘要正文。\n"
    BAD = ("# 谛听日报 · 2026-11-02\n\n## 三、内容（规则版 · 未调用模型）\n\n"
           "> ⚠️ 降级原因：日预算已用尽\n")

    def test_degraded_does_not_overwrite_good(self):
        """★ 2026-11-05 事故的根：预算耗尽 → 规则版 → 覆盖同名文件。"""
        with override_settings(REPORT_DIR=self._dir):
            day = date(2026, 11, 2)
            reportstore.write(day, self.GOOD)
            with self.assertLogs("core.reportstore", level="WARNING") as cm:
                reportstore.write(day, self.BAD)          # 不传 degraded，靠指纹也认得出
            self.assertEqual(reportstore.read(day), self.GOOD, "正常报告被覆盖了")
            kept = sorted(self._dir.glob("daily-2026-11-02.degraded-*.md"))
            self.assertEqual(len(kept), 1, "降级版没留档")
            self.assertIn("降级原因", kept[0].read_text(encoding="utf-8"))
            self.assertIn("不覆盖", "\n".join(cm.output), "没记日志 = 静默")
            # ★ 留档不能被当成「这天的报告」：否则同一天枚举成两份
            self.assertEqual(reportstore.days(), [day])

    def test_degraded_flag_wins_over_text(self):
        """调用方知道是降级版时，正文里没有指纹也不许覆盖。"""
        with override_settings(REPORT_DIR=self._dir):
            day = date(2026, 11, 3)
            reportstore.write(day, self.GOOD)
            reportstore.write(day, "# 看着很正常\n", degraded=True)
            self.assertEqual(reportstore.read(day), self.GOOD)

    def test_degraded_may_replace_degraded(self):
        with override_settings(REPORT_DIR=self._dir):
            day = date(2026, 11, 4)
            reportstore.write(day, self.BAD)
            reportstore.write(day, self.BAD + "\n第二次\n")
            self.assertIn("第二次", reportstore.read(day))

    def test_good_may_replace_good(self):
        """正常重跑照旧覆盖（守卫只拦「更差的产物」）。"""
        with override_settings(REPORT_DIR=self._dir):
            day = date(2026, 11, 5)
            reportstore.write(day, self.GOOD)
            reportstore.write(day, self.GOOD + "\n新版本\n")
            self.assertIn("新版本", reportstore.read(day))

    def test_days_only_counts_nonempty_in_window(self):
        with override_settings(REPORT_DIR=self._dir):
            reportstore.write(date(2026, 10, 1), "a")
            reportstore.write(date(2026, 10, 3), "b")
            (self._dir / "daily-2026-10-02.md").write_text("", encoding="utf-8")
            got = reportstore.days(since=date(2026, 9, 30), upto=date(2026, 10, 4))
            self.assertEqual(got, [date(2026, 10, 1), date(2026, 10, 3)])
            self.assertEqual(reportstore.first_day(), date(2026, 10, 1))


class ReportStoreActiveSinceTest(TestCase):
    """没有报告文件时的两级退回（SummaryRun → LlmUsage）。"""

    def test_falls_back_to_summary_run(self):
        from core.models import SummaryRun
        from django.utils import timezone

        SummaryRun.objects.create(date=date(2026, 9, 1), state="pending")
        with override_settings(REPORT_DIR=Path(tempfile.mkdtemp())):
            self.assertEqual(reportstore.active_since(), date(2026, 9, 1))


class TreeKitTest(SimpleTestCase):
    def fake_tree(self):
        f1 = FeedNode(id=1, title="源一", site_url="", category_id=10,
                      category_title="投资")
        f2 = FeedNode(id=2, title="源二", site_url="", category_id=20,
                      category_title="技术")
        return [CategoryNode(id=10, title="投资", feeds=[f1]),
                CategoryNode(id=20, title="技术", feeds=[f2])]

    def test_feed_index_and_lookups(self):
        tree = self.fake_tree()
        nodes, cats = treekit.feed_index(tree)
        self.assertEqual(nodes[1].title, "源一")
        self.assertEqual(cats[2], 20)
        self.assertEqual(treekit.cat_of(1, tree), 10)
        self.assertIsNone(treekit.cat_of(999, tree))
        self.assertEqual(treekit.feed_title(1, tree), "源一")
        self.assertEqual(treekit.feed_title(999, tree), "feed:999")
        self.assertEqual(treekit.category_title(20, tree), "技术")
        self.assertEqual(treekit.feeds_of_category(10, tree)[0].id, 1)

    def test_title_maps_safe_swallows_datasource_failure(self):
        with mock.patch.object(treekit, "get_datasource",
                               side_effect=RuntimeError("miniflux down")):
            feeds, cats, f2c = treekit.title_maps(safe=True)
        self.assertEqual((feeds, cats, f2c), ({}, {}, {}))

    def test_title_maps_unsafe_raises(self):
        with mock.patch.object(treekit, "get_datasource",
                               side_effect=RuntimeError("miniflux down")):
            with self.assertRaises(RuntimeError):
                treekit.title_maps(safe=False)


class DeleteCategoryLocalTest(TestCase):
    """分类删除的级联清理：分组 + 成员 + 分类判据一次清干净。"""

    def test_cascade_clears_groups_and_criteria(self):
        FeedGroup.objects.create(category_id=500, title="组A", sort=1)
        FeedGroup.objects.create(category_id=500, title="组B", sort=2)
        FeedGroup.objects.create(category_id=501, title="别的分类的组", sort=1)
        FeedGroupMember.objects.create(feed_id=101, group_id=1)
        FeedGroupMember.objects.create(feed_id=102, group_id=2)
        FeedGroupMember.objects.create(feed_id=103, group_id=3)
        CategoryTriageConfig.objects.create(category_id=500, l2_text="判据")
        CategoryTriageConfig.objects.create(category_id=501, l1_text="别的分类")

        # ★ 快照会写 data/backups —— 测试里打桩，不碰真实快照目录
        with mock.patch("core.groupbackup.snapshot"):
            n = readergroups.delete_category_local(500)

        self.assertEqual(n, 2)
        self.assertEqual(list(FeedGroup.objects.values_list("id", flat=True)), [3])
        self.assertEqual(list(FeedGroupMember.objects.values_list("feed_id", flat=True)),
                         [103])                       # 别的分类的成员不受影响
        self.assertFalse(
            CategoryTriageConfig.objects.filter(category_id=500).exists())
        self.assertTrue(
            CategoryTriageConfig.objects.filter(category_id=501).exists())


class CriteriaIndexTest(TestCase):
    """树上 L 标记与「是否调 LLM」必须同口径：criteria_index ↔ build_spec.has_any。"""

    def test_agrees_with_build_spec(self):
        CategoryTriageConfig.objects.create(category_id=10, l2_text="分类的 L2")
        FeedTriageConfig.objects.create(feed_id=1)                       # 全空
        FeedTriageConfig.objects.create(feed_id=2, note="只有备注")       # note 不是判据
        FeedTriageConfig.objects.create(feed_id=3, default_level=3)      # 只设默认级
        FeedTriageConfig.objects.create(feed_id=4, l0_text="本源自己的判据")
        # feed 5/6：没有行（分别挂在有/无判据的分类下）

        cats, feeds = promptspec.criteria_index()
        self.assertEqual(cats, {10})
        self.assertEqual(feeds, {4})

        for fid, in_cat in [(1, True), (2, True), (3, True), (4, True),
                            (5, True), (6, False)]:
            spec = promptspec.build_spec(fid, category_id=10 if in_cat else 99)
            marked = (fid in feeds) or (in_cat and 10 in cats)
            self.assertEqual(spec["has_any"], marked,
                             f"feed {fid} 的树标记与 has_any 分叉了")
