"""周报调度的日期契约（2026-11-01 真实事故的守卫）。

事故经过：2026-11-01（周日）12:00 的 cron `weekly.run()` 无参调用 → `_due_week_end()`
返回 `date`，正常生成 7 份日报的周报（12:24 完成，落盘 `weekly-2026-11-01.md`）。
12:21 的兜底 `guard_tick()` 把 `timezone.localtime()`（**datetime**）传给
`_due_week_end(now)`，而它当时写的是 `return (now or timezone.localdate())`
—— 直接把 datetime 原样返回。于是：

  · `weekly_path(datetime)` 拼出 `weekly-2026-11-01T12:21:12+08:00.md`（不存在）
  · `week_days(datetime)` 生成的也是 datetime，`reportstore.read()` 去找
    `daily-2026-10-26T12:21:12+08:00.md` —— **7 天日报全部"不存在"**
  · 判「缺历史日报：2026-10-26T12:21:12+08:00、…」并通知
  · 12:38 又跑一次，`_fail` 里的 `_claim(force=True)` 把 12:24 那条 **done 覆盖成 failed**

用户看到的就是「周报跑了两次 + 提示缺历史日报」，而周报文件其实好端端在盘上。

这一组盯三件事：
1. `_due_week_end(datetime)` 必须返回 **date**（类型契约）；
2. 日期链路上的每个出口（week_days / weekly_path / collect）都吃 datetime 不出错；
3. **已有成品时 `_fail` 不许把 done 改成 failed**（哪怕判定逻辑以后再出错）。
"""
import shutil
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from core import summary, weekly
from core.models import SummaryRun

DAY = date(2026, 11, 1)


class WeekEndIsDateTest(SimpleTestCase):
    """★ 这条是事故的根因：类型契约。"""

    def test_datetime_input_returns_date(self):
        now = timezone.localtime()          # aware datetime（guard_tick 传的就是它）
        got = weekly._due_week_end(now)
        self.assertIsInstance(got, date)
        self.assertNotIsInstance(got, datetime, "datetime 漏出去就会拼错文件名")
        self.assertEqual(got, now.date())

    def test_naive_datetime_also_normalized(self):
        got = weekly._due_week_end(datetime(2026, 11, 1, 12, 21, 12))
        self.assertEqual(got, DAY)
        self.assertNotIsInstance(got, datetime)

    def test_date_input_passthrough(self):
        self.assertEqual(weekly._due_week_end(DAY), DAY)

    def test_no_argument_uses_local_today(self):
        self.assertEqual(weekly._due_week_end(), timezone.localdate())

    def test_as_date_is_idempotent(self):
        self.assertEqual(weekly._as_date(DAY), DAY)
        self.assertEqual(weekly._as_date(datetime(2026, 11, 1, 8, 0)), DAY)


class DateChainTest(SimpleTestCase):
    """日期链路上的每个出口都必须吃 datetime 而不出错。"""

    def test_week_days_with_datetime_yields_dates(self):
        got = weekly.week_days(datetime(2026, 11, 1, 12, 21, 12), 3)
        self.assertEqual(got, [date(2026, 10, 30), date(2026, 10, 31), DAY])
        for d in got:
            self.assertNotIsInstance(d, datetime, "带时分秒的日期拼不出日报文件名")

    def test_weekly_path_with_datetime(self):
        with override_settings(REPORT_DIR="/tmp/wk-path-probe"):
            p = weekly.weekly_path(datetime(2026, 11, 1, 12, 21, 12))
        self.assertEqual(p.name, "weekly-2026-11-01.md")


