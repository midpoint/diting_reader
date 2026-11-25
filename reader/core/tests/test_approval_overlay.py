"""第五步 5a 切片验收：只读审批浮层（纯前端展示、零花钱）。

★ 第十三步（形态 A）口径：正文 = 纯 polish 一段，`polish_html` / `polish_empty`
  双分支整套退役，右栏只显示正文；正文留空 = 结论还没生成，给「结论待重生成」
  可见说明；分组变更的标记写在 frontmatter `stale: true`（不再插进正文），
  浮层据此给「分组已由审批对话调整…结论还没重编成功」可见说明。
  frontmatter `candidates` 是分组唯一真值，左栏候选明细才是真相。
  ★ 造数用的 `mk_draft` 默认正文是一份清零前老格式草稿（读侧兼容用，另一个
    测试文件也 import 了这个基类，不要动它）；形态 A 新用例一律用 `body=`
    显式给纯 polish / 空正文。

铁律口径：
  - **只读守卫**：浮层相关视图若误调 `wa.set` / `wiki_loop.run`（写/loop 入口）或
    `wiki_loop._call` / `citeguard._call`（LLM 出口），测试变红 —— 桩在**被调用点的
    命名空间**（`core.wiki_agent.set` / `core.wiki_loop.run`），`assert_not_called`。
  - **自动弹出只在有产物时**：编译 `drafts` 非空才发 `wikiApprovalOpen`，无产物不发。
  - **页切换不整页刷新**：`approval_draft` 返回片段根节点 `id="approval-workarea"`。
  - `WIKI_DIR` 一律 override 到临时目录，`IngestCandidate` 落测试库，不动真实库。
  - 断言只看可见文字 / 真实产物（按 innerText 口径，先剥标签）。
"""
import json
import re
import shutil
import tempfile
from unittest.mock import patch

from django.test import Client, TestCase, override_settings

from core import citeguard
from core import ingest
from core import wiki as wstore
from core import wiki_agent as wa
from core import wiki_loop as wl
from core.models import IngestCandidate

DOMAIN = "invest"


def plain(html):
    """剥标签后的可见文字（模板断言按 innerText 口径，只看人眼能读到的字）。"""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


class ApprovalOverlayBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-approval-overlay-")
        cls.ov = override_settings(WIKI_DIR=cls.tmp)
        cls.ov.enable()

    @classmethod
    def tearDownClass(cls):
        cls.ov.disable()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        # ★ 每个用例都要空的 wiki 目录：草稿是磁盘文件，不随事务回滚
        shutil.rmtree(self.tmp, ignore_errors=True)
        wstore.ensure_dirs()

    def mk_cand(self, n, claim=None, quote=None, published_at=None):
        return IngestCandidate.objects.create(
            batch="t", source_type="entry", source_id="123", kind="decision",
            claim=claim or f"判断{n}", quote=quote or f"我的原话{n}",
            domain=DOMAIN, confidence="medium", status="pending",
            published_at=published_at)

    def mk_draft(self, slug, ids, body=None, title=None):
        text = ("---\n"
                f"title: {title or slug}\n"
                f"domain: {DOMAIN}\n"
                "sources: [entry:123]\n"
                "updated: 2026-11-05\n"
                "confidence: medium\n"
                "tags: []\n"
                f"candidates: [{', '.join(str(i) for i in ids)}]\n"
                "---\n\n"
                + (body if body is not None
                   else "一条判断的结论。\n"))
        (wstore.root() / ".drafts" / f"{slug}.md").write_text(text, encoding="utf-8")
        return text


