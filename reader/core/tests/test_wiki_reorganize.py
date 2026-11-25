"""第六步：统一重组模型 —— 「拉入审批编辑」整条链路（2026-11-06）。

盯四件事：
  1. `pull_page()`：把已入库页复制成**替换稿**草稿（`edits: <slug>`），原页原地不动；
  2. `edits` 稿的候选**可以减**（人编辑本来就要能拆能删），而 `merges` 稿仍然只增不减；
  3. `_approve_edit()`：备份旧版 → 覆盖 → 重建 sources / backlinks / index；
  4. `edits` 是程序独占字段，`set` 写不了（否则普通草稿能伪装成覆盖稿）。

★ 与 `test_wiki_edit.py`（草稿编辑 2.3b）/ `test_wiki_page_edit.py`（手敲入库页 2.3c）
  是**不同的东西**：那两个改正文文本，这个改「这页归哪些候选、叫什么、在哪个域」。
★ 与 `test_commit.py` 同一套隔离：临时 `WIKI_DIR`，不碰真实 `data/wiki/`。
"""
import json
import re
import shutil
import tempfile

from django.test import TestCase, override_settings

from core import wiki as wstore
from core import wiki_agent as wa


def set_cands(text, ids):
    """把 frontmatter 的 `candidates: [...]` 换成给定 ids（测试造「候选被改」）。"""
    body = ", ".join(str(i) for i in ids)
    out, n = re.subn(r"^candidates: \[.*?\]$", f"candidates: [{body}]", text,
                     count=1, flags=re.M)
    assert n == 1, "测试数据里没有 candidates 行"
    return out


PAGE = """---
title: {title}
domain: {domain}
sources: [art-{slug}]
updated: 2026-11-01
confidence: high
tags: [{tags}]
candidates: {cands}
{extra}---
## 候选清单

{body}

## 通顺话

{polish}
"""


class EditBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-wiki-6-")
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

    # ---------------- 造数据 ----------------
    def mk_page(self, slug, *, domain="tech", cands=(1, 2), tags="", extra="",
                body="一段话。", polish="一段通顺的话。"):
        text = PAGE.format(
            title=f"标题-{slug}", domain=domain, slug=slug, tags=tags,
            cands=json.dumps(list(cands), ensure_ascii=False), extra=extra,
            body=body, polish=polish)
        p = wstore.root() / "wiki" / domain / f"{slug}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def draft_path(self, slug):
        return wstore.root() / ".drafts" / f"{slug}.md"

    def draft_text(self, slug):
        return self.draft_path(slug).read_text(encoding="utf-8")

    def page_text(self, slug, domain="tech"):
        return (wstore.root() / "wiki" / domain / f"{slug}.md").read_text(encoding="utf-8")

    def backlinks(self):
        p = wstore.root() / "wiki" / "backlinks.json"
        return json.loads(p.read_text(encoding="utf-8") or "{}")


# ================================================================ pull_page
class PullPageTest(EditBase):
    def test_copies_page_and_keeps_original(self):
        self.mk_page("moat", cands=(1, 2))
        before = self.page_text("moat")

        ok, payload = wstore.pull_page("moat")

        self.assertTrue(ok, payload)
        self.assertEqual(payload["draft"], ".drafts/moat.md")
        self.assertEqual(payload["page"], "wiki/tech/moat.md")
        # ★ 原页一个字节都不许动：它是「当前已定稿版本」，编辑期间还得能读
        self.assertEqual(self.page_text("moat"), before)
        # 副本存在且带 edits 标记
        fm, _body = wstore.parse_front(self.draft_text("moat"))
        self.assertEqual(fm["edits"], "moat")
        self.assertEqual(fm["title"], "标题-moat")
        self.assertEqual(wstore._cand_ids(fm), [1, 2])

    def test_refuses_when_draft_already_exists(self):
        self.mk_page("moat")
        ok, _ = wstore.pull_page("moat")
        self.assertTrue(ok)
        ok2, why = wstore.pull_page("moat")
        self.assertFalse(ok2)
        self.assertIn("已有一份同名草稿", why)

    def test_refuses_unknown_page(self):
        ok, why = wstore.pull_page("no-such-page")
        self.assertFalse(ok)
        self.assertIn("不存在", why)

    def test_logs_pull_page(self):
        self.mk_page("moat")
        wstore.pull_page("moat")
        rows = [r for r in wstore.read_log(limit=10) if r["action"] == "pull-page"]
        self.assertEqual(len(rows), 1)
        self.assertIn("wiki/tech/moat.md", rows[0]["target"])

    def test_draft_is_listed_with_edits(self):
        """审批浮层的 ① 列表要能认出这是替换稿，不能显示成普通新草稿。"""
        self.mk_page("moat")
        wstore.pull_page("moat")
        rows = wstore.list_drafts()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["edits"], "moat")


