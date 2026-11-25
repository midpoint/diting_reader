"""2026-11-13：退稿依赖同步 —— 唯一落点 / 零 LLM / 已入库页走审批副本。

背景（真实库 16:58 → 18:34 实测）：用户手点退稿 D3 后，另一份草稿正文里的
`[[inference-compute-and-payment]]` 没人同步；18:34 那份草稿带死链入库，
commit 批末才发现断链、又补拉一份编辑副本 ⇒ 用户问「怎么还剩一个 draft」。

★ 判据分层：单测钉「产物长什么样 / wiki/ 有没有被动 / 四条路径都同步」；
  「点一下之后浮层还在、编辑副本看得见」由真浏览器 e2e 验。
★ 每条空集合断言都配一条可见文字 / 逐字节断言，避免 `[].every()` 式假绿。
"""
import json
from pathlib import Path
from unittest.mock import patch

from django.urls import reverse

from core import wiki as wstore
from core import wiki_agent as wa
from core import wiki_loop as wl
from core.tests.test_wiki_ui_step11 import UiBase

FM = ("---\ntitle: {title}\ndomain: ai\nsources: [entry:9101]\n"
      "updated: 2026-11-13\nconfidence: medium\ntags: []\n"
      "candidates: [201]\n{extra}---\n\n{body}\n")

GONE = "inference-compute-and-payment"


def draft(slug, body, *, title=None, extra=""):
    (wstore.root() / ".drafts" / f"{slug}.md").write_text(
        FM.format(title=title or slug, extra=extra, body=body), encoding="utf-8")


def page(slug, body, *, title=None, domain="ai", cands="[201]"):
    d = wstore.root() / "wiki" / domain
    d.mkdir(parents=True, exist_ok=True)
    text = FM.format(title=title or slug, extra="", body=body).replace(
        "candidates: [201]", f"candidates: {cands}")
    (d / f"{slug}.md").write_text(text, encoding="utf-8")


def read(rel):
    return (wstore.root() / rel).read_text(encoding="utf-8")


def gone_link(text):
    """正文里是否仍有指向 GONE 的内链（frontmatter 不参与）。"""
    return [m.group(1).strip() for m in wstore._WIKILINK_RE.finditer(
        wstore.parse_front(text)[1])]


class SyncBase(UiBase):
    def seed(self, *, keeper=True, published=False, keeper_extra=""):
        draft(GONE, "这份要被退掉。", title="推理算力的供给与付费")
        if keeper:
            draft("keeper", f"其一 [[{GONE}]]；其二 [[{GONE}|供给与付费]]；"
                            f"还有 [[{GONE}-extra]] 别动。", extra=keeper_extra)
        self.page_text = None
        if published:
            page("page-a", f"已定稿的结论，链到 [[{GONE}]]。", title="已入库页 A")
            self.page_text = read("wiki/ai/page-a.md")


class TestUnwrapWikilinks(SyncBase):
    """文本层：别名优先、精确匹配、frontmatter 一个字节不碰。"""

    def test_alias_wins_plain_uses_archived_title(self):
        text = FM.format(title="页", extra="", body=f"[[{GONE}]] 和 [[{GONE}|别名]]")
        out, n = wstore.unwrap_wikilinks(text, GONE, "推理算力的供给与付费")
        self.assertEqual(n, 2)
        self.assertIn("推理算力的供给与付费 和 别名", out)
        self.assertNotIn("[[", out)

    def test_exact_match_only_and_frontmatter_untouched(self):
        # ★ frontmatter 里故意也放一个 [[gone]]：只写「out.startswith(head)」时，
        #   全文替换因为 head 里没有链接照样通过，判据是假的（变异反证 #06 实测）。
        head = f"---\ntitle: 页\ntags: [[{GONE}]]\n---\n"
        body = f"[[{GONE}-extra]] 和 [[{GONE}]]"
        out, n = wstore.unwrap_wikilinks(head + body, GONE, "标题")
        self.assertEqual(n, 1, "frontmatter 里的 [[gone]] 不该被算进去")
        self.assertTrue(out.startswith(head), "frontmatter 一个字节都不能动")
        self.assertIn(f"[[{GONE}-extra]]", out, "前缀相同的页被误伤")
        self.assertIn("和 标题", out)

    def test_archived_title_falls_back_to_slug_when_no_archive(self):
        self.assertEqual(wstore.archived_title("never-seen"), "never-seen")
        self.assertEqual(wstore.unwrap_wikilinks(f"[[{GONE}]]", GONE)[0], GONE)

    def test_no_link_returns_same_bytes(self):
        text = "没有内链的一段话"
        self.assertEqual(wstore.unwrap_wikilinks(text, GONE), (text, 0))


