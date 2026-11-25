"""候选免审（改动 A）的反证（2026-11-05）。

语义：**未丢弃即编译**。pending 与 accepted 一律进编译，逐条「采纳」不再是
前置动作；唯一的提前干预是 discarded（预排除，不是审批）。

★ 最贵的一种假绿：判据函数改对了，但页面上的编译按钮被 status 条件藏起来
  —— 页面 200、日志干净、点都没得点。所以这里既测函数、也测真渲染的页面。
"""
import shutil
import tempfile
from unittest import mock

from django.test import Client, TestCase, override_settings

from core import ingest
from core import wiki as wstore
from core.models import IngestCandidate

SID = "daily-2026-10-31"


def mk(claim, status="pending"):
    return IngestCandidate.objects.create(
        batch="b", source_type="report", source_id=SID, kind="decision",
        claim=claim, quote=claim, quote_from="mine", domain="invest",
        confidence="high", status=status)


class AutoApproveBase(TestCase):
    """草稿/页是磁盘文件，不随事务回滚；每个用例一个空 wiki 目录。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-autoapprove-")
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

    def html(self, query=""):
        return Client().get("/wiki" + query).content.decode("utf-8")


class BacklogJudgementTest(AutoApproveBase):
    """编译判据：pending 真进编译，discarded 真被排除。"""

    def test_pending_candidate_is_compilable(self):
        """★ 守卫会响：改动前 `filter(status="accepted")` 会把这条漏掉。"""
        p = mk("待审的判断")
        bl = ingest.compile_backlog()
        self.assertIn(p.id, [c.id for c in bl["cands"]])
        self.assertEqual(bl["todo"], 1)

    def test_discarded_candidate_is_excluded(self):
        d = mk("明确不要的", status="discarded")
        bl = ingest.compile_backlog()
        self.assertNotIn(d.id, [c.id for c in bl["cands"]])

    def test_accepted_still_compilable(self):
        a = mk("历史采纳的", status="accepted")
        self.assertIn(a.id, [c.id for c in ingest.compile_backlog()["cands"]])

    def test_compile_candidates_really_feeds_pending_to_the_paid_path(self):
        """★ 不只测判据返回值：花钱那条路（`compile_candidates`）也要真的吃到。

        打桩在 `plan_groups`（分组之前），一次模型调用都不发；`pg.call_args`
        非 None 才能证明「没在早退里被当成没活」。
        """
        p = mk("待审的判断")
        with mock.patch.object(ingest, "plan_groups",
                               return_value=([], "打桩：到此为止，不花钱", [])) as pg:
            res = ingest.compile_candidates()
        self.assertIsNotNone(pg.call_args, "旧实现会在这里早退，根本走不到分组")
        self.assertIn(p.id, [c.id for c in pg.call_args[0][0]])
        self.assertFalse(res["ok"])

    def test_list_candidates_default_means_not_discarded(self):
        p, a, d = mk("待审"), mk("采纳", "accepted"), mk("丢弃", "discarded")
        self.assertEqual({c.id for c in ingest.list_candidates()}, {p.id, a.id})
        self.assertNotIn(d.id, [c.id for c in ingest.list_candidates()])

    def test_explicit_status_still_filters(self):
        """显式传参的行为不许被默认值改动破坏（待审面板 / 已处理折叠区都靠它）。"""
        p, a = mk("待审"), mk("采纳", "accepted")
        self.assertEqual({c.id for c in ingest.list_candidates(status="pending")}, {p.id})
        self.assertEqual({c.id for c in ingest.list_candidates(status="accepted")}, {a.id})


class EndpointTest(AutoApproveBase):
    """端点 + 真渲染页面：只放行丢弃，编译入口不许被 status 条件藏掉。"""

    def test_endpoint_refuses_accepted_and_does_not_write(self):
        c = mk("待审的判断")
        r = Client().post("/wiki/candidate", {"id": c.id, "status": "accepted"})
        self.assertEqual(r.status_code, 200)
        c.refresh_from_db()
        self.assertEqual(c.status, "pending", "旧链接不许悄悄写库")
        self.assertIn("无需逐条采纳", r.content.decode("utf-8"))

    def test_endpoint_still_allows_discard(self):
        c = mk("不想要的")
        r = Client().post("/wiki/candidate", {"id": c.id, "status": "discarded"})
        self.assertEqual(r.status_code, 200)
        c.refresh_from_db()
        self.assertEqual(c.status, "discarded")

    def test_panel_has_no_accept_button(self):
        """查询页纯查询（2026-11-09 用户拍板）：采纳、丢弃按钮都不在页面上。

        候选提炼出来马上就会被编译进草稿，丢弃几乎永远被产物守卫禁用 ——
        不如不摆。要排除某条，去审批浮层让助手移出分组。
        """
        mk("待审的判断")
        html = Client().get("/wiki/candidates").content.decode("utf-8")
        self.assertNotIn('value="accepted"', html, "采纳按钮要真的从页面上消失")
        self.assertNotIn('value="discarded"', html, "丢弃按钮也下线（纯查询）")
        self.assertNotIn('action="/wiki/candidate"', html, "整页没有任何候选操作表单")

    def test_compile_entry_survives_when_every_candidate_is_pending(self):
        """★★ 定海神针：全是 pending、零 accepted、零已处理 ⇒ 编译入口必须还在。

        改动前编译按钮被包在「有已处理候选」+「accepted > 0」两层里，
        这两层全假 ⇒ 按钮静默消失（HTTP 200、日志干净）。这条专门挡它，
        附带的 `assertNotIn(disabled)` 证明按钮是**可点**的而不是灰着摆设。
        """
        mk("判断A")
        mk("判断B")
        html = self.html()
        self.assertIn('hx-post="/wiki/compile"', html, "编译入口不许被 status 条件藏掉")
        self.assertNotIn('type="button" disabled', html)
        self.assertIn("2 条待编译", html)

    def test_compile_entry_is_independent_of_processed_groups(self):
        """连一条候选都没有，编译面板也要渲染（灰着说明原因），不是整块消失。"""
        html = self.html()
        self.assertIn('type="button" disabled', html, "没活时灰着而不是消失")
        self.assertIn("还没有候选", html, "0 条候选时不许说「这 0 条都已变成页了」")
        self.assertNotIn("这 0 条", html)

    def test_s1_prompt_no_longer_claims_candidates_were_approved(self):
        """同口径两处必漏一处：判据改了、给模型的 prompt 还写「已被采纳」就是误导。"""
        self.assertNotIn("已被采纳", ingest.COMPILE_S1_SYSTEM)
        self.assertIn("待编译", ingest.COMPILE_S1_SYSTEM)
