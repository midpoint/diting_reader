"""第五步 5c：`commit()` / backlinks 索引 / 批次归档的守卫（2026-11-06）。

★ 盯的是批量入库最贵的错法：**半途失败留下「一半进了 wiki、一半还是草稿」**——
  每一步单看都成功，没有任何一处会报错。所以每条都构造反例，断言「一个字节没动」。
★ 与 `test_wiki_step1` 同一套隔离：临时 `WIKI_DIR`，不碰真实 `data/wiki/`。
"""
import json
import shutil
import tempfile

from django.test import TestCase, override_settings

from core import wiki as wstore
from core import wiki_agent as wa

DRAFT = """---
title: {title}
domain: {domain}
sources: [art-{slug}]
updated: 2026-11-06
confidence: {conf}
tags: []
candidates: {cands}
{extra}---
## 候选清单

{body}

## 通顺话

{polish}
"""


class CommitBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-wiki-5c-")
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
    def mk_draft(self, slug, *, domain="tech", conf="high", cands=None, extra="",
                 body=None, polish=None):
        cands = cands if cands is not None else [1]
        text = DRAFT.format(
            title=slug, domain=domain, slug=slug, conf=conf,
            cands=json.dumps(cands, ensure_ascii=False),
            extra=extra,
            body=body if body is not None else f"- 候选 {slug} 的一条判断",
            polish=polish if polish is not None else "一段通顺的话。")
        (wstore.root() / ".drafts" / f"{slug}.md").write_text(text, encoding="utf-8")
        return text

    def mk_page(self, slug, *, domain="tech", cands=(1,), body="一段话。", extra=""):
        text = DRAFT.format(
            title=slug, domain=domain, slug=slug, conf="high",
            cands=json.dumps(list(cands), ensure_ascii=False),
            extra=extra, body=f"- 候选 {slug}", polish=body)
        p = wstore.root() / "wiki" / domain / f"{slug}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def draft_files(self):
        return sorted(p.name for p in (wstore.root() / ".drafts").glob("*.md"))


class CommitHappyPathTest(CommitBase):
    def test_commits_all_drafts_and_clears_pending_state(self):
        self.mk_draft("alpha")
        self.mk_draft("beta", domain="ai")
        self.assertEqual(len(self.draft_files()), 2)
        self.assertTrue(wstore.approval_active())

        ok, payload = wstore.commit()

        self.assertTrue(ok, payload)
        self.assertEqual(sorted(payload["committed"]),
                         ["wiki/ai/beta.md", "wiki/tech/alpha.md"])
        self.assertEqual(payload["merged"], [])
        self.assertEqual(self.draft_files(), [], "草稿没清空 ⇒ 审批浮层入口永远挂着")
        self.assertFalse(wstore.approval_active(), "approval_active 是展示判据：草稿清空就该灭")
        self.assertTrue((wstore.root() / "wiki/tech/alpha.md").exists())
        self.assertTrue((wstore.root() / "wiki/ai/beta.md").exists())

    def test_index_and_log_updated(self):
        self.mk_draft("alpha")
        wstore.commit()
        index = (wstore.root() / "index.md").read_text(encoding="utf-8")
        self.assertIn("wiki/tech/alpha.md", index)
        self.assertTrue(wstore._has_log_action("commit"), "commit 没记 log ⇒ 事后查不到这批是什么时候进的")

    def test_commit_clears_approved_marks(self):
        """★ 整批入库后审批标记清空（含历史幽灵 slug）：
        标记只对「待审批草稿」有意义，页入库了标记就该灭。"""
        self.mk_draft("alpha")
        self.mk_draft("beta", domain="ai")
        wstore.toggle_approved("alpha")
        # 塞一个历史遗留的幽灵 slug：commit 要连它一起清掉（2026-11-09 实测
        # 「已审 2/9 但一个圈都没勾」就是这种旧账）
        wstore.write_approved(wstore.read_approved() + ["ghost-old-slug"])

        ok, payload = wstore.commit()

        self.assertTrue(ok, payload)
        self.assertEqual(wstore.read_approved(), [],
                         "入库后 approved.json 必须为空，不留幽灵")

    def test_empty_drafts_is_refused_not_crash(self):
        ok, err = wstore.commit()
        self.assertFalse(ok)
        self.assertIn("没有待审批草稿", err)