class TestSyncCore(SyncBase):
    """core 层唯一落点：discard() 必须自己同步，不用调用点记得。"""

    def test_other_draft_unwrapped_with_archived_title(self):
        self.seed()
        report = {}
        ok, dest = wstore.discard(GONE, "不重要", sync_report=report)
        self.assertTrue(ok, dest)
        keeper = read(".drafts/keeper.md")
        self.assertNotIn(GONE, gone_link(keeper), "草稿正文里还留着死链")
        self.assertIn("推理算力的供给与付费", keeper, "无别名处应显示归档页标题")
        self.assertIn("供给与付费", keeper, "有别名处应优先用别名")
        self.assertIn(f"[[{GONE}-extra]]", keeper)
        self.assertIn("candidates: [201]", keeper, "frontmatter 不该被改")
        self.assertEqual(report["drafts"], ["keeper"])
        self.assertEqual(report["links"], 2)
        self.assertEqual(report["guard_errors"], [])
        self.assertEqual(report["display"], "推理算力的供给与付费")
        self.assertIn("discard-sync", read("log.md"))

    def test_published_page_gets_edit_draft_and_wiki_never_touched(self):
        self.seed(published=True)
        report = {}
        ok, _ = wstore.discard(GONE, "不重要", sync_report=report)
        self.assertTrue(ok)
        self.assertEqual(read("wiki/ai/page-a.md"), self.page_text,
                         "★ 已入库页一个字节都不能被直接改写")
        copy = read(".drafts/page-a.md")
        self.assertIn("edits: page-a", copy, "必须走审批副本，不是偷偷写 wiki/")
        self.assertNotIn(GONE, gone_link(copy))
        self.assertIn("推理算力的供给与付费", copy)
        self.assertEqual(report["pages"], ["page-a"])
        self.assertEqual(report["guard_errors"], [])
        self.assertTrue(wstore._validate_draft("page-a")[0],
                        "自动生成的编辑副本必须能直接提交入库")
        self.assertIn("→ .drafts/page-a.md", read("log.md"))

    def test_existing_edit_draft_is_reused_not_blocked(self):
        self.seed(published=True)
        draft("page-a", f"人正在编辑的一版，也链到 [[{GONE}]]。",
              extra="edits: page-a\n")
        report = {}
        ok, _ = wstore.discard(GONE, "不重要", sync_report=report)
        self.assertTrue(ok)
        self.assertEqual(report["pages"], ["page-a"], "已有副本被解包后应复用")
        self.assertEqual(report["blocked"], [], "别把已处理好的副本报成待处理")
        self.assertNotIn(GONE, gone_link(read(".drafts/page-a.md")))
        self.assertIn("人正在编辑的一版", read(".drafts/page-a.md"))

    def test_frontmatter_dependency_reported_not_guessed(self):
        self.seed(keeper_extra=f"merges: {GONE}\n")
        report = {}
        ok, _ = wstore.discard(GONE, "不重要", sync_report=report)
        self.assertTrue(ok)
        self.assertIn(f"merges: {GONE}", read(".drafts/keeper.md"),
                      "程序猜不出该改指向哪，不许悄悄改 frontmatter")
        self.assertTrue(any("frontmatter merges" in b["why"]
                            for b in report["blocked"]), report)
        self.assertIn("需人工改指向", " ".join(b["why"] for b in report["blocked"]))

    def test_empty_candidates_page_is_blocked_not_delete_draft(self):
        """e2e 种子实测：原页候选为空 ⇒ pull_page 副本被 is_delete_fm 判成删页稿。

        退稿同步绝不能凭空造一份「人一点提交就把这页删掉」的稿。
        """
        self.seed(keeper=False)
        page("page-b", f"链到 [[{GONE}]]。", title="空候选页", cands="[]")
        report = {}
        ok, _ = wstore.discard(GONE, "不重要", sync_report=report)
        self.assertTrue(ok)
        self.assertFalse((wstore.root() / ".drafts" / "page-b.md").exists(),
                         "不许凭空生成一份点提交就删页的稿")
        self.assertTrue(any("删页稿" in b["why"] for b in report["blocked"]),
                        report["blocked"])
        self.assertEqual(report["pages"], [])
        self.assertIn(f"[[{GONE}]]", read("wiki/ai/page-b.md"),
                      "没生成副本也不许直接改 wiki/")

    def test_no_dependency_writes_no_sync_log(self):
        draft(GONE, "这份要被退掉。")
        report = {}
        ok, _ = wstore.discard(GONE, "不重要", sync_report=report)
        self.assertTrue(ok)
        self.assertEqual(report["links"], 0)
        self.assertEqual(report["drafts"] + report["pages"] + report["blocked"], [])
        self.assertNotIn("discard-sync", read("log.md"), "没动作就不该有噪音日志")

    def test_guard_catches_program_thinking_it_wrote_but_did_not(self):
        """一致性守卫：把解包桩成「报了 1 处却没改正文」，必须显式报错。"""
        self.seed()
        real = wstore.unwrap_wikilinks

        def lying(text, slug, display=None):
            out, n = real(text, slug, display)
            return text, max(n, 1)

        with patch.object(wstore, "unwrap_wikilinks", lying):
            report = wstore.sync_discard_references(GONE)
        self.assertTrue(report["guard_errors"], "守卫没响 = 静默失败")
        self.assertIn(f"仍链向 [[{GONE}]]", " ".join(report["guard_errors"]))
        self.assertIn("守卫未通过", read("log.md"))

    def test_sync_is_zero_llm(self):
        self.seed(published=True)
        with patch.object(wl, "_call") as loop_call, \
             patch("core.citeguard._call") as cg_call:
            wstore.discard(GONE, "不重要")
        loop_call.assert_not_called()
        cg_call.assert_not_called()

    def test_return_value_contract_unchanged(self):
        """verify_wiki / agent 都把 payload 当路径字符串用，不许改成 dict。"""
        self.seed()
        ok, dest = wstore.discard(GONE, "不重要")
        self.assertTrue(ok)
        self.assertIsInstance(dest, str)
        self.assertTrue((wstore.root() / dest).exists())
        self.assertIn(".discarded", dest)


