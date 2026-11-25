"""审批对话后端原语 `read` / `set` 的守卫反证（第三步，2026-11-05）。

★ 这里只证明「守卫自己会响」，不证明「改完能跑」：每条都构造一个**故意踩线**
  的输入，断言被拒、且**文件一个字节都没动**。只写期望值不构造反例，守卫
  写坏了照样全绿（项目里付过这个学费）。

三条程序守卫（不靠 agent 自觉）：
  1. 锁 frontmatter：只有 title/domain/tags/candidates 可写；
  2. candidates 一致性：不许幽灵候选、不许重复归属、不许弄丢，拒写发生在落盘前；
  3. set 只改 draft 结构，从不 mv 进 wiki/（commit 是改动 F）。

★ `WIKI_DIR` 一律 override 到临时目录 —— 真实库的 `.drafts/` 里有一份旧草稿，
  直接在真实库上跑这些测试会把它改掉（读真实产物才算验证，但别拿真库当靶子）。
"""
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from core import conversation as conv_mod
from core import wiki as wstore
from core import wiki_agent as wa
from core.models import INGEST_LIVE_STATUS, Conversation, IngestCandidate

DOMAIN = "invest"


def _draft_text(ids, *, title="测试页", domain=DOMAIN, body=None) -> str:
    """一份字段齐全、**结论已生成**的草稿（形态 A：正文 = 纯 polish 一段）。

    ★ 与编译链 `_compose_page` 的差别只在「结论有没有生成」：`_compose_page`
      先产空正文 + `stale: true`，等 `wiki_loop._polish_one` 回填。测试要验的是
      回填之后的前台形态，所以这里直接给一段结论，**不带任何结构标题**
      （三块 / 候选清单 / 通顺话都已退役，带上就会被读侧判成老页）。
    """
    return ("---\n"
            f"title: {title}\n"
            f"domain: {domain}\n"
            "sources: [entry:123]\n"
            "updated: 2026-11-05\n"
            "confidence: medium\n"
            "tags: []\n"
            f"candidates: [{', '.join(str(i) for i in ids)}]\n"
            "---\n\n"
            + (body if body is not None
               else "一条判断，见 [文章《测试》](http://example.com)。\n"))


class WikiAgentBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-wiki-agent-")
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

    # ---- 造数 ---------------------------------------------------------
    def mk_cand(self, n, status="pending"):
        return IngestCandidate.objects.create(
            batch="t", source_type="entry", source_id="123", kind="decision",
            claim=f"判断{n}", quote=f"我的原话{n}", domain=DOMAIN,
            confidence="medium", status=status)

    def mk_draft(self, slug, ids, **kw):
        p = wstore.root() / ".drafts" / f"{slug}.md"
        p.write_text(_draft_text(ids, **kw), encoding="utf-8")
        return p

    def draft_ids(self, slug):
        text = (wstore.root() / ".drafts" / f"{slug}.md").read_text(encoding="utf-8")
        return wstore._cand_ids(wstore.parse_front(text)[0])

    def snapshot(self):
        """所有 .drafts/*.md 的字节，用来证明「拒写时一个字节都没动」。"""
        return {p.name: p.read_bytes() for p in
                sorted((wstore.root() / ".drafts").glob("*.md"))}


