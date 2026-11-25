"""第十一步：`compile_run` 一条龙的反馈口径与 `_ctx` 计数。

用户看不到提炼过程，所以「花完钱那句话」是**唯一的可见出口**：少报一处
没提炼成的来源、少报被守卫丢掉的候选，都等于静默。这里逐条钉死。

打桩纪律：`extract_then_compile` / `compile_candidates` 都打桩（零模型调用），
但配「桩真被用上」的反证 —— 否则「没调 compile_candidates」也可能是链路
压根没通。断言只看响应里**人真的看得到的文字**。
"""
import shutil
import tempfile
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from core import ingest
from core import wiki as wstore
from core.models import Annotation

OK = {"ok": True, "err": "", "drafts": ["page-one"], "groups": [],
      "dropped": [], "skipped": 0, "extracted": 2,
      "extract_dropped": 0, "extract_skipped": []}


class ReportBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-report-")
        cls.ov = override_settings(WIKI_DIR=cls.tmp)
        cls.ov.enable()

    @classmethod
    def tearDownClass(cls):
        cls.ov.disable()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        wstore.ensure_dirs()

    def post_compile(self, res=None, **data):
        payload = dict(OK if res is None else res)
        with patch("core.ingest.extract_then_compile", return_value=payload) as fn:
            resp = self.client.post(reverse("wiki_compile"), data)
            return resp, fn

    def text(self, resp):
        return resp.content.decode("utf-8", "replace")


class TestOneShotDispatch(ReportBase):
    def test_home_button_goes_through_oneshot(self):
        """首页那枚按钮（不带 source）= 提炼 + 编译一条龙，不走旧的只编译路径。"""
        resp, fn = self.post_compile()
        self.assertEqual(resp.status_code, 200)
        fn.assert_called_once_with()

    def test_stub_really_used(self):
        """反证：桩必须真被调，否则上一条的 assert_called_once 无意义。"""
        with patch("core.ingest.extract_then_compile",
                   return_value=dict(OK)) as fn:
            self.client.post(reverse("wiki_compile"), {})
            self.assertEqual(fn.call_count, 1)

    def test_oneshot_never_calls_compile_candidates(self):
        """★ 死锁坑：一条龙自己拿锁，再调 compile_candidates 会自己挡自己。"""
        with patch("core.ingest.extract_then_compile",
                   return_value=dict(OK)) as one, \
             patch("core.ingest.compile_candidates") as old:
            self.client.post(reverse("wiki_compile"), {})
            one.assert_called_once_with()
            old.assert_not_called()

    def test_source_scoped_call_still_works_in_code(self):
        """按来源编译保留在代码层（脚本/测试可用），且不谎报「提炼了 N 处」。"""
        res = {k: v for k, v in OK.items() if k not in ("extracted",)}
        with patch("core.ingest.compile_candidates", return_value=res) as old, \
             patch("core.ingest.extract_then_compile") as one:
            resp = self.client.post(reverse("wiki_compile"),
                                    {"source_type": "entry", "source_id": "7101"})
            old.assert_called_once_with(source=("entry", "7101"))
            one.assert_not_called()
        t = self.text(resp)
        self.assertIn("生成了 1 页草稿", t)
        self.assertNotIn("先提炼了", t)


