"""2026-11-13（晚）：退稿 = 内容级否定 —— 候选一并废弃，不再自动复活。

背景（真实库实测）：16:53 退「美股情绪与杠杆」（理由「看图看线」）、16:58 退
「推理算力的供给与付费」（理由「不重要」），20:10 下一轮编译把两条候选又编回
草稿（一份同名、一份换新名）—— 旧语义「候选回池、下次重编」与用户的内容级
否定对不上，用户拍板：退稿连带废弃候选，反悔走候选面板改判回待审。

★ 判据分层：这里钉「DB 状态 / 哪些候选不许误杀 / 回执与日志」；
  「退稿后浮层还在」在 test_wiki_discard_stay，依赖同步在 test_wiki_discard_sync，
  「改判后才能重编」在 test_ingest_compile。
★ 误杀防护是本次变更最贵的边界：合并归档 / 编辑稿 / 搬迁稿都走同一个
  discard()，那些候选已经在新家里，废弃它们 = 新产物挂幽灵引用。
"""
import json
from unittest.mock import patch

from django.urls import reverse

from core import wiki as wstore
from core import wiki_agent as wa
from core.tests.test_wiki_agent import WikiAgentBase, _draft_text
from core.tests.test_wiki_ui_step11 import UiBase, mk_cand, write_draft


class DiscardCandidatesTest(WikiAgentBase):
    def test_discard_marks_candidates_discarded_and_reports(self):
        a, b = self.mk_cand(1), self.mk_cand(2)
        self.mk_draft("p", [a.id, b.id])
        report = {}
        ok, dest = wstore.discard("p", "看图看线", sync_report=report)
        self.assertTrue(ok, dest)
        for c in (a, b):
            c.refresh_from_db()
            self.assertEqual(c.status, "discarded",
                             "候选还活着 = 下一轮编译原地复活")
        self.assertEqual(report["released_candidates"], [a.id, b.id])
        self.assertNotIn("release_error", report)
        log = (wstore.root() / "log.md").read_text(encoding="utf-8")
        self.assertIn("看图看线", log)
        self.assertIn("候选 2 条一并废弃", log, "废弃了哪些要留痕，事后能查")

    def test_candidates_still_in_other_draft_survive(self):
        """同一候选挂在两份稿上（合并写盘后、归档前的真实形态）：只废弃没下家的。"""
        a, b = self.mk_cand(1), self.mk_cand(2)
        self.mk_draft("gone", [a.id, b.id])
        self.mk_draft("keeper", [b.id])
        report = {}
        ok, _ = wstore.discard("gone", "不要这页", sync_report=report)
        self.assertTrue(ok)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.status, "discarded")
        self.assertEqual(b.status, "pending", "已有下家的候选误杀 = 幽灵引用")
        self.assertEqual(report["released_candidates"], [a.id])

    def test_merge_archive_does_not_discard_merged_candidates(self):
        """真实合并路径：旧稿经 discard 归档，候选已进合并稿，一条都不许废弃。"""
        a, b = self.mk_cand(1), self.mk_cand(2)
        self.mk_draft("p-a", [a.id])
        self.mk_draft("p-b", [b.id])
        ok, payload = wa.set("merge", {"pages": ["p-a", "p-b"], "to": "p-c"})
        self.assertTrue(ok, payload)
        for c in (a, b):
            c.refresh_from_db()
            self.assertEqual(c.status, "pending",
                             "合并归档误杀候选 = 合并稿挂幽灵引用")

    def test_edit_draft_discard_keeps_page_candidates(self):
        """退编辑稿：候选还挂在已入库页上，页不动、候选不废弃。"""
        a = self.mk_cand(1)
        d = wstore.root() / "wiki" / "invest"
        d.mkdir(parents=True, exist_ok=True)
        (d / "kept.md").write_text(_draft_text([a.id]), encoding="utf-8")
        (wstore.root() / ".drafts" / "kept.md").write_text(
            _draft_text([a.id]).replace("tags: []", "tags: []\nedits: kept"),
            encoding="utf-8")
        report = {}
        ok, _ = wstore.discard("kept", "这次编辑不要", sync_report=report)
        self.assertTrue(ok)
        a.refresh_from_db()
        self.assertEqual(a.status, "pending", "已入库页的候选不随编辑稿废弃")
        self.assertEqual(report["released_candidates"], [])
        self.assertTrue((d / "kept.md").exists(), "退编辑稿绝不碰已入库页")

    def test_release_failure_reported_not_rolled_back(self):
        """DB 写失败：草稿已归档不回滚，但失败必须进回执与日志，不静默。"""
        a = self.mk_cand(1)
        self.mk_draft("p", [a.id])
        report = {}
        with patch("core.wiki._release_candidates",
                   return_value=(0, "候选废弃失败（模拟）")):
            ok, _ = wstore.discard("p", "不要", sync_report=report)
        self.assertTrue(ok, "文件已挪走，不能因 DB 失败假装没退稿")
        self.assertFalse((wstore.root() / ".drafts" / "p.md").exists())
        self.assertEqual(report["release_error"], "候选废弃失败（模拟）")
        self.assertEqual(report["released_candidates"], [a.id])
        a.refresh_from_db()
        self.assertEqual(a.status, "pending", "失败就是没废弃，状态不许假装")
        log = (wstore.root() / "log.md").read_text(encoding="utf-8")
        self.assertIn("候选废弃失败", log)


class DiscardFlashTest(UiBase):
    def test_flash_mentions_discarded_candidates(self):
        """回执必须说清候选去向：只说「已退稿」= 下次又问「怎么又回来了」。"""
        c = mk_cand()
        write_draft("gone", [c.id])
        resp = self.client.post(reverse("wiki_discard"),
                                {"from": "approval", "slug": "gone",
                                 "reason": "不重要"})
        self.assertEqual(resp.status_code, 200)
        flash = json.loads(resp.get("HX-Trigger", "{}"))
        msg = flash.get("wikiFlash", {}).get("msg", "")
        self.assertIn("已退稿", msg)
        self.assertIn("候选 1 条一并废弃", msg)
        c.refresh_from_db()
        self.assertEqual(c.status, "discarded")