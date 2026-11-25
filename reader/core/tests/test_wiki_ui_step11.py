"""第十一步：首页重排 + 互斥禁用态 + 只读候选查询页的渲染契约。

单测盯的是「源码事实」，真浏览器 e2e 另验「页面没脏东西」。
★ 每条结构断言都配一条**可见文字**断言，避免只验 DOM 骨架。
"""
import re
import shutil
import tempfile
from unittest.mock import patch

from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from core import ingest
from core import wiki as wstore
from core.models import IngestCandidate

GUARD_WORD = "不能审批或编辑"


def mk_cand(claim="我认为这条值得沉淀", sid="9101", status="pending"):
    return IngestCandidate.objects.create(
        batch="b1", source_type="entry", source_id=sid, kind="decision",
        claim=claim, quote="我的原话：值得沉淀", quote_from="mine",
        domain="invest", confidence="high", status=status)


def plain(html):
    """剥标签后的可见文字（不追求精确排版，只验关键话有没有出现在屏幕上）。"""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


class UiBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-ui11-")
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
        self.fd = None

    def tearDown(self):
        if self.fd is not None:
            ingest._unlock_compile(self.fd)
            self.fd = None
        ingest._clear_state()

    def running(self):
        self.fd = ingest._try_lock_compile()
        assert self.fd is not None
        ingest._mark_running(phase="compile", n=1, skipped=0, source="")


class TestHomeLayout(UiBase):
    def test_single_root_node(self):
        """★ 双重 #wiki-body 会让 htmx 的 outerHTML 替换结构漂移（本步顺手修）。"""
        html = self.client.get(reverse("wiki_index")).content.decode()
        self.assertEqual(html.count('id="wiki-body"'), 1)
        # 空闲且没活时编译**表单**不渲染（换成禁用按钮），但入口必须始终在：
        # 这是 2026-11-05「编译按钮静默消失」那次事故留下的契约。
        self.assertIn('id="compile"', html)
        self.assertIn('编译成草稿', html)

    def test_product_before_machine(self):
        """首页第一眼 = 状态行 + 已入库页表；工作台/草稿卡已下线（2026-11-09）。"""
        html = self.client.get(reverse("wiki_index")).content.decode()
        i_status = html.find('id="compile"')
        i_pages = html.find("<b>已入库 ")
        self.assertGreater(i_status, 0)
        self.assertGreater(i_pages, 0)
        self.assertLess(i_status, i_pages, "状态行必须在页表之前")
        # 裁决唯一入口是审批浮层：首页不再有并行草稿卡 / 工作台
        self.assertNotIn('id="workbench"', html)
        self.assertNotIn('id="drafts"', html)
        self.assertNotIn("draft-card", html)
        self.assertNotIn('hx-post="/wiki/approve"', html)

    def test_pipeline_panels_are_gone(self):
        """#ingest / #cands 两个中间态面板已从首页下线。"""
        html = self.client.get(reverse("wiki_index")).content.decode()
        self.assertNotIn('id="ingest"', html)
        self.assertNotIn('id="cands"', html)
        self.assertNotIn("提炼候选", html, "提炼过程不该在首页出现")
        self.assertIn("候选查询", html)

    def test_status_counts_are_visible(self):
        html = self.client.get(reverse("wiki_index")).content.decode()
        self.assertIn("处新痕迹", html)
        self.assertIn("待你裁决", html)
        self.assertEqual(html.count('class="card-main"'), 0)  # 删除了统计卡片，避免重复显示

    def test_no_stale_roadmap_copy(self):
        """过时的「自动编译成页在阶段 2.3，还没做」必须消失。"""
        html = self.client.get(reverse("wiki_index")).content.decode()
        self.assertNotIn("还没做", html)
        self.assertIn("只有「编译」会调模型", html)


class TestMutexUi(UiBase):
    def test_buttons_disabled_and_reason_visible_when_running(self):
        """硬锁方向二的 UI：按钮真 disabled + 同一句人话可见。

        ★ 测试前置要自己造（第十步教训）：一个写按钮都不渲染时，
          「没找到 disabled」只是没样本，不是守卫没生效。
        ★ 样本换成「已处理候选」区的改判按钮（2026-11-09）：首页草稿卡已下线，
          那里不再有可禁用的写按钮。
        """
        mk_cand(status="discarded")
        self.running()
        html = self.client.get(reverse("wiki_index")).content.decode()
        self.assertIn("mutex-bar", html)
        self.assertIn(GUARD_WORD, plain(html))
        self.assertIn('disabled', html)
        self.assertIn('title="上一次编译还在跑', html)

    def test_no_disabled_markup_when_idle(self):
        """反证：禁用态不是模板里恒有的字。"""
        html = self.client.get(reverse("wiki_index")).content.decode()
        self.assertNotIn("mutex-bar", html)
        self.assertNotIn(GUARD_WORD, plain(html))

    def test_page_detail_also_shows_reason(self):
        """详情页的「拉入审批编辑 / 提交删除请求」同口径，不漏一处。"""
        self.running()
        from apps.wiki import views
        ctx = views.page(RequestFactory().get("/wiki/page", {"p": "wiki/invest/x.md"}))
        # page() 返回 HttpResponse；取内容验可见文字
        self.assertIn(GUARD_WORD, plain(ctx.content.decode()))

    def test_approval_overlay_also_shows_reason(self):
        self.running()
        html = self.client.get(reverse("wiki_approval")).content.decode()
        self.assertIn(GUARD_WORD, plain(html))