class DataModelTest(WikiAgentBase):
    def test_draftbatch_conversation_can_be_created(self):
        """改动 C 数据模型：审批对话复用 Conversation，不建新表。"""
        conv = wa.get_or_create_approval_conversation()
        self.assertIsNotNone(conv)
        conv.messages.create(role="user", body="第 3、5 条挪去新页")
        self.assertEqual(conv.messages.count(), 1)
        self.assertEqual(conv.pk, wa.get_or_create_approval_conversation().pk,
                         "刷新不该开出第二条审批对话（ORM 每次返回新实例，比主键）")
        self.assertEqual(Conversation.objects.filter(
            source_type="draftbatch", source_id="current").count(), 1)

    def test_conversation_helpers_reject_draftbatch_as_a_content_block(self):
        """★ 语义边界：审批对话不是内容块，`validate_source` 明确拒挂痕迹。

        不写这条，将来有人「顺手补个分支」放行 draftbatch，就能给审批对话
        加划线，而 source_id="current" 指向一个不存在的正文 —— 静默的脏数据。
        """
        from core import marks
        _, _, err = marks.validate_source("draftbatch", "current")
        self.assertIn("不是内容块", err)
        conv, err2 = conv_mod.get_or_create("draftbatch", "current")
        self.assertIsNone(conv)
        self.assertIn("不是内容块", err2)

    def test_clear_approval_conversation(self):
        conv = wa.get_or_create_approval_conversation()
        conv.messages.create(role="user", body="一句话")
        self.assertTrue(wa.clear_approval_conversation())
        self.assertIsNone(wa.approval_conversation())

    def test_same_source_id_is_unique(self):
        """固定 source_id="current" + 唯一约束 ⇒ 同时刻只有一条审批对话。"""
        Conversation.objects.create(source_type="draftbatch", source_id="current")
        # ★ atomic 包一层：IntegrityError 会把 TestCase 的外层事务标记为坏掉，
        #   不包的话后面的断言会炸 TransactionManagementError（假红）。
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Conversation.objects.create(source_type="draftbatch",
                                            source_id="current")

    def test_approval_conversation_is_not_the_pause_judgement(self):
        """★ 判据澄清：`approval_active()` 看**文件**，不看 DB 里有没有这条对话。

        对话建了但 .drafts/ 空 ⇒ 不算审批中。若拿 DB 判，审批结束后对话
        还在（留着回看），编译/提炼会被永久锁死，而且日志一行不红。
        """
        Conversation.objects.create(source_type="draftbatch", source_id="current")
        self.assertIsNotNone(wa.approval_conversation())
        self.assertFalse(wstore.approval_active())


class ReadTest(WikiAgentBase):
    def test_read_drafts_returns_grouping_summary(self):
        a, b = self.mk_cand(1), self.mk_cand(2)
        self.mk_draft("page-a", [a.id])
        self.mk_draft("page-b", [b.id])
        res = wa.read("drafts")
        self.assertTrue(res["ok"])
        self.assertEqual(res["count"], 2)
        self.assertTrue(res["approval_active"])
        self.assertEqual({d["slug"] for d in res["drafts"]}, {"page-a", "page-b"})

    def test_read_draft_returns_real_candidate_fields(self):
        """read 必须给 agent 真实字段（quote/claim/发布时间/出处），否则它只能靠标题猜分组。"""
        c = self.mk_cand(1)
        self.mk_draft("page-a", [c.id])
        res = wa.read("draft", "page-a")
        self.assertTrue(res["ok"])
        self.assertEqual(res["candidates"], [c.id])
        self.assertEqual(res["candidate_detail"][0]["quote"], "我的原话1")
        self.assertEqual(res["candidate_detail"][0]["source"], "entry:123")
        self.assertFalse(res["body_stale"])

    def test_read_draft_flags_unknown_candidate(self):
        """幽灵候选要**点名**，不能静默略过 —— 否则 agent 看不到分组已经坏了。"""
        self.mk_draft("page-a", [999999])
        res = wa.read("draft", "page-a")
        self.assertEqual(res["unknown_candidates"], [999999])

    def test_read_candidates_excludes_discarded_and_shares_the_judgement(self):
        """read(candidates) 与 compile_backlog 必须同一份 `INGEST_LIVE_STATUS`。"""
        a = self.mk_cand(1, "pending")
        self.mk_cand(2, "accepted")
        self.mk_cand(3, "discarded")
        res = wa.read("candidates")
        self.assertEqual(res["count"], 3 - 1)
        self.assertEqual(res["status_filter"], list(INGEST_LIVE_STATUS))
        self.assertNotIn("discarded", [c["status"] for c in res["candidates"]])
        self.assertEqual(res["candidates"][0]["cand_id"], a.id)

    def test_read_page_from_wiki_dir(self):
        p = wstore.root() / "wiki" / DOMAIN / "old.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_draft_text([1]), encoding="utf-8")
        res = wa.read("page", "old")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["page"], f"wiki/{DOMAIN}/old.md")

    def test_unknown_scope_is_a_readable_error(self):
        res = wa.read("whatever")
        self.assertFalse(res["ok"])
        self.assertIn("未知的 scope", res["err"])