class CommitAtomicityTest(CommitBase):
    """★ 这一组是 5c 的核心：批量动作要么全做，要么一点不做。"""

    def test_one_bad_draft_rejects_whole_batch(self):
        self.mk_draft("good")
        # 缺 tags（REQUIRED_FM 里唯一的「键在即可」之外的字段）—— 直接写裸文件
        (wstore.root() / ".drafts" / "bad.md").write_text(
            "---\ntitle: bad\ndomain: tech\nsources: [x]\nupdated: 2026-11-06\n"
            "confidence: high\ncandidates: [2]\n---\n## 候选清单\n\n- 一条\n",
            encoding="utf-8")

        ok, err = wstore.commit()

        self.assertFalse(ok)
        self.assertIn("bad", err, "错误没点名是哪一份 ⇒ 人得自己逐份试")
        self.assertIn("tags", err, "没说是缺哪个字段")
        # 一个字节不动
        self.assertEqual(self.draft_files(), ["bad.md", "good.md"])
        self.assertFalse((wstore.root() / "wiki/tech/good.md").exists(),
                         "好草稿先被入库了 ⇒ 半途失败残局（最贵的错法）")
        self.assertFalse(wstore._has_log_action("commit"), "拒绝了却记了 commit 日志")

    def test_atomicity_when_good_sorts_first(self):
        """★ 这条才是「先全验」的**真正反证**：好草稿按 slug 排在坏草稿之前。

        `list_drafts()` 把 `complete=False` 的排最前，所以「缺字段」那类天然不会
        留下残局 —— 光靠它证明不了原子性。这里用「目标页已存在」（该草稿是完整的，
        排序靠后）：若实现改成逐份 approve，`aaa-good` 会先入库，然后 `zzz-dup`
        失败，留下「一半在 wiki、一半还是草稿」且每步都成功的残局。
        """
        self.mk_draft("aaa-good")
        self.mk_draft("zzz-dup")
        self.mk_page("zzz-dup", body="人上周确认过的内容。")

        ok, err = wstore.commit()

        self.assertFalse(ok)
        self.assertIn("zzz-dup", err)
        self.assertFalse((wstore.root() / "wiki/tech/aaa-good.md").exists(),
                         "好草稿先入库了 ⇒ 半途失败残局（「先全验」存在的全部理由）")
        self.assertEqual(self.draft_files(), ["aaa-good.md", "zzz-dup.md"])

    def test_bad_confidence_rejects_batch(self):
        self.mk_draft("good")
        self.mk_draft("bad", conf="sure")
        ok, err = wstore.commit()
        self.assertFalse(ok)
        self.assertIn("confidence", err)
        self.assertEqual(self.draft_files(), ["bad.md", "good.md"])
        self.assertFalse((wstore.root() / "wiki/tech/good.md").exists())

    def test_duplicate_target_page_rejects_batch(self):
        """目标已存在且没有 merges 声明 ⇒ 拒绝，且不许悄悄覆盖那一页。"""
        self.mk_draft("good")
        self.mk_draft("dup")
        self.mk_page("dup", body="人上周确认过的内容。")

        ok, err = wstore.commit()

        self.assertFalse(ok)
        self.assertIn("已存在同名页", err)
        self.assertEqual(self.draft_files(), ["dup.md", "good.md"])
        kept = (wstore.root() / "wiki/tech/dup.md").read_text(encoding="utf-8")
        self.assertIn("人上周确认过的内容", kept, "被覆盖了 ⇒ 静默删掉一页人的知识")