# ================================================================ 候选可减
class EditsAllowsShrinkTest(EditBase):
    def test_edits_draft_with_fewer_candidates_is_accepted(self):
        """★ 本步最核心的一条：人编辑时**候选可以减**。

        对照 `test_merge_refuses_lost_candidate`（merges 稿仍然拒绝）：两套松紧
        由 frontmatter 的字段区分，不是靠「编译链 vs 审批链」的位置区分。
        """
        self.mk_page("moat", cands=(1, 2, 3))
        ok, _ = wstore.pull_page("moat")
        self.assertTrue(ok)
        # 人把候选 3 拆去别处（或丢弃）了 —— 只剩 1、2
        text = set_cands(self.draft_text("moat"), [1, 2])
        self.draft_path("moat").write_text(text, encoding="utf-8")

        ok, payload = wstore.approve("moat")

        self.assertTrue(ok, payload)
        self.assertTrue(payload.get("edited"))
        self.assertEqual(payload["removed"], [3])
        fm, _ = wstore.parse_front(self.page_text("moat"))
        self.assertEqual(wstore._cand_ids(fm), [1, 2])

    def test_edits_draft_can_add_candidates(self):
        self.mk_page("moat", cands=(1,))
        wstore.pull_page("moat")
        text = set_cands(self.draft_text("moat"), [1, 2])
        self.draft_path("moat").write_text(text, encoding="utf-8")

        ok, payload = wstore.approve("moat")
        self.assertTrue(ok, payload)
        self.assertEqual(payload["added"], [2])
        fm, _ = wstore.parse_front(self.page_text("moat"))
        self.assertEqual(wstore._cand_ids(fm), [1, 2])