class SetActionsTest(WikiAgentBase):
    def test_split_moves_candidates_without_orphan(self):
        """拆页：候选从旧页消失、在新页出现，并集守恒。"""
        a, b, c = self.mk_cand(1), self.mk_cand(2), self.mk_cand(3)
        self.mk_draft("old", [a.id, b.id, c.id])
        ok, payload = wa.set("candidates", {
            "move": [b.id, c.id], "from": "old", "to": "new",
            "title": "端侧 AI 成本"})
        self.assertTrue(ok, payload)
        self.assertEqual(self.draft_ids("old"), [a.id])
        self.assertEqual(self.draft_ids("new"), [b.id, c.id])
        self.assertIn("new", payload["written"])

    def test_split_into_existing_page_appends(self):
        """目标是已存在页 = 并入（追加），不是静默覆盖。"""
        a, b, c = self.mk_cand(1), self.mk_cand(2), self.mk_cand(3)
        self.mk_draft("old", [a.id, b.id])
        self.mk_draft("other", [c.id])
        ok, payload = wa.set("candidates", {
            "move": [b.id], "from": "old", "to": "other"})
        self.assertTrue(ok, payload)
        self.assertEqual(self.draft_ids("other"), [c.id, b.id])

    def test_split_all_out_archives_source_draft(self):
        """★ 候选全移走 = 来源页归档，不留缺 candidates 的空壳
        （2026-11-11 实测：空壳在浮层报「缺 frontmatter 字段：candidates」、
        还占着草稿计数）。归档进 .discarded/ 留证据，审批标记一起清掉；
        其他草稿正文里对它的 [[wikilink]] 解包成标题/别名纯文本，不留死链。"""
        a, b = self.mk_cand(1), self.mk_cand(2)
        self.mk_draft("old", [a.id], title="旧页的标题")
        self.mk_draft("other", [b.id])
        self.mk_draft("third", [self.mk_cand(3).id],
                      body="见 [[old]] 与 [[old|别名]]。\n")
        wstore.toggle_approved("old")
        ok, payload = wa.set("candidates", {
            "move": [a.id], "from": "old", "to": "other"})
        self.assertTrue(ok, payload)
        self.assertEqual(self.draft_ids("other"), [b.id, a.id])
        self.assertFalse((wstore.root() / ".drafts" / "old.md").exists(),
                         "来源草稿必须移除，不能留空壳")
        self.assertTrue(any(
            (wstore.root() / ".drafts" / ".discarded").glob("old-*.md")),
            "归档进 .discarded/ 留证据")
        self.assertIn("old", payload["archived"])
        self.assertNotIn("old", wstore.read_approved(),
                         "归档页的审批标记要一起清掉")
        text = (wstore.root() / ".drafts" / "third.md").read_text(encoding="utf-8")
        self.assertNotIn("[[old", text, "对已归档页的 wikilink 必须解包，不留死链")
        self.assertIn("见 旧页的标题 与 别名。", text,
                      "无别名解包成页标题（不是裸 slug），有别名解包成别名")

    def test_split_same_source_and_target_is_rejected(self):
        """★ 同页移动（X→X）必须被拒：它是 no-op，还会踩「来源候选清空→归档」
        分支把真页归档（2026-11-11 实测：模型想靠伪造 X→X 触发重新 polish，
        结果把页归档了）。要重编正文走 polish 工具，不是 set。"""
        a = self.mk_cand(1)
        self.mk_draft("p", [a.id])
        before = self.snapshot()
        ok, payload = wa.set("candidates", {
            "move": [a.id], "from": "p", "to": "p"})
        self.assertFalse(ok)
        self.assertIn("来源与目标相同", str(payload))
        self.assertTrue((wstore.root() / ".drafts" / "p.md").exists(),
                        "同页移动不能把页归档")
        self.assertEqual(self.snapshot(), before, "拒写时一个字节都不能动")
        self.assertEqual(self.draft_ids("p"), [a.id])

    def test_merge_joins_pages_and_archives_old_ones(self):
        a, b = self.mk_cand(1), self.mk_cand(2)
        self.mk_draft("p-a", [a.id])
        self.mk_draft("p-b", [b.id])
        ok, payload = wa.set("merge", {"pages": ["p-a", "p-b"], "to": "p-c"})
        self.assertTrue(ok, payload)
        self.assertEqual(self.draft_ids("p-c"), [a.id, b.id])
        self.assertFalse((wstore.root() / ".drafts" / "p-a.md").exists())
        self.assertFalse((wstore.root() / ".drafts" / "p-b.md").exists())

    def test_merge_reusing_a_source_slug_keeps_the_new_page(self):
        """★ 目标复用旧页 slug 时，新页不能被归档动作自己移走。"""
        a, b = self.mk_cand(1), self.mk_cand(2)
        self.mk_draft("p-a", [a.id])
        self.mk_draft("p-b", [b.id])
        ok, payload = wa.set("merge", {"pages": ["p-a", "p-b"], "to": "p-a"})
        self.assertTrue(ok, payload)
        self.assertEqual(self.draft_ids("p-a"), [a.id, b.id], "新页还在")
        self.assertFalse((wstore.root() / ".drafts" / "p-b.md").exists())

    def test_merge_into_replacement_draft_keeps_identity(self):
        """★ 2026-11-13（晚）实测回归：D3 并入 D2（带 merges 的替换稿）。

        旧实现整份新建：merges 被洗掉、标题被 pages[0]（D3）顶掉 ⇒ 入库时
        当成「撞同名页的新页」拒收。身份（merges/标题/tags/域）必须保住，
        且入库校验要走 merges 覆盖分支。
        """
        a, b = self.mk_cand(1), self.mk_cand(2)
        d = wstore.root() / "wiki" / DOMAIN
        d.mkdir(parents=True, exist_ok=True)
        (d / "p-b.md").write_text(_draft_text([a.id], title="甲作者晒单"),
                                  encoding="utf-8")
        (wstore.root() / ".drafts" / "p-b.md").write_text(
            _draft_text([a.id], title="甲作者晒单").replace(
                "tags: []", "tags: [仓位管理, 甲作者]\nmerges: p-b"),
            encoding="utf-8")
        self.mk_draft("p-a", [b.id], title="甲作者谈英伟达")
        # pages[0] 是 D3：旧实现的标题/身份正是从它拿的，顺序即反证
        ok, payload = wa.set("merge", {"pages": ["p-a", "p-b"], "to": "p-b"})
        self.assertTrue(ok, payload)
        text = (wstore.root() / ".drafts" / "p-b.md").read_text(encoding="utf-8")
        fm, body = wstore.parse_front(text)
        self.assertEqual(fm.get("merges"), "p-b", "覆盖声明被洗掉 = 入库必拒")
        self.assertEqual(fm.get("title"), "甲作者晒单", "标题不能被 pages[0] 顶掉")
        self.assertEqual(fm.get("tags"), ["仓位管理", "甲作者"])
        self.assertEqual(wstore._cand_ids(fm), [b.id, a.id])
        self.assertEqual(fm.get("stale"), "true", "分组变了，正文要标待重编")
        self.assertTrue(body.strip(), "旧结论保留等 polish，不许洗成空页")
        ok, info = wstore._validate_draft("p-b")
        self.assertTrue(ok, info)
        self.assertEqual(info["merges"], "p-b", "入库必须走覆盖分支而不是新页")

    def test_merge_new_dst_inherits_source_merges(self):
        """目标是新 slug、但来源里有替换稿：覆盖声明跟着合并走，不能丢。"""
        a, b = self.mk_cand(1), self.mk_cand(2)
        d = wstore.root() / "wiki" / DOMAIN
        d.mkdir(parents=True, exist_ok=True)
        (d / "target.md").write_text(_draft_text([a.id]), encoding="utf-8")
        (wstore.root() / ".drafts" / "p-a.md").write_text(
            _draft_text([a.id]).replace("tags: []", "tags: []\nmerges: target"),
            encoding="utf-8")
        self.mk_draft("p-b", [b.id])
        ok, payload = wa.set("merge", {"pages": ["p-a", "p-b"], "to": "p-c"})
        self.assertTrue(ok, payload)
        fm = wstore.parse_front(
            (wstore.root() / ".drafts" / "p-c.md").read_text(encoding="utf-8"))[0]
        self.assertEqual(fm.get("merges"), "target")
        # 新页正文留空等 polish（真实链路由 agent-loop 自动重编）；
        # 入库校验挡空壳页，这里补上结论再验覆盖分支。
        p = wstore.root() / ".drafts" / "p-c.md"
        p.write_text(p.read_text(encoding="utf-8") + "合并后的结论。\n",
                     encoding="utf-8")
        ok, info = wstore._validate_draft("p-c")
        self.assertTrue(ok, info)

    def test_merge_conflicting_merges_targets_rejected(self):
        """两份来源指向不同覆盖目标 = 不可判定：拒写，一个字节都不动。"""
        a, b = self.mk_cand(1), self.mk_cand(2)
        d = wstore.root() / "wiki" / DOMAIN
        d.mkdir(parents=True, exist_ok=True)
        for slug, c in (("t-x", a), ("t-y", b)):
            (d / f"{slug}.md").write_text(_draft_text([c.id]), encoding="utf-8")
        for slug, c, target in (("p-a", a, "t-x"), ("p-b", b, "t-y")):
            (wstore.root() / ".drafts" / f"{slug}.md").write_text(
                _draft_text([c.id]).replace(
                    "tags: []", f"tags: []\nmerges: {target}"), encoding="utf-8")
        before = self.snapshot()
        ok, err = wa.set("merge", {"pages": ["p-a", "p-b"], "to": "p-c"})
        self.assertFalse(ok)
        self.assertIn("不可判定", err)
        self.assertEqual(self.snapshot(), before, "拒写时一个字节都不能动")

    def test_set_title_domain_tags(self):
        a = self.mk_cand(1)
        self.mk_draft("p", [a.id])
        # ★ 改标题 = 改 slug（跟着标题走）：中文标题自动走拼音 p → xin-biao-ti
        ok, payload = wa.set("title", {"slug": "p", "title": "新标题"})
        self.assertTrue(ok, payload)
        self.assertEqual(payload["slug"], "xin-biao-ti")
        self.assertFalse((wstore.root() / ".drafts" / "p.md").exists())
        for action, val in (("domain", "ai"), ("tags", ["成本", "端侧"])):
            ok, payload = wa.set(action, {"slug": "xin-biao-ti", action: val})
            self.assertTrue(ok, payload)
        fm = wstore.parse_front(
            (wstore.root() / ".drafts" / "xin-biao-ti.md").read_text(encoding="utf-8"))[0]
        self.assertEqual(fm["title"], "新标题")
        self.assertEqual(fm["domain"], "ai")
        self.assertEqual(fm["tags"], ["成本", "端侧"])

    def test_title_rename_carries_approved_mark(self):
        """★ 审批标记跟页走（2026-11-09 实测「已审 2/9 但一个圈都没勾」）：
        改名后 approved.json 里是新 slug，不留幽灵旧 slug。"""
        self.mk_draft("p", [self.mk_cand(1).id])
        wstore.toggle_approved("p")
        ok, payload = wa.set("title", {"slug": "p", "title": "新标题"})
        self.assertTrue(ok, payload)
        self.assertEqual(payload["slug"], "xin-biao-ti")
        self.assertTrue(wstore.is_approved("xin-biao-ti"))
        self.assertNotIn("p", wstore.read_approved())

    def test_title_rename_rewrites_wikilinks_in_other_drafts(self):
        """★ 改名时其他草稿正文里的 [[old-slug]] 引用跟着改（2026-11-10 实测：
        只改了 merges/edits，正文链接还指着旧 slug，入库后全是断链）。
        带 |别名 的也改；前缀相同的 [[old-slug-extra]] 不能误伤。"""
        self.mk_draft("b", [self.mk_cand(1).id])
        self.mk_draft("a", [self.mk_cand(2).id],
                      body="见 [[b]] 与 [[b|别名]]，也见 [[b-extra]]。\n")
        ok, payload = wa.set("title", {"slug": "b", "title": "新标题"})
        self.assertTrue(ok, payload)
        text = (wstore.root() / ".drafts" / "a.md").read_text(encoding="utf-8")
        self.assertIn("[[xin-biao-ti]]", text)
        self.assertIn("[[xin-biao-ti|别名]]", text)
        self.assertIn("[[b-extra]]", text, "前缀相同的其它 slug 不能被误伤")

    def test_discard_drops_approved_mark(self):
        """退稿把审批标记一起清掉，不留在计数里撑大分子。"""
        self.mk_draft("p", [self.mk_cand(1).id])
        wstore.toggle_approved("p")
        ok, payload = wa.set("discard", {"slug": "p"})
        self.assertTrue(ok, payload)
        self.assertNotIn("p", wstore.read_approved())

    def test_title_ignores_short_or_keep_old_new_slug(self):
        """★ new_slug 守卫：模型传 "ai" 或原 slug 都不算数（提示词拦不住，
        代码拦）—— slug 必须跟着标题走（2026-11-09 用户拍板）。"""
        self.mk_draft("p", [self.mk_cand(1).id])
        ok, payload = wa.set("title", {"slug": "p", "title": "AI泡沫",
                                       "new_slug": "ai"})
        self.assertTrue(ok, payload)
        self.assertEqual(payload["slug"], "ai-pao-mo")
        ok, payload = wa.set("title", {"slug": "ai-pao-mo", "title": "投资的心法",
                                       "new_slug": "ai-pao-mo"})
        self.assertTrue(ok, payload)
        self.assertEqual(payload["slug"], "tou-zi-de-xin-fa")

    def test_title_accepts_descriptive_new_slug(self):
        """用户明确要某个描述性 slug 时照用（≥3 词或 15+ 字符）。"""
        self.mk_draft("p", [self.mk_cand(1).id])
        ok, payload = wa.set("title", {"slug": "p", "title": "AI泡沫",
                                       "new_slug": "ai-bubble-backlash"})
        self.assertTrue(ok, payload)
        self.assertEqual(payload["slug"], "ai-bubble-backlash")
        self.assertFalse((wstore.root() / ".drafts" / "p.md").exists())

    def test_set_domain_rejects_value_outside_ontology(self):
        a = self.mk_cand(1)
        self.mk_draft("p", [a.id])
        ok, err = wa.set("domain", {"slug": "p", "domain": "不存在的域"})
        self.assertFalse(ok)
        self.assertIn("不在受控清单", err)

    def test_discard_also_discards_candidates(self):
        """丢弃 = 内容级否定：候选一并废弃，草稿留证据不删。

        ★ 2026-11-13（晚）语义变更：旧「回池重编」让退稿在下一轮编译原地复活
        （实测 16:53 退稿、20:10 同名重编）。反悔走候选面板改判回待审。
        """
        a = self.mk_cand(1)
        self.mk_draft("p", [a.id])
        ok, payload = wa.set("discard", {"slug": "p"})
        self.assertTrue(ok, payload)
        self.assertFalse((wstore.root() / ".drafts" / "p.md").exists())
        self.assertIn("archived", payload)
        self.assertEqual(payload["released_candidates"], [a.id])
        a.refresh_from_db()
        self.assertEqual(a.status, "discarded",
                         "候选回池的话，退稿下一轮编译就会复活")