class TestCandidatesPage(UiBase):
    def test_page_is_read_only(self):
        """查询页纯查询（2026-11-09 用户拍板）：连丢弃/改判表单都没有。

        候选提炼出来马上就会被编译进草稿，丢弃按钮几乎永远被产物守卫禁用 ——
        摆一个永远灰着的按钮不如不摆。要排除某条，去审批浮层让助手移出分组。
        """
        mk_cand()
        html = self.client.get(reverse("wiki_candidates")).content.decode()
        self.assertEqual(self.client.get(reverse("wiki_candidates")).status_code, 200)
        self.assertIn("候选查询", html)
        self.assertIn("我认为这条值得沉淀", html)
        self.assertNotIn('value="accepted"', html)
        self.assertNotIn('hx-post="/wiki/compile"', html)
        self.assertNotIn('hx-post="/wiki/ingest"', html)
        self.assertNotIn('action="/wiki/candidate"', html)
        self.assertNotIn('name="from" value="candidates"', html)
        self.assertIn("不花一分钱", plain(html))

    def test_query_page_shows_all_candidates_beyond_old_cap(self):
        """★ 变异靶子（2026-11-09 真实库抓到）：默认 limit=60 静默截断。

        66 条 pending 时计数说 66、列表只画 60 ——「翻看全部候选」的承诺破了。
        查询页必须全量（limit=None），与 inventory 那次同一个坑。
        """
        for i in range(65):
            mk_cand("候选 %02d" % i, sid="91%02d" % i)
        html = self.client.get(reverse("wiki_candidates")).content.decode()
        self.assertEqual(html.count('class="cand-card"'), 65)
        self.assertIn("未丢弃候选 65", html)

    def test_discard_redirects_back_and_really_changes_status(self):
        """原生 POST → 重定向回查询页（不是 #wiki-body 片段），且状态真改。"""
        c = mk_cand()
        resp = self.client.post(reverse("wiki_candidate"),
                                {"from": "candidates", "id": str(c.id),
                                 "status": "discarded"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/wiki/candidates?msg=", resp["Location"])
        c.refresh_from_db()
        self.assertEqual(c.status, "discarded")
        # 刷新后仍能看到结果说明（不是一闪而过的 toast）
        html = self.client.get(resp["Location"]).content.decode()
        self.assertIn("已标记为", html)

    def test_home_htmx_path_unchanged(self):
        """首页那条 htmx 路径（不带 from）行为一字不动。"""
        c = mk_cand()
        resp = self.client.post(reverse("wiki_candidate"),
                                {"id": str(c.id), "status": "discarded"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('id="wiki-body"', resp.content.decode())
        c.refresh_from_db()
        self.assertEqual(c.status, "discarded")

    def test_query_page_discard_blocked_during_compile(self):
        """互斥同样覆盖查询页：被挡时重定向带 bad=1 与原因，且不改库。"""
        c = mk_cand()
        self.running()
        resp = self.client.post(reverse("wiki_candidate"),
                                {"from": "candidates", "id": str(c.id),
                                 "status": "discarded"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("bad=1", resp["Location"])
        self.assertIn("%E4%B8%8D%E8%83%BD", resp["Location"])  # 「不能」被 quote
        c.refresh_from_db()
        self.assertEqual(c.status, "pending")
        html = self.client.get(resp["Location"]).content.decode()
        self.assertIn(GUARD_WORD, plain(html))


def write_draft(slug, cand_ids):
    (wstore.root() / ".drafts" / ("%s.md" % slug)).write_text(
        "---\ntitle: %s\ndomain: invest\nsources: [entry:9101]\n"
        "updated: 2026-11-09\nconfidence: medium\ntags: []\n"
        "candidates: [%s]\n---\n\n正文\n" % (slug, ", ".join(str(i) for i in cand_ids)),
        encoding="utf-8")


class TestCompiledDiscardGuard(UiBase):
    """已编进草稿/页的候选，查询页不能丢弃（2026-11-09）。

    set_status 只改 DB：直接丢弃会让草稿 frontmatter 挂着幽灵 id，审批照样能过、
    入库后溯源区多一条「已不存在」。正确路径是审批浮层移出分组（回池再丢弃）。
    """

    def test_discard_blocked_when_candidate_in_draft(self):
        c = mk_cand()
        write_draft("has-cand", [c.id])
        resp = self.client.post(reverse("wiki_candidate"),
                                {"from": "candidates", "id": str(c.id),
                                 "status": "discarded"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("bad=1", resp["Location"])
        c.refresh_from_db()
        self.assertEqual(c.status, "pending", "被拒就不该改库")
        html = self.client.get(resp["Location"]).content.decode()
        self.assertIn("审批浮层", plain(html), "要告诉人该去哪操作")

    def test_discard_blocked_when_candidate_in_page(self):
        c = mk_cand()
        d = wstore.root() / "wiki" / "invest"
        d.mkdir(parents=True, exist_ok=True)
        (d / "p.md").write_text(
            "---\ntitle: p\ndomain: invest\nsources: [entry:9101]\n"
            "updated: 2026-11-09\nconfidence: medium\ntags: []\n"
            "candidates: [%d]\n---\n\n正文\n" % c.id, encoding="utf-8")
        ok, err = ingest.set_status(c.id, "discarded")
        self.assertFalse(ok)
        self.assertIn("拉入审批编辑", err)
        c.refresh_from_db()
        self.assertEqual(c.status, "pending")

    def test_home_htmx_path_also_blocked(self):
        """守卫在 core 的 set_status，两条 UI 路径同挡，不是只挡查询页。"""
        c = mk_cand()
        write_draft("has-cand", [c.id])
        resp = self.client.post(reverse("wiki_candidate"),
                                {"id": str(c.id), "status": "discarded"})
        self.assertEqual(resp.status_code, 200)
        c.refresh_from_db()
        self.assertEqual(c.status, "pending")

    def test_discard_allowed_after_draft_archived(self):
        """退稿（.discarded/）不算「已编译」：归档不占位，改判后的候选还能丢弃/重编。"""
        c = mk_cand()
        arch = wstore.root() / ".drafts" / ".discarded"
        arch.mkdir(parents=True, exist_ok=True)
        (arch / "old.md").write_text(
            "---\ntitle: old\ndomain: invest\nsources: [entry:9101]\n"
            "updated: 2026-11-09\nconfidence: medium\ntags: []\n"
            "candidates: [%d]\n---\n\n正文\n" % c.id, encoding="utf-8")
        self.assertTrue(ingest.set_status(c.id, "discarded")[0])

    def test_query_page_marks_compiled_candidates(self):
        """查询页：已进草稿的候选打标；计数说真待编译数；整页无任何操作表单。"""
        c1, c2 = mk_cand("已编的"), mk_cand("没编的", sid="9102")
        write_draft("has-cand", [c1.id])
        html = self.client.get(reverse("wiki_candidates")).content.decode()
        self.assertIn("已进草稿/页", html)
        self.assertIn("1 条待编译", html)
        self.assertIn("未丢弃候选 2", html)
        self.assertNotIn('action="/wiki/candidate"', html, "纯查询：丢弃/改判都不在UI上")
        self.assertNotIn("回池后再丢弃", html, "没有按钮了，就不该再留按钮的说明文案")


class TestApprovalDiscard(UiBase):
    """退稿唯一 UI 入口在审批浮层（from=approval）：成功/失败两种响应。

    ★ 2026-11-13 改契约：成功也不整页刷新 —— HX-Refresh 会把人正在用的
      浮层一起关掉，而退完一份通常还要接着审下一份。
    """

    def test_discard_from_approval_stays_in_overlay_and_archives(self):
        write_draft("one", [])
        resp = self.client.post(reverse("wiki_discard"),
                                {"from": "approval", "slug": "one", "reason": "不要这版"})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("HX-Refresh", resp, "整页刷新会把浮层关掉")
        self.assertIn('id="approval-workarea"', resp.content.decode())
        self.assertFalse((wstore.root() / ".drafts" / "one.md").exists())
        self.assertEqual(
            len(list((wstore.root() / ".drafts" / ".discarded").glob("one*.md"))), 1)

    def test_discard_failure_stays_in_workarea_with_reason(self):
        resp = self.client.post(reverse("wiki_discard"),
                                {"from": "approval", "slug": "no-such", "reason": ""})
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('id="approval-workarea"', html)
        self.assertIn("退稿未生效", html)
        self.assertNotIn("HX-Refresh", resp)

    def test_discard_blocked_during_compile_with_visible_reason(self):
        write_draft("one", [])
        self.running()
        resp = self.client.post(reverse("wiki_discard"),
                                {"from": "approval", "slug": "one", "reason": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(GUARD_WORD, plain(resp.content.decode()))
        self.assertTrue((wstore.root() / ".drafts" / "one.md").exists())