class TestAllDiscardPaths(SyncBase):
    """四条退稿路径都必须经过同一个同步落点（同类扫全集）。"""

    def test_button_path_via_view(self):
        self.seed(published=True)
        resp = self.client.post(reverse("wiki_discard"),
                                {"from": "approval", "slug": GONE, "reason": "不重要"})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(GONE, gone_link(read(".drafts/page-a.md")))
        flash = json.loads(resp.get("HX-Trigger", "{}"))["wikiFlash"]["msg"]
        self.assertIn("同步解包", flash)
        self.assertIn("生成 1 份编辑副本", flash)
        self.assertIn("待提交入库", flash)

    def test_agent_discard_path(self):
        self.seed()
        with patch.object(wa, "_check_candidates", return_value=""):
            ok, payload = wa._set_discard({"slug": GONE}, {GONE: [], "keeper": []})
        self.assertTrue(ok, payload)
        self.assertNotIn(GONE, gone_link(read(".drafts/keeper.md")))

    def test_commit_writes_archive_path(self):
        self.seed()
        target = wstore.root() / ".drafts" / "new-page.md"
        with patch.object(wstore, "discard", wraps=wstore.discard) as spy:
            ok, payload = wa._commit_writes(
                [(target, FM.format(title="新页", extra="", body="新页正文"))],
                "merge", "old → new", archive=[GONE],
                archive_reason="审批对话合并归档")
        self.assertTrue(ok, payload)
        self.assertTrue(spy.called, "归档必须走 wstore.discard（唯一落点）")
        self.assertNotIn(GONE, gone_link(read(".drafts/keeper.md")))

    def test_home_legacy_path_opens_approval_for_new_edit_draft(self):
        self.seed(published=True)
        resp = self.client.post(reverse("wiki_discard"),
                                {"slug": GONE, "reason": "不重要"})
        self.assertEqual(resp.status_code, 200)
        triggers = json.loads(resp.get("HX-Trigger", "{}"))
        self.assertIn("wikiApprovalOpen", triggers,
                      "首页退稿若生成编辑副本，要让人看得见")
        self.assertIn("生成 1 份编辑副本", triggers["wikiFlash"]["msg"])

    def test_view_selection_follows_new_edit_draft(self):
        """选中显式切到新副本：把另一份草稿弄成「缺字段」排前面，验证不靠排序巧合。"""
        self.seed(published=True)
        keeper = read(".drafts/keeper.md").replace(
            "candidates: [201]", "candidates: []")
        (wstore.root() / ".drafts" / "keeper.md").write_text(keeper, encoding="utf-8")
        resp = self.client.post(reverse("wiki_discard"),
                                {"from": "approval", "slug": GONE, "reason": "不重要"})
        html = resp.content.decode()
        self.assertIn('name="selected_slug" value="page-a"', html)
        self.assertIn("已入库页 A", html, "工作区要当场显示待确认的那份 diff")

    def test_flash_clean_when_no_dependency(self):
        draft(GONE, "这份要被退掉。")
        resp = self.client.post(reverse("wiki_discard"),
                                {"from": "approval", "slug": GONE, "reason": "不重要"})
        flash = json.loads(resp.get("HX-Trigger", "{}"))["wikiFlash"]["msg"]
        self.assertEqual(
            flash, "已退稿（在 .discarded/，没删） · 候选 1 条一并废弃（可改判回待审）",
            "候选去向是退稿回执的必备信息（2026-11-13 晚）；没依赖同步时不加别的")