class ApprovalOverlayViewTest(ApprovalOverlayBase):
    """§三 验收：浮层壳 + 单页双栏 + 空值处理 + 不整页。"""

    def test_shell_renders_list_and_workarea(self):
        c = self.mk_cand(1, claim="买点判断")
        self.mk_draft("p1", [c.id], title="测试页")
        resp = self.client.get("/wiki/approval")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("审批浮层", html)          # 浮层壳标题
        self.assertIn("测试页", html)            # ① draft 列表
        self.assertIn("买点判断", html)          # ② 左栏候选 claim
        self.assertIn('id="approval-workarea"', html)
        self.assertNotIn("<html", html, "openModal 拉的是片段，不能包整页 html")

    def test_draft_fragment_has_workarea_root_and_both_columns(self):
        c = self.mk_cand(1, claim="买点判断", quote="原话原文")
        self.mk_draft("p1", [c.id], title="测试页")
        resp = self.client.get("/wiki/approval/draft?slug=p1")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("买点判断", html)          # 左栏 claim
        self.assertIn("原话原文", html)          # 左栏 quote
        self.assertIn("一条判断的结论。", html)     # 右栏正文（形态 A：纯一段）
        # ★ 反证：形态 A 的正文不许再出现旧双层 / 三块标题（否则「右栏正文」只是
        #   把老格式原样显示，断言照样绿 —— 那正是「假绿」最像绿的地方）。
        self.assertNotIn(wstore.BODY_HEAD, html)
        self.assertNotIn(wstore.POLISH_HEAD, html)
        self.assertNotIn("<html", html)
        # ★ 反证 3：片段根节点必须是 approval-workarea（htmx 换它，不整页刷新）
        self.assertTrue(html.lstrip().startswith('<section id="approval-workarea"'),
                        html[:120])

    def test_body_stale_shows_hint(self):
        """形态 A：分组变更标记写在 frontmatter `stale: true`（不再插进正文），
        浮层据此给「结论还没重编成功」的**可见说明**；剥掉标记说明必须消失
        （守卫自己会响的反证）。"""
        c = self.mk_cand(1)
        text = self.mk_draft("p1", [c.id], body="这是一段结论。")
        text = wstore._set_fm_line(text, wstore.STALE_FM, "true")
        p = wstore.root() / ".drafts" / "p1.md"
        p.write_text(text, encoding="utf-8")

        self.assertIn("%s: true" % wstore.STALE_FM, text)   # 标记在 frontmatter
        _, body = wstore.parse_front(text)
        self.assertNotIn(wstore.STALE_MARK, body)           # 正文仍是纯 polish
        self.assertTrue(wa.read("draft", "p1")["body_stale"])
        txt = plain(self.client.get("/wiki/approval/draft?slug=p1").content.decode())
        self.assertIn("分组已由审批对话调整", txt)
        self.assertIn("结论还没重编成功", txt)
        self.assertIn("当前真相是 frontmatter", txt)
        self.assertIn("这是一段结论。", txt)                 # 正文没被警告吞掉

        # 反证：把 stale 剥掉，可见说明应当消失（不是恒真的空集合）
        self.mk_draft("p1", [c.id], body="这是一段结论。")
        txt2 = plain(self.client.get("/wiki/approval/draft?slug=p1").content.decode())
        self.assertNotIn("结论还没重编成功", txt2)

    def test_published_at_none_renders_placeholder(self):
        c = self.mk_cand(1, published_at=None)
        self.mk_draft("p1", [c.id])
        resp = self.client.get("/wiki/approval/draft?slug=p1")
        self.assertIn("发布时间未知", resp.content.decode())

    def test_no_polish_shows_regenerate_hint(self):
        """形态 A：正文留空 = 结论还没生成，右栏给「结论待重生成」可见说明。
        旧的 `polish_html` / `polish_empty` 双分支已退役，不再有「通顺话」措辞。"""
        c = self.mk_cand(1)
        self.mk_draft("p1", [c.id], body="")
        resp = self.client.get("/wiki/approval/draft?slug=p1")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        txt = plain(html)
        self.assertIn("结论待重生成", txt)
        self.assertIn("这页还没有正文", txt)
        self.assertIn("左栏候选明细是分组的真相", txt)
        self.assertNotIn("通顺话", txt)                  # 旧双层产物已退役
        # frontmatter candidates 是分组唯一真值：左栏候选明细仍可见
        self.assertIn("判断1", txt)
        self.assertIn("我的原话1", txt)
        self.assertIn("我的判断", txt)                   # 按 quote_from 的分组标签
        self.assertIn('id="approval-workarea"', html)    # 片段根节点，不整页刷新

    def test_empty_drafts_shell_does_not_crash(self):
        resp = self.client.get("/wiki/approval")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("审批浮层", html)
        self.assertIn("还没有待审批草稿", html)
        self.assertIn("没有可预览的草稿", html)

    def test_unknown_slug_returns_error_fragment(self):
        resp = self.client.get("/wiki/approval/draft?slug=不存在")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("没有可预览的草稿", resp.content.decode())