class Guard1LockFrontmatterTest(WikiAgentBase):
    def setUp(self):
        super().setUp()
        self.c = self.mk_cand(1)
        self.p = self.mk_draft("p", [self.c.id])

    def test_payload_cannot_smuggle_sources(self):
        """守卫 1：夹带机器字段必须被**拒绝**，不是被静默忽略。"""
        ok, err = wa.set("title", {"slug": "p", "title": "新标题",
                                   "sources": ["fake:999"]})
        self.assertFalse(ok)
        self.assertIn("sources", err)
        self.assertIn("程序维护", err)

    def test_frontmatter_action_rejects_locked_key(self):
        """★ 通用动作是 agent 最常走的路，不给它开改写 sources 的后门。"""
        ok, err = wa.set("frontmatter", {"slug": "p",
                                         "fields": {"confidence": "high"}})
        self.assertFalse(ok)
        self.assertIn("只允许", err)

    def test_frontmatter_action_allows_settable_keys(self):
        ok, payload = wa.set("frontmatter", {"slug": "p",
                                             "fields": {"title": "新的", "tags": ["x"]}})
        self.assertTrue(ok, payload)
        fm = wstore.parse_front(self.p.read_text(encoding="utf-8"))[0]
        self.assertEqual(fm["title"], "新的")

    def test_rewrite_fm_preserves_machine_fields_and_order(self):
        """结构事实：按行替换，机器字段连碰都没碰到（不是靠守卫自觉）。"""
        new = wa._rewrite_fm(self.p.read_text(encoding="utf-8"),
                             {"title": "改个标题"})
        fm = wstore.parse_front(new)[0]
        self.assertEqual(fm["sources"], ["entry:123"])
        self.assertEqual(fm["updated"], "2026-11-05")
        self.assertEqual(fm["confidence"], "medium")
        self.assertEqual(fm["title"], "改个标题")

    def test_guard_itself_will_fire(self):
        """★ 守卫自己会响：放宽 `_write_fm` 的白名单，改 sources 就会真的落盘。

        正向（守卫在 ⇒ 拒）+ 反向（守卫放宽 ⇒ 写进去），两边都验才算数：
        只写「被拒」的话，把守卫整段删掉测试照样可能因为别的原因绿。
        """
        ok, err = wa._write_fm("p", {"sources": ["fake:999"]})
        self.assertFalse(ok)
        self.assertIn("不能改", err)
        with patch.object(wa, "SETTABLE_FM", wa.SETTABLE_FM + ("sources",)):
            ok2, err2 = wa._write_fm("p", {"sources": ["fake:999"]})
        self.assertTrue(ok2, err2)
        fm = wstore.parse_front(self.p.read_text(encoding="utf-8"))[0]
        self.assertEqual(fm["sources"], ["fake:999"], "守卫一放宽，机器字段就被改了")