def read_log_functions():
    """给静态判据用：wiki_agent 源码里不该再有第二份解包实现。"""
    return Path(wa.__file__).read_text(encoding="utf-8")


class TestRelinkDisplayUnified(SyncBase):
    """两套降级显示文本收口：relink 也用归档页标题，不再留裸 slug。"""

    def test_rewrite_uses_display_mapping(self):
        text = FM.format(title="页", extra="", body=f"链到 [[{GONE}]]。")
        out = wstore._rewrite_wikilinks(
            text, [{"target": GONE, "action": "plain"}],
            display={GONE: "推理算力的供给与付费"})
        self.assertIn("链到 推理算力的供给与付费。", out)
        self.assertNotIn(f"链到 {GONE}", out)

    def test_write_relink_drafts_passes_archived_title(self):
        self.seed(keeper=False, published=True)
        # 造一份归档文件，让 archived_title 能读到中文标题
        box = wstore.root() / ".drafts" / ".discarded"
        (box / f"{GONE}-20261113-165805.md").write_text(
            FM.format(title="推理算力的供给与付费", extra="", body="归档"),
            encoding="utf-8")
        wstore.discard(GONE, "不重要")  # 会生成 page-a 副本，先清掉再单独验 relink
        (wstore.root() / ".drafts" / "page-a.md").unlink()
        plan = {"auto": [{"target": GONE, "action": "plain",
                          "referrers": ["wiki/ai/page-a.md"]}], "ask": []}
        written, blocked, _ = wstore._write_relink_drafts(plan)
        self.assertEqual(written, ["page-a"], blocked)
        body = read(".drafts/page-a.md")
        self.assertIn("推理算力的供给与付费", body, "relink 降级不该再留裸 slug")
        self.assertNotIn(f"链到 [[{GONE}]]", body)


class TestStaticContract(SyncBase):
    """源码形状：唯一落点、零 LLM、不直接写 wiki/。"""

    def test_core_discard_calls_sync_and_agent_does_not(self):
        """只认调用形状，不认注释里的字样（11-13 上一轮刚踩过这个假红）。"""
        import inspect
        self.assertIn("sync_discard_references", inspect.getsource(wstore.discard))
        agent_src = read_log_functions()
        self.assertNotIn("def _unwrap_slug_references", agent_src)
        self.assertNotIn("_unwrap_slug_references(", agent_src)

    def test_sync_body_is_zero_llm_and_goes_through_pull_page(self):
        """「不直接写 wiki/」由逐字节行为判据钉住，这里只钉源码形状。"""
        import inspect
        src = inspect.getsource(wstore.sync_discard_references)
        self.assertIn("pull_page(", src)
        for banned in ("_call(", "recommend(", "_polish_one(", "llm"):
            self.assertNotIn(banned, src, f"同步路径不许碰 LLM：{banned}")