# ================================================================ 覆盖与关联
class ApproveEditTest(EditBase):
    def test_backs_up_old_version(self):
        self.mk_page("moat")
        wstore.pull_page("moat")
        ok, payload = wstore.approve("moat")
        self.assertTrue(ok, payload)
        bak = wstore.root() / payload["backup"]
        self.assertTrue(bak.is_file())
        self.assertIn("标题-moat", bak.read_text(encoding="utf-8"))

    def test_overwrites_body_and_strips_edits_marker(self):
        self.mk_page("moat", polish="旧结论。")
        wstore.pull_page("moat")
        text = self.draft_text("moat").replace("旧结论。", "新结论：我改主意了。")
        self.draft_path("moat").write_text(text, encoding="utf-8")

        ok, payload = wstore.approve("moat")
        self.assertTrue(ok, payload)
        after = self.page_text("moat")
        self.assertIn("新结论：我改主意了。", after)
        self.assertNotIn("旧结论。", after)
        # ★ edits 标记必须剥掉：带着它留库，下次编辑会以为这页还在等替换
        fm, _ = wstore.parse_front(after)
        self.assertNotIn("edits", fm)

    def test_can_change_title_and_tags(self):
        self.mk_page("moat", tags="")
        wstore.pull_page("moat")
        text = self.draft_path("moat").read_text(encoding="utf-8")
        text = text.replace("title: 标题-moat", "title: 换了个标题")
        text = text.replace("tags: []", "tags: [护城河, 判断]")
        self.draft_path("moat").write_text(text, encoding="utf-8")

        ok, payload = wstore.approve("moat")
        self.assertTrue(ok, payload)
        fm, _ = wstore.parse_front(self.page_text("moat"))
        self.assertEqual(fm["title"], "换了个标题")
        self.assertEqual(list(fm["tags"]), ["护城河", "判断"])

    def test_can_move_to_another_domain(self):
        """改 domain = 移动：旧位置删掉、新位置写入，index 两边都要对。"""
        self.mk_page("moat", domain="tech")
        wstore.index_upsert("wiki/tech/moat.md", "标题-moat", "摘要", "2026-11-01")
        wstore.pull_page("moat")
        text = self.draft_path("moat").read_text(encoding="utf-8")
        self.draft_path("moat").write_text(
            text.replace("domain: tech", "domain: invest"), encoding="utf-8")

        ok, payload = wstore.approve("moat")
        self.assertTrue(ok, payload)
        self.assertEqual(payload["page"], "wiki/invest/moat.md")
        self.assertEqual(payload["moved_from"], "wiki/tech/moat.md")
        self.assertFalse((wstore.root() / "wiki" / "tech" / "moat.md").exists())
        self.assertTrue((wstore.root() / "wiki" / "invest" / "moat.md").is_file())
        # index：旧行没了、新行在
        idx = (wstore.root() / "index.md").read_text(encoding="utf-8")
        self.assertNotIn("](wiki/tech/moat.md)", idx)
        self.assertIn("](wiki/invest/moat.md)", idx)

    def test_rebuilds_backlinks_after_body_change(self):
        """★ 编辑改的是正文，而正文里的 [[wikilink]] 正是反链来源。

        `save_page`（2.3c 手敲编辑）只重建了 sources、漏了 backlinks —— 本步补上。
        """
        self.mk_page("moat", polish="见 [[old-target]]。")
        wstore.rebuild_backlinks_index(mark_log=False)
        self.assertIn("old-target", self.backlinks())

        wstore.pull_page("moat")
        text = self.draft_path("moat").read_text(encoding="utf-8")
        self.draft_path("moat").write_text(
            text.replace("[[old-target]]", "[[new-target]]"), encoding="utf-8")

        ok, payload = wstore.approve("moat")
        self.assertTrue(ok, payload)
        bl = self.backlinks()
        self.assertNotIn("old-target", bl)
        self.assertIn("new-target", bl)
        self.assertIn("wiki/tech/moat.md", bl["new-target"])

    def test_ambiguous_slug_is_refused_not_guessed(self):
        """两个域有同名页时，pull 必须**拒绝**而不是猜一个。

        ★ 这条同时解释了一个「看似该有、其实不可达」的状态：既然 pull/approve 都不
          接受重名 slug，「新位置被别的同名页占着」就构不出来 —— 所以 `_validate_draft`
          里那句「已存在另一页」是给**手改 domain 且中间被人塞了同名页**的最后兜底。
        """
        self.mk_page("moat", domain="tech")
        self.mk_page("moat", domain="invest")
        ok, why = wstore.pull_page("moat")
        self.assertFalse(ok)
        self.assertIn("重名", why)