class CollectFindsDailiesTest(TestCase):
    """★ 这条直接复现事故：7 份日报都在，传 datetime 也必须全部读到。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        over = override_settings(REPORT_DIR=self.tmp)
        over.enable()
        self.addCleanup(over.disable)
        for d in weekly.week_days(DAY, 7):
            (Path(self.tmp) / f"daily-{d.isoformat()}.md").write_text(
                f"# 日报 {d}\n有内容的一段。", encoding="utf-8")

    def test_collect_with_datetime_finds_all(self):
        col = weekly.collect(datetime(2026, 11, 1, 12, 21, 12), 7)
        self.assertEqual(col["missing"], [], "传 datetime 不该把 7 天日报全判成缺失")
        self.assertEqual(col["covered"], 7)

    def test_collect_with_date_finds_all(self):
        col = weekly.collect(DAY, 7)
        self.assertEqual(col["missing"], [])
        self.assertEqual(col["covered"], 7)


class WindowSpanTest(SimpleTestCase):
    """★★ 周报窗口 = **7 份日报的真实合集**（2026-11-01 用户指出）。

    week_end=11/01 的周报素材是日报 10/26..11/01，而日报 10/26 的窗口是
    `[10/25 06:00, 10/26 06:00)` ⇒ 整期 = `[10/25 06:00, 11/01 06:00)`。
    原来标签写成 10/26 起（起点用了 `days-1`），把 7 天说成了 6 天；
    star 窗则整体后移一天（漏本期第一天、重复下期第一天）。

    ★ 期望值一律从 `summary.window_for`（日报窗口的唯一来源）**派生**，
      不写死字符串 —— 上一版正是把错误标签写成字面量，才让 bug 变成了"契约"。
    """

    def test_label_equals_union_of_daily_windows(self):
        ds = weekly.week_days(DAY)
        a, b = summary.window_for(ds[0])[0], summary.window_for(ds[-1])[1]
        self.assertEqual(weekly.window_label(DAY),
                         f"{a:%Y-%m-%d %H:%M} → {b:%Y-%m-%d %H:%M}")

    def test_label_starts_at_first_daily_window_start(self):
        """起点是 10-25（首份日报 10/26 的窗口起点），不是 10-26。"""
        self.assertTrue(weekly.window_label(DAY).startswith("2026-10-25 06:00"),
                        weekly.window_label(DAY))

    def test_span_is_exactly_seven_days(self):
        s, e = weekly.window_span(DAY)
        self.assertEqual((e - s).days, 7)

    def test_star_window_matches_daily_union(self):
        """star 段与日报必须同窗：否则同一篇内容在两处归到不同周。"""
        ds = weekly.week_days(DAY)
        self.assertEqual(weekly._star_window(DAY),
                         (summary.window_for(ds[0])[0], summary.window_for(ds[-1])[1]))

    def test_star_window_covers_first_day_of_week(self):
        """★ 10/25 06:00 加星的内容属于日报 10/26 ⇒ 必须进本期 star 段。"""
        s, _ = weekly._star_window(DAY)
        self.assertLessEqual(s, timezone.make_aware(datetime(2026, 10, 25, 7, 0)))

    def test_consecutive_weeks_are_contiguous(self):
        """相邻两期首尾相接：既不空出整天、也不重叠。"""
        self.assertEqual(weekly._star_window(DAY)[1],
                         weekly._star_window(DAY + timedelta(days=7))[0])

    def test_window_hour_comes_from_settingknob(self):
        """★ 窗口点与日报**同源**（`sk.effective`），不是直读静态 settings。

        改成旋钮之前，在设置页改「窗口边界」只会动日报，周报标签和 star 窗
        还停在旧值 —— 同一件事两个来源必漂。
        """
        with patch.object(weekly.sk, "effective", return_value=9):
            got = weekly.window_label(DAY)
        self.assertTrue(got.startswith("2026-10-25 09:00") and got.endswith("09:00"), got)


class FailDoesNotOverwriteDoneTest(TestCase):
    """★★ 成功结果不能被失败判定覆盖 —— 事故里最坏的一步。

    周报文件明明在盘上，SummaryRun 却被写成 failed，页面和通知都在说「未生成」。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        over = override_settings(REPORT_DIR=self.tmp)
        over.enable()
        self.addCleanup(over.disable)

    def test_fail_keeps_done_when_report_exists(self):
        weekly.write_weekly(DAY, "# 周报\n已有的成品正文。")
        SummaryRun.all_objects.create(kind="weekly", date=DAY, state="done")
        with patch.object(weekly, "notify_weekly_failed") as notify:
            res = weekly._fail(DAY, {"missing": [date(2026, 10, 26)], "covered": 7},
                               "缺历史日报：2026-10-26T12:21:12+08:00")
        self.assertTrue(res.get("skipped"), f"应忽略失败判定，实际: {res}")
        row = SummaryRun.all_objects.get(kind="weekly", date=DAY)
        self.assertEqual(row.state, "done", "done 被覆盖成 failed 是事故的第二步")
        notify.assert_not_called()          # 也不该发「未生成」通知

    def test_fail_still_fails_without_report_file(self):
        """反证：没有成品时必须照常判失败，守卫不能变成"永远跳过"。"""
        SummaryRun.all_objects.create(kind="weekly", date=DAY, state="done")
        with patch.object(weekly, "notify_weekly_failed"):
            res = weekly._fail(DAY, {"missing": [], "covered": 0}, "7 天日报一份都没有")
        self.assertFalse(res.get("ok"), f"没有文件就不该跳过，实际: {res}")
        row = SummaryRun.all_objects.get(kind="weekly", date=DAY)
        self.assertEqual(row.state, "failed")

    def test_fail_marks_failed_from_scratch(self):
        with patch.object(weekly, "notify_weekly_failed"):
            res = weekly._fail(DAY, {"missing": [], "covered": 0}, "7 天日报一份都没有")
        self.assertFalse(res.get("ok"))
        self.assertEqual(SummaryRun.all_objects.get(kind="weekly", date=DAY).state,
                         "failed")