class ApprovalReadOnlyGuardTest(ApprovalOverlayBase):
    """反证 1：浮层视图只读 —— 写入口 / LLM 出口从未被调用。"""

    def test_views_never_write_nor_call_llm(self):
        c = self.mk_cand(1)
        self.mk_draft("p1", [c.id])
        with patch.object(wa, "set") as mock_set, \
                patch.object(wl, "run") as mock_run, \
                patch.object(wl, "_call") as mock_call, \
                patch.object(citeguard, "_call") as mock_cite:
            self.client.get("/wiki/approval")
            self.client.get("/wiki/approval/draft?slug=p1")
        mock_set.assert_not_called()
        mock_run.assert_not_called()
        mock_call.assert_not_called()
        mock_cite.assert_not_called()


class CompileAutoOpenTest(ApprovalOverlayBase):
    """§三 验收 + 反证 2：编译有产物才自动弹浮层。"""

    def _compile_res(self, drafts):
        return {"ok": True, "drafts": drafts, "dropped": [], "groups": [],
                "noop": [], "notes": [], "skipped": 0, "collisions": []}

    def test_compile_with_drafts_triggers_approval_open(self):
        c = self.mk_cand(1)
        self.mk_draft("p1", [c.id])
        # ★ 第十一步：compile_run 改走一条龙，桩要打在**被调用点**上
        #   （打 compile_candidates 会漏；这正是「桩真被用上」的反证）。
        with patch.object(ingest, "extract_then_compile",
                          return_value=self._compile_res(["p1"])):
            resp = self.client.post("/wiki/compile")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("wikiApprovalOpen", resp.get("HX-Trigger", ""))

    def test_compile_without_drafts_does_not_trigger(self):
        with patch.object(ingest, "compile_candidates",
                          return_value=self._compile_res([])):
            resp = self.client.post("/wiki/compile")
        self.assertNotIn("wikiApprovalOpen", resp.get("HX-Trigger", ""))


class ApprovalRepolishButtonTest(ApprovalOverlayBase):
    """2026-11-22「重编结论」按钮：警告条里可点、点一次真花钱重编、失败原因可见。"""

    def _stale_draft(self, body=""):
        c = self.mk_cand(1)
        self.mk_draft("p1", [c.id], body=body)
        p = wstore.root() / ".drafts" / "p1.md"
        p.write_text(p.read_text(encoding="utf-8").replace(
            "confidence: medium", "confidence: medium\nstale: true"), encoding="utf-8")

    def test_stale_warning_has_clickable_repolish_button(self):
        self._stale_draft()
        with patch.object(wl, "_polish_one", return_value=(True, "")) as mock:
            resp = self.client.post("/wiki/approval/repolish", {"slug": "p1"})
        self.assertEqual(resp.status_code, 200)
        mock.assert_called_once_with("p1")
        html = resp.content.decode()
        self.assertIn('id="approval-workarea"', html, "换的是工作区，不是主页面")
        self.assertIn('id="approval-list"', html, "列表同步刷（完成态/选中态）")
        self.assertIn("结论已重编", json.loads(resp["HX-Trigger"])["wikiFlash"]["msg"])

        # ★ 真正入口的形状：stale 草稿的工作区里有表单，POST 指到这个端点、带 slug
        work = self.client.get("/wiki/approval/draft?slug=p1").content.decode()
        self.assertIn('hx-post="/wiki/approval/repolish"', work)
        self.assertIn('name="slug" value="p1"', work)
        self.assertIn("重编结论", work)

    def test_failure_reason_visible_in_workarea(self):
        self._stale_draft(body="旧结论保留。")
        with patch.object(wl, "_polish_one",
                          return_value=(False, "正文里引用了不存在的页：foo")):
            resp = self.client.post("/wiki/approval/repolish", {"slug": "p1"})
        html = resp.content.decode()
        self.assertIn("重编结论未能完成：", html)
        self.assertIn("foo", html, "失败原因必须留在页面上，不能只发 toast")
        self.assertIn("foo", json.loads(resp["HX-Trigger"])["wikiFlash"]["msg"])

    def test_mutex_blocks_repolish(self):
        self._stale_draft()
        with patch.object(ingest, "compile_state",
                          return_value={"running": True, "phase": "compile",
                                        "started": "16:00", "elapsed_text": "1 分钟"}), \
                patch.object(wl, "_polish_one") as mock:
            resp = self.client.post("/wiki/approval/repolish", {"slug": "p1"})
        mock.assert_not_called(), "互斥期不许花钱重编"
        self.assertIn("不能审批或编辑", resp.content.decode())

    def test_get_not_allowed(self):
        resp = self.client.get("/wiki/approval/repolish?slug=p1")
        self.assertEqual(resp.status_code, 405, "花钱端点只许 POST")