class CommitMergeTest(CommitBase):
    def test_merge_goes_through_backup(self):
        self.mk_page("moat", cands=(1, 2))
        self.mk_draft("moat-new", extra="merges: moat\n", cands=[1, 2, 3])

        ok, payload = wstore.commit()

        self.assertTrue(ok, payload)
        self.assertEqual(payload["merged"], ["wiki/tech/moat.md"])
        backups = list((wstore.root() / ".drafts/.merged").glob("moat-*.md"))
        self.assertEqual(len(backups), 1, "覆盖前没备份 ⇒ 旧版找不回来")
        self.assertIn("候选 moat", backups[0].read_text(encoding="utf-8"))
        page = (wstore.root() / "wiki/tech/moat.md").read_text(encoding="utf-8")
        self.assertNotIn("merges:", page, "merges 没被剥掉 ⇒ 下次编译以为还在等合并")

    def test_merge_losing_candidate_rejects_batch(self):
        self.mk_page("moat", cands=(1, 2))
        self.mk_draft("good")
        self.mk_draft("moat-new", extra="merges: moat\n", cands=[1])

        ok, err = wstore.commit()

        self.assertFalse(ok)
        self.assertIn("丢掉原有候选", err)
        self.assertIn("2", err)
        self.assertEqual(self.draft_files(), ["good.md", "moat-new.md"])
        self.assertFalse((wstore.root() / "wiki/tech/good.md").exists())

    def test_two_drafts_merging_into_same_page_rejects_batch(self):
        """★★ 用户实测：D1 / D8 各成一份 merges 同一页的稿，提交时后一份覆盖
        前一份，先入库的候选**静默消失**。逐份校验看不出这个错，必须跨稿拦。"""
        self.mk_page("moat", cands=(1,))
        self.mk_draft("moat-a", extra="merges: moat\n", cands=[1, 2])
        self.mk_draft("moat-b", extra="merges: moat\n", cands=[1, 3])

        ok, err = wstore.commit()

        self.assertFalse(ok)
        self.assertIn("同一页 moat", err)
        self.assertIn("moat-a", err)
        self.assertIn("moat-b", err)
        self.assertEqual(self.draft_files(), ["moat-a.md", "moat-b.md"],
                         "拒写时草稿一个都不许动")
        self.assertIn("候选 moat",
                      (wstore.root() / "wiki/tech/moat.md").read_text(encoding="utf-8"),
                      "被拒时目标页一个字节都不许动")


class BacklinksIndexTest(CommitBase):
    def test_backlinks_are_bidirectional_and_skip_self(self):
        self.mk_page("alpha", cands=(1,), body="讲 [[beta]] 的事，也提到 [[alpha]]。")
        self.mk_page("beta", cands=(2,), body="回看 [[alpha]]。")

        mapping = wstore.rebuild_backlinks_index()

        self.assertEqual(mapping["beta"], ["wiki/tech/alpha.md"])
        self.assertEqual(mapping["alpha"], ["wiki/tech/beta.md"],
                         "自引用不该进反链（alpha 提到 alpha 是噪音）")

    def test_dangling_link_is_kept(self):
        """断链也留一行 —— 悄悄丢掉 = 断链永远发现不了。"""
        self.mk_page("alpha", cands=(1,), body="链到 [[nobody]]。")
        mapping = wstore.rebuild_backlinks_index()
        self.assertEqual(mapping["nobody"], ["wiki/tech/alpha.md"])

    def test_commit_writes_backlinks_file(self):
        self.mk_page("alpha", cands=(1,), body="链到 [[beta]]。")
        self.mk_draft("beta", cands=[2])
        ok, _ = wstore.commit()
        self.assertTrue(ok)
        data = json.loads((wstore.root() / "wiki/backlinks.json").read_text(encoding="utf-8"))
        self.assertEqual(data["beta"], ["wiki/tech/alpha.md"])
        self.assertEqual(wstore.backlinks_of("beta"), ["wiki/tech/alpha.md"])

    def test_empty_repo_writes_empty_json(self):
        wstore.rebuild_backlinks_index()
        p = wstore.root() / "wiki/backlinks.json"
        self.assertTrue(p.exists(), "空库没写文件 ⇒ 被读成「索引没建」而不是「没有反链」")
        self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {})