class Guard2CandidateConsistencyTest(WikiAgentBase):
    def test_ghost_candidate_rejected(self):
        """候选 999999 不存在 ⇒ 拒写，且原文件一个字节没动。"""
        a = self.mk_cand(1)
        self.mk_draft("p", [a.id])
        before = self.snapshot()
        err = wa._check_candidates({"p": [999999]}, {"p": [a.id]})
        self.assertIn("幽灵候选", err)
        self.assertEqual(self.snapshot(), before)

    def test_discarded_candidate_rejected_and_no_orphan(self):
        """★ 规格反证 2：拆 [3,5] 时 5 已 discarded ⇒ 拒写，3 还在旧页。"""
        a = self.mk_cand(1)
        b = self.mk_cand(2, "discarded")
        self.mk_draft("old", [a.id, b.id])
        before = self.snapshot()
        ok, err = wa.set("candidates", {
            "move": [a.id, b.id], "from": "old", "to": "new"})
        self.assertFalse(ok)
        self.assertIn("不存在或已丢弃", err)
        self.assertEqual(self.snapshot(), before, "拒写时一个字节都不该动")
        self.assertEqual(self.draft_ids("old"), [a.id, b.id])

    def test_duplicate_ownership_rejected(self):
        """同一候选出现在两页 ⇒ 拒（拆页写一半留下的残局）。"""
        a = self.mk_cand(1)
        err = wa._check_candidates({"x": [a.id], "y": [a.id]}, {"x": [a.id]})
        self.assertIn("多页", err)

    def test_lost_candidate_rejected(self):
        """候选从页面消失且无归宿 ⇒ 拒。"""
        a, b = self.mk_cand(1), self.mk_cand(2)
        err = wa._check_candidates({"p": [a.id]}, {"p": [a.id, b.id]})
        self.assertIn("弄丢候选", err)

    def test_discard_is_allowed_to_shrink(self):
        """丢弃允许减少 —— 但不允许凭空多出候选（用真实存在的候选造反例）。"""
        a, b, c = self.mk_cand(1), self.mk_cand(2), self.mk_cand(3)
        self.assertEqual(wa._check_candidates({}, {"p": [a.id, b.id]},
                                              allow_shrink=True), "")
        self.assertIn("凭空多出候选", wa._check_candidates(
            {"p": [a.id, b.id, c.id]}, {"p": [a.id, b.id]}, allow_shrink=True))

    def test_guard_itself_will_fire(self):
        """★ 守卫自己会响：把幽灵判据短路，被拒的那条必须变成通过。"""
        a = self.mk_cand(1)
        with patch.object(wa, "live_candidates", return_value={999999: a}):
            err = wa._check_candidates({"p": [999999]}, {"p": [999999]})
        self.assertEqual(err, "", "幽灵判据拆掉后，999999 会被当成真候选")


