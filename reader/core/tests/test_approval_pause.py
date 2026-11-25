"""全局暂停（改动 E）的守卫反证（第三步，2026-11-05；2026-11-09 用户澄清恢复）。

盯的是"静默失败最贵"那条：审批期间若偷偷跑了一次提炼 / 编译，新的候选与
草稿会涌进审批上下文，把「当前 .drafts/ 全量 = 一批」的边界冲掉；而页面
200、日志干净，谁都不知道。

★ 但暂停的是「自动触发」，**不是记录**：划线 / 批注照常落库。这条同样要有
  反证 —— 只验「暂停了」不验「没误伤」，最容易写出一个把 marks 也关掉的实现。

★ 2026-11-09 用户澄清：原「改动 E」的暂停逻辑是对的（审批未完成时拒绝新
  编译），问题出在编译**过程中**草稿分批产生时也被阻止。现在两者分开：
  编译中由 compile_run.running 挡，编译后待审批由 approval_active 挡。

花钱端点一律打桩（`core.citeguard._call`），且配「桩真被用上」的反证：
解除暂停后同一个调用必须真的走到模型，否则 `assert_not_called()` 也可能是
因为链路压根没通（假绿）。
"""
import json
import shutil
import tempfile
from unittest.mock import patch

from django.test import TestCase, override_settings

from core import ingest
from core import marks
from core import wiki as wstore
from core.models import Annotation, IngestCandidate

BLOCK = {"title": "测试文章", "text": "这是作者的正文，不是我的判断。",
         "html": "<p>这是作者的正文，不是我的判断。</p>",
         "feed_title": "", "published_at": None}
SID = "5730"


def _draft_text(ids):
    return ("---\ntitle: 待审批页\ndomain: invest\nsources: [entry:5730]\n"
            "updated: 2026-11-05\nconfidence: medium\ntags: []\n"
            f"candidates: [{', '.join(str(i) for i in ids)}]\n---\n\n正文\n")


class PauseBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-approval-pause-")
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

    def add_draft(self, slug="d1", ids=(1,)):
        p = wstore.root() / ".drafts" / f"{slug}.md"
        p.write_text(_draft_text(ids), encoding="utf-8")
        return p

    def clear_drafts(self):
        for p in (wstore.root() / ".drafts").glob("*.md"):
            p.unlink()

    def mk_word(self, body="我认为暂停不该挡住划线"):
        """造一条「我的字」，让 extract 的信号闸能过（min_chars=1）。"""
        return Annotation.objects.create(source_type="entry", source_id=SID,
                                         body=body)


class ApprovalActiveTest(PauseBase):
    def test_empty_drafts_means_not_active(self):
        self.assertFalse(wstore.approval_active())

    def test_any_md_makes_it_active(self):
        self.add_draft()
        self.assertTrue(wstore.approval_active())

    def test_discarded_dir_does_not_count(self):
        """归档区不是待审批：不能让 .discarded/ 里的旧草稿把展示永久钉住。"""
        box = wstore.root() / ".drafts" / ".discarded"
        box.mkdir(parents=True, exist_ok=True)
        (box / "old.md").write_text(_draft_text([1]), encoding="utf-8")
        self.assertFalse(wstore.approval_active())

    def test_empty_after_commit_releases(self):
        """审批结束 = .drafts/ 空 ⇒ 自动释放，不靠人点「结束」。"""
        p = self.add_draft()
        self.assertTrue(wstore.approval_active())
        p.unlink()
        self.assertFalse(wstore.approval_active())

    def test_judgement_is_one_function_not_a_db_flag(self):
        """★ DB 里留着审批对话也不算 active（对话留着是给人回看的）。"""
        from core.models import Conversation
        Conversation.objects.create(source_type="draftbatch", source_id="current")
        self.assertFalse(wstore.approval_active())


class ExtractPausedTest(PauseBase):
    def _extract(self):
        with patch("core.citeguard._call",
                   return_value=('{"candidates": []}', "")) as call, \
                patch("core.sourceblock.load", return_value=BLOCK):
            res = ingest.extract("entry", SID)
        return res, call

    def test_paused_extract_never_calls_model(self):
        """★ 有草稿待审批时，提炼必须被暂停（2026-11-09 用户澄清恢复）。"""
        self.add_draft()
        self.mk_word()
        res, call = self._extract()
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("paused"))
        self.assertIn("审批进行中", res["err"])
        call.assert_not_called()

    def test_stub_really_gets_used(self):
        """★ 桩真被用上的反证：解除暂停后，同一条链路必须真的走到模型。"""
        self.mk_word()
        res, call = self._extract()
        self.assertTrue(res["ok"], res)
        call.assert_called_once()


class CompilePausedTest(PauseBase):
    def _compile(self):
        with patch("core.citeguard._call",
                   return_value=('{"candidates": []}', "")) as call:
            res = ingest.compile_candidates()
        return res, call

    def test_paused_compile_is_refused_before_spending(self):
        """★ 有草稿待审批时，编译必须被暂停（2026-11-09 用户澄清恢复）。"""
        self.add_draft()
        IngestCandidate.objects.create(
            batch="t", source_type="entry", source_id=SID, kind="decision",
            claim="一条判断", quote="我的原话", status="pending")
        res, call = self._compile()
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("paused"))
        self.assertIn("审批进行中", res["err"])
        call.assert_not_called()

    def test_backlog_exposes_paused_without_changing_todo(self):
        """`paused` 是只读标记：不能把「还有几条活」混成「审批状态」。

        ★ 草稿的 candidates 故意用 999999（不存在的 id）：若用真候选 id，
          `_compiled_ids()` 会把它算成「已编译」，todo 自然掉 1 —— 那是另一个
          机制在动，测不出 `paused` 有没有污染 backlog。
        """
        c = IngestCandidate.objects.create(
            batch="t", source_type="entry", source_id=SID, kind="decision",
            claim="一条判断", quote="我的原话", status="pending")
        free = ingest.compile_backlog()
        self.assertFalse(free.get("paused"))
        self.add_draft(ids=(999999,))
        busy = ingest.compile_backlog()
        self.assertTrue(busy.get("paused"))
        self.assertEqual(busy["todo"], free["todo"], "暂停不该改「还有几条活」")
        self.assertEqual([x.id for x in busy["cands"]], [c.id])


class MarksNotPausedTest(PauseBase):
    def test_highlight_and_annotation_land_with_drafts_present(self):
        """★ 痕迹记录从来不受审批状态影响：有草稿时划线/批注照常落库。"""
        self.add_draft()
        ok1, p1 = marks.add_highlight("entry", SID, "作者说成本是核心")
        ok2, p2 = marks.add_annotation("entry", SID, "我的判断：成本优先")
        self.assertTrue(ok1, p1)
        self.assertTrue(ok2, p2)
        rows = marks.list_marks("entry", SID)
        self.assertEqual(len(rows["highlights"]), 1)
        self.assertEqual(len(rows["annotations"]), 1)

    def test_extract_sees_words_written_while_drafts_exist(self):
        """攒下来的痕迹，解暂停后要真的能被提炼读到（不是只存着用不上）。"""
        self.add_draft()
        marks.add_annotation("entry", SID, "我在审批期间写的判断")
        self.assertGreater(ingest.my_words("entry", SID)["chars"], 0)
        self.clear_drafts()
        with patch("core.citeguard._call", return_value=('{"candidates": []}', "")), \
                patch("core.sourceblock.load", return_value=BLOCK):
            res = ingest.extract("entry", SID)
        self.assertTrue(res["ok"], res)