class CommitViewTest(CommitBase):
    """视图只转发，不复制判据 —— 但「JSON 形状」本身是前端的契约，要钉住。"""

    def test_post_returns_json_and_commits(self):
        self.mk_draft("alpha")
        r = self.client.post("/wiki/approval/commit")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["committed"], ["wiki/tech/alpha.md"])
        self.assertEqual(self.draft_files(), [])

    def test_post_failure_keeps_error_text(self):
        self.mk_draft("bad", conf="sure")
        r = self.client.post("/wiki/approval/commit")
        data = r.json()
        self.assertFalse(data["ok"])
        self.assertIn("confidence", data["error"],
                      "错误被换成一句通用话 ⇒ 人不知道该修哪份")
        self.assertEqual(self.draft_files(), ["bad.md"])

    def test_get_is_rejected(self):
        """GET 会改文件（入库）⇒ 必须只接受 POST（预取/预览链接不该触发）。"""
        r = self.client.get("/wiki/approval/commit")
        self.assertEqual(r.status_code, 405)


class CommitPreviewTest(CommitBase):
    """影响预览：只报告，不改变「入库还是拒绝」的结论。"""

    def test_cross_page_candidate_is_reported_but_not_blocking(self):
        """同一候选已挂在别的页 ⇒ 进 `duplicate_candidates`，但照常入库。

        ★ 如果拿它当拒绝条件，`merges` 草稿永远过不去（合并的候选必然与目标页重叠）。
        """
        self.mk_page("old", cands=(7,))
        self.mk_draft("fresh", cands=[7])

        ok, payload = wstore.commit()

        self.assertTrue(ok, payload)
        self.assertEqual([x["candidate"] for x in payload["duplicate_candidates"]], [7],
                         "跨页重复候选没被报告 ⇒ 影响预览漏了一条应该给人看的信息")
        self.assertEqual(payload["duplicate_candidates"][0]["already_in"], "wiki/tech/old.md")
        self.assertTrue((wstore.root() / "wiki/tech/fresh.md").exists())

    def test_merge_draft_is_not_reported_as_duplicate(self):
        """合并草稿的候选与目标页重叠是**正常**的，不该报成重复。"""
        self.mk_page("moat", cands=(1, 2))
        self.mk_draft("moat-new", extra="merges: moat\n", cands=[1, 2, 3])
        ok, payload = wstore.commit()
        self.assertTrue(ok, payload)
        self.assertEqual(payload["duplicate_candidates"], [])


class ConversationResetTest(CommitBase):
    def test_commit_resets_conversation_for_next_batch(self):
        """2026-11-22：每轮审批从零开始，commit 后不留上一轮的消息。"""
        conv = wa.get_or_create_approval_conversation()
        conv.messages.create(role="user", body="把第 3 条挪去新页")
        conv.messages.create(role="assistant", body="已挪")
        self.mk_draft("alpha")
        ok, payload = wstore.commit()
        self.assertTrue(ok)
        self.assertTrue(payload["chat_reset"])
        self.assertIsNone(wa.approval_conversation(),
                          "commit 后还留着上一轮对话 ⇒ 下一轮会从旧上下文继续")
        fresh = wa.get_or_create_approval_conversation()
        self.assertEqual(list(fresh.messages.all()), [])
        # 批次信息不丢：落进 wiki 的 commit 日志，不靠聊天记录存档
        self.assertIn("commit", (wstore.root() / "log.md").read_text())

    def test_reset_failure_does_not_undo_commit(self):
        """清空失败不该让已入库的页「变回草稿」——那是更坏的错法。"""
        self.mk_draft("alpha")
        from unittest.mock import patch
        with patch.object(wa, "clear_approval_conversation",
                          side_effect=RuntimeError("db down")):
            ok, payload = wstore.commit()
        self.assertTrue(ok, "清空失败把整个 commit 判失败 ⇒ 页其实已经进了 wiki")
        self.assertFalse(payload["chat_reset"])
        self.assertTrue((wstore.root() / "wiki/tech/alpha.md").exists())
        self.assertEqual(self.draft_files(), [])