class Guard3NoCommitTest(WikiAgentBase):
    def test_set_never_writes_into_wiki_dir(self):
        """守卫 3：set 只改 .drafts/，入库页一个都不出现（commit 是改动 F）。"""
        a, b = self.mk_cand(1), self.mk_cand(2)
        self.mk_draft("old", [a.id, b.id])
        ok, _ = wa.set("candidates", {
            "move": [b.id], "from": "old", "to": "new"})
        self.assertTrue(ok)
        self.assertEqual(list((wstore.root() / "wiki").rglob("*.md")), [])

    def test_set_does_not_call_approve(self):
        """程序动作不许被 agent 绕过：整个 set 链路不碰 approve()。"""
        a = self.mk_cand(1)
        self.mk_draft("p", [a.id])
        with patch.object(wstore, "approve") as approve:
            wa.set("title", {"slug": "p", "title": "新"})
            wa.set("discard", {"slug": "p"})
        approve.assert_not_called()


class BodyStaleTest(WikiAgentBase):
    def test_old_body_gets_a_visible_warning(self):
        """分组改了、结论还没重编 —— 状态必须可判，且不许污染结论正文。

        ★ 形态 A：标记写在 frontmatter `stale: true`，正文保持纯 polish。
        ★ 「可见」这条产品承诺由审批浮层负责（`_approval_workarea.html` 的
          `{% if body_stale %}` → 「分组已由审批对话调整」），回归在
          `test_approval_overlay.ApprovalOverlayTest.test_body_stale_shows_hint`。
          本用例守的是**数据层两份口径**：文件里有机器可读标记 + `read()` 翻得出来；
          少了任一份，浮层要么不提示、要么靠正文里的脏字提示（两者都已踩过）。
        """
        a, b = self.mk_cand(1), self.mk_cand(2)
        self.mk_draft("old", [a.id, b.id])
        wa.set("candidates", {"move": [b.id], "from": "old", "to": "new"})
        text = (wstore.root() / ".drafts" / "old.md").read_text(encoding="utf-8")
        fm, body = wstore.parse_front(text)
        self.assertEqual(str(fm.get(wstore.STALE_FM)).strip().lower(), "true",
                         "frontmatter 必须有 stale: true")
        self.assertNotIn(wstore.STALE_MARK, body, "提示不许塞进结论正文")
        self.assertTrue(wa.read("draft", "old")["body_stale"])

    def test_stale_mark_does_not_pollute_index_summary(self):
        """标记在 frontmatter，正文与摘要都不许被它污染（不是「恒不出现」的空断言）。"""
        a, b = self.mk_cand(1), self.mk_cand(2)
        self.mk_draft("old", [a.id, b.id])
        wa.set("candidates", {"move": [b.id], "from": "old", "to": "new"})
        fm, body = wstore.parse_front(
            (wstore.root() / ".drafts" / "old.md").read_text(encoding="utf-8"))
        # 正证：标记真在（否则下面「正文里没有」是空集合假绿）
        self.assertEqual(str(fm.get(wstore.STALE_FM)).strip().lower(), "true")
        self.assertNotIn(wstore.STALE_MARK, body)
        self.assertNotIn("stale", body.lower())
        first = wstore._first_sentence(body)
        self.assertTrue(first.strip(), "正文非空，摘要才有东西可截")
        self.assertNotIn("⚠️", first)
        self.assertNotIn("stale", first.lower())
