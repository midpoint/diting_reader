"""重跑覆盖前的二次确认（有划线/批注就拦一下，人确认后才跑）。

为什么必须拦：报告正文是**文件**，重跑 = 同名文件被换掉；划线/批注锚的是
原文句子，原文换了就落不了位（记录还在，但掉进页面底部「未落位」列表）。
这不是理论风险：2026-11-05 隔离 server 的补跑把 4 份日报换成规则版，
11-02..11-05 上 9 条划线当场失去落位基础。

判据口径（只一条，落在 `marks.rerun_warning`）：
  · 软删的不算（与 `list_marks` 同用 `.live()`）；
  · `force=1` 才可能覆盖 → 只有 force 才拦；
  · 拦 = 返回 need_confirm + 条数 + 人话，**不删任何东西**；
  · 带 `confirm=1` 仍然放行 —— 警告不是禁令。

★ 花钱路径打桩：`summary.trigger_async` / `weekly.run` 全程 mock，
  断言「被拦时一次都没调用」与「确认后恰好调用一次」——后者同时是
  「桩真被用上」的反证，否则 need_confirm 消失也可能因为压根没走到。
"""
from unittest import mock

from django.test import Client, TestCase
from django.utils import timezone

from core import marks
from core.models import Annotation, Highlight

DAY = "2026-11-02"
SID = f"daily-{DAY}"
WEEK = "2026-11-01"
WSID = f"weekly-{WEEK}"


def _post(path, data):
    return Client().post(path, data)


class _InlineThread:
    """把后台线程改成同步执行：测「确认后到底有没有真跑」。"""

    def __init__(self, target=None, name=None, daemon=None):
        self._target = target

    def start(self):
        self._target()

    def join(self):
        pass


class RerunConfirmTest(TestCase):
    def _hit(self, **kw):
        data = {"date": DAY, "force": "1"}
        data.update(kw)
        return _post("/summary/run", data)

    def test_no_marks_runs_directly(self):
        with mock.patch("apps.summary.views.summary.trigger_async",
                        return_value={"ok": True, "date": DAY}) as tr:
            r = self._hit()
        self.assertTrue(r.json()["ok"])
        self.assertEqual(tr.call_count, 1)

    def test_highlight_blocks_without_confirm(self):
        marks.add_highlight("report", SID, "被划的句子", note="")
        with mock.patch("apps.summary.views.summary.trigger_async",
                        return_value={"ok": True}) as tr:
            r = self._hit()
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertTrue(body["need_confirm"])
        self.assertEqual(body["highlights"], 1)
        self.assertIn("划线", body["msg"])
        self.assertIn("仍然重跑", body["msg"])
        # ★ 拦下就不能有副作用：一次都没提交
        self.assertEqual(tr.call_count, 0)

    def test_confirm_allows_rerun(self):
        marks.add_highlight("report", SID, "被划的句子", note="")
        with mock.patch("apps.summary.views.summary.trigger_async",
                        return_value={"ok": True, "date": DAY}) as tr:
            r = self._hit(confirm="1")
        self.assertTrue(r.json()["ok"])
        self.assertEqual(tr.call_count, 1)

    def test_no_force_never_blocks(self):
        """不带 force 的生成不覆盖已有报告（run() 自己会跳过），不该打扰。"""
        marks.add_highlight("report", SID, "被划的句子", note="")
        with mock.patch("apps.summary.views.summary.trigger_async",
                        return_value={"ok": True}) as tr:
            r = _post("/summary/run", {"date": DAY})
        self.assertTrue(r.json()["ok"])
        self.assertEqual(tr.call_count, 1)

    def test_soft_deleted_marks_not_counted(self):
        ok, payload = marks.add_highlight("report", SID, "划了又不要了", note="")
        self.assertTrue(ok, payload)
        Highlight.objects.filter(id=payload["id"]).update(
            deleted_at=timezone.now())
        self.assertEqual(marks.mark_counts("report", SID)["total"], 0)
        with mock.patch("apps.summary.views.summary.trigger_async",
                        return_value={"ok": True}) as tr:
            r = self._hit()
        self.assertTrue(r.json()["ok"])
        self.assertEqual(tr.call_count, 1)

    def test_counts_and_wording_for_both_kinds(self):
        marks.add_highlight("report", SID, "一", note="")
        marks.add_highlight("report", SID, "二", note="")
        ok, payload = marks.add_annotation("report", SID, "我的判断")
        self.assertTrue(ok, payload)
        w = marks.rerun_warning("report", SID, what="x 的日报")
        self.assertEqual((w["highlights"], w["annotations"], w["total"]), (2, 1, 3))
        self.assertIn("2 条划线", w["msg"])
        self.assertIn("1 条批注", w["msg"])
        # 落不了位 ≠ 被删除：这句必须在，否则用户以为数据没了
        self.assertIn("不会丢", w["msg"])
        self.assertEqual(Annotation.objects.live().count(), 1)

    def test_weekly_same_guard(self):
        marks.add_highlight("report", WSID, "周报上划的", note="")
        with mock.patch("apps.summary.views.wk.run") as run, \
                mock.patch("threading.Thread", _InlineThread):
            r = _post("/summary/weekly", {"week_end": WEEK, "force": "1"})
            self.assertTrue(r.json()["need_confirm"])
            self.assertEqual(run.call_count, 0)
            r2 = _post("/summary/weekly",
                       {"week_end": WEEK, "force": "1", "confirm": "1"})
            self.assertTrue(r2.json()["ok"])
            self.assertEqual(run.call_count, 1)