class TestReportHonesty(ReportBase):
    def test_reports_both_extract_and_compile(self):
        t = self.text(self.post_compile()[0])
        self.assertIn("先提炼了 2 处新痕迹", t)
        self.assertIn("生成了 1 页草稿", t)
        self.assertIn("page-one", t)

    def test_extract_dropped_is_not_silent(self):
        res = dict(OK, extract_dropped=3)
        t = self.text(self.post_compile(res)[0])
        self.assertIn("3 条候选被引文守卫丢弃", t)

    def test_extract_skipped_names_the_source(self):
        res = dict(OK, extract_skipped=[
            {"source_id": "7101", "title": "某篇长文", "err": "字数不够"},
            {"source_id": "7102", "title": "另一篇", "err": "预算用尽"}])
        t = self.text(self.post_compile(res)[0])
        self.assertIn("2 处没提炼成", t)
        self.assertIn("某篇长文", t)
        self.assertIn("字数不够", t)

    def test_many_skipped_says_how_many(self):
        res = dict(OK, extract_skipped=[
            {"source_id": str(i), "title": "篇%d" % i, "err": "失败"}
            for i in range(6)])
        t = self.text(self.post_compile(res)[0])
        self.assertIn("6 处没提炼成", t)
        self.assertIn("等 6 处", t)
        self.assertIn("篇0", t)
        self.assertNotIn("篇5（失败）", t)  # 只列前三条，别把反馈撑爆

    def test_no_drafts_still_reports_extract(self):
        res = dict(OK, drafts=[], msg="没有新内容需要编译")
        t = self.text(self.post_compile(res)[0])
        self.assertIn("先提炼了 2 处新痕迹", t)
        self.assertIn("没有新内容需要编译", t)
        self.assertNotIn("is-bad", t)  # 合法结果，不是失败

    def test_failure_is_visible_and_marked_bad(self):
        res = dict(OK, ok=False, err="上一次编译还在跑")
        with patch("core.ingest.extract_then_compile", return_value=res):
            resp = self.client.post(reverse("wiki_compile"), {})
        t = self.text(resp)
        self.assertIn("编译未完成：上一次编译还在跑", t)
        self.assertIn("is-bad", t)

    def test_dropped_candidates_are_reported(self):
        res = dict(OK, dropped=[{"cand_id": 1, "why": "引文对不上"},
                                {"cand_id": 2, "why": "引文对不上"}])
        t = self.text(self.post_compile(res)[0])
        self.assertIn("另有 2 条被守卫丢弃", t)
        self.assertIn("引文对不上 ×2", t)

    def test_extract_left_is_reported(self):
        """★ 一次最多提炼 EXTRACT_MAX_SOURCES 个来源，没轮到的必须说出来。

        2026-11-09 清零重来后真实库 33 个来源，上限 30 是**隐含**在
        `inventory()` 默认 limit 里的，一个字都不报 —— 人会以为「跑完了，
        剩 3 处大概是没内容的」。这句人话是唯一出口。
        """
        res = dict(OK, extract_left=3)
        t = self.text(self.post_compile(res)[0])
        self.assertIn("还有 3 处没轮到", t)
        self.assertIn("最多提炼 %d 个来源" % ingest.EXTRACT_MAX_SOURCES, t)

    def test_no_leftover_no_noise(self):
        """反证：没有遗留时不许冒这句，否则人天天读一句无意义的提示。"""
        t = self.text(self.post_compile(dict(OK, extract_left=0))[0])
        self.assertNotIn("没轮到", t)

    def test_truncated_extract_is_reported(self):
        """★ 提炼输出被预算截断的来源，必须单独说一句（2026-11-09）。

        它既不在 `extracted` 里（`extract()` 截断时**不记台账**，下次要重提炼），
        也不在 `extract_skipped` 里（那不是失败）—— 不单独报出来，人就只看到
        「新痕迹还在、这一轮什么都没变」，真实原因是那篇正文太长、JSON 被切半截。
        真实数据：30 次提炼里有 1 次 i=3214 / out=4096 / 27.7 s / ¥0.0399。
        """
        t = self.text(self.post_compile(dict(OK, extract_cut=1))[0])
        self.assertIn("1 处正文太长", t)
        self.assertIn("这次的内容不算数", t)

    def test_no_truncation_no_noise(self):
        """反证：没截断时不许冒这句。"""
        t = self.text(self.post_compile(dict(OK, extract_cut=0))[0])
        self.assertNotIn("正文太长", t)


class TestStatusCounts(ReportBase):
    def test_new_trace_count_comes_from_inventory(self):
        Annotation.objects.create(source_type="entry", source_id="8101",
                                  body="我认为这条值得沉淀")
        Annotation.objects.create(source_type="entry", source_id="8102",
                                  body="另一条也值得")
        from apps.wiki import views
        from django.test import RequestFactory
        req = RequestFactory().get("/wiki")
        ctx = views._ctx(req)
        self.assertEqual(ctx["new_trace_count"], len(
            [r for r in ingest.inventory() if r["state"] in ("new", "changed")]))
        self.assertGreaterEqual(ctx["new_trace_count"], 2)

    def test_pending_decision_excludes_soft_trace_hint(self):
        """硬待办 = 待审批草稿 + 断链待决；新痕迹是软提示，不该混进去。"""
        from apps.wiki import views
        from django.test import RequestFactory
        Annotation.objects.create(source_type="entry", source_id="8101",
                                  body="我认为这条值得沉淀")
        req = RequestFactory().get("/wiki")
        ctx = views._ctx(req)
        self.assertEqual(ctx["pending_decision"],
                         ctx["stats"]["drafts"] + len(ctx["relink_ask"]["ask"]))

    def test_index_renders_status_counts(self):
        """计数必须真的出现在页面上，不能只活在 ctx 字典里。"""
        Annotation.objects.create(source_type="entry", source_id="8101",
                                  body="我认为这条值得沉淀")
        resp = self.client.get(reverse("wiki_index"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("处新痕迹", self.text(resp))

    def test_new_trace_count_not_capped_by_inventory_default(self):
        """★★ 「还有多少活」不许被表格行数截断（2026-11-09 清零后暴露）。

        真相：`inventory()` 原来默认 `limit=30`，而 `_ctx` 拿它的**行数**当计数。
        来源超过 30（清零重来后真实库 33 个）时面板少报成「30 处」，还看不出被截。
        这里造 33 个来源：计数必须 33，页面必须写 33，且 `inventory(limit=30)`
        仍然只回 30 行 —— 三条一起证明「限行」与「计数」是两件事。
        """
        for i in range(33):
            Annotation.objects.create(source_type="entry", source_id=str(9100 + i),
                                      body="第 %d 条我认为值得沉淀" % i)
        from apps.wiki import views
        from django.test import RequestFactory
        ctx = views._ctx(RequestFactory().get("/wiki"))
        self.assertEqual(ctx["new_trace_count"], 33,
                         "计数被截断了：%r" % ctx["new_trace_count"])
        self.assertEqual(len(ingest.inventory(limit=30)), 30, "limit 仍要能限行")
        self.assertEqual(len(ingest.inventory(limit=None)), 33, "limit=None = 全量")
        resp = self.client.get(reverse("wiki_index"))
        self.assertIn("33 处新痕迹", self.text(resp))