# ================================================================ 回归：旧路径不变
class LegacyPathUnchangedTest(EditBase):
    def test_plain_draft_still_creates_new_page(self):
        text = PAGE.format(title="新页", domain="tech", slug="fresh", tags="",
                           cands="[9]", extra="", body="- 一条判断",
                           polish="一句话。")
        self.draft_path("fresh").write_text(text, encoding="utf-8")
        ok, payload = wstore.approve("fresh")
        self.assertTrue(ok, payload)
        self.assertFalse(payload.get("edited"))
        self.assertTrue((wstore.root() / "wiki" / "tech" / "fresh.md").is_file())

    def test_merges_still_refuses_lost_candidate(self):
        """merges 稿（编译链的机器合并）保持只增不减 —— 本步没动它。"""
        self.mk_page("moat", cands=(1, 2, 3))
        text = PAGE.format(title="标题-moat", domain="tech", slug="moat", tags="",
                           cands="[1, 2]", extra="merges: moat\n",
                           body="- 少了一条", polish="话。")
        self.draft_path("moat").write_text(text, encoding="utf-8")

        ok, why = wstore.approve("moat")
        self.assertFalse(ok)
        self.assertIn("合并会丢掉原有候选", why)

    def test_edits_and_merges_together_is_edits(self):
        """两个字段都在时按 edits 走（人编辑优先），不静默退回只增不减。"""
        self.mk_page("moat", cands=(1, 2, 3))
        text = PAGE.format(title="标题-moat", domain="tech", slug="moat", tags="",
                           cands="[1]", extra="edits: moat\nmerges: moat\n",
                           body="- 只留一条", polish="话。")
        self.draft_path("moat").write_text(text, encoding="utf-8")
        ok, payload = wstore.approve("moat")
        self.assertTrue(ok, payload)
        self.assertTrue(payload.get("edited"))


# ================================================================ 字段锁
class EditsIsProgramOwnedTest(EditBase):
    def test_set_cannot_write_edits_field(self):
        """edits 若能被 agent 写，普通草稿就能伪装成覆盖稿。"""
        self.mk_page("moat")
        wstore.pull_page("moat")
        ok, why = wa.set("frontmatter", {"slug": "moat", "fields": {"edits": "other"}})
        self.assertFalse(ok)
        self.assertIn("edits", why)

    def test_set_cannot_pass_edits_at_top_level(self):
        ok, why = wa.set("frontmatter", {"slug": "moat", "edits": "other",
                                          "fields": {"title": "改"}})
        self.assertFalse(ok)
        self.assertIn("edits", why)


# ================================================================ commit 归类
class CommitEditsTest(EditBase):
    def test_commit_classifies_edited_and_new(self):
        self.mk_page("moat", cands=(1,))
        wstore.pull_page("moat")
        # 再放一份普通新页草稿
        text = PAGE.format(title="新页", domain="ai", slug="fresh", tags="",
                           cands="[7]", extra="", body="- 判断", polish="话。")
        self.draft_path("fresh").write_text(text, encoding="utf-8")

        ok, payload = wstore.commit()

        self.assertTrue(ok, payload)
        self.assertEqual(payload["committed"], ["wiki/ai/fresh.md"])
        self.assertEqual(payload["edited"], ["wiki/tech/moat.md"])
        self.assertEqual(payload["merged"], [])
        self.assertEqual(sorted(payload["pages"]),
                         ["wiki/ai/fresh.md", "wiki/tech/moat.md"])
        # 草稿目录清空（编辑稿与普通稿都走了）
        self.assertEqual(sorted(p.name for p in
                                (wstore.root() / ".drafts").glob("*.md")), [])

    def test_commit_all_or_nothing_with_bad_edits_draft(self):
        """编辑稿目标页不存在 → 整批拒绝，普通草稿也不许先入库。"""
        self.mk_page("moat")
        wstore.pull_page("moat")
        # 把 edits 指向一个不存在的页
        text = self.draft_path("moat").read_text(encoding="utf-8")
        self.draft_path("moat").write_text(
            text.replace("edits: moat", "edits: ghost"), encoding="utf-8")
        good = PAGE.format(title="新页", domain="ai", slug="fresh", tags="",
                           cands="[7]", extra="", body="- 判断", polish="话。")
        self.draft_path("fresh").write_text(good, encoding="utf-8")

        ok, why = wstore.commit()

        self.assertFalse(ok)
        self.assertIn("替换目标不存在", why)
        self.assertFalse((wstore.root() / "wiki" / "ai" / "fresh.md").exists())
        self.assertEqual(len([p for p in (wstore.root() / ".drafts").glob("*.md")]), 2)
