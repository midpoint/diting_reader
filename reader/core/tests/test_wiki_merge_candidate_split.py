"""2026-11-13：并入 / 替换草稿审批时，候选要区分「本次新增」与「历史已在页里」。

★ 单测钉源码事实：`_approval_workarea_ctx` 必须按「被覆盖页当前 candidates」把
  草稿候选分成 new / historical，并把计数与逐条 is_new 交给模板。
★ 真浏览器 e2e 另验「徽标真的画出来、无脏东西」；这里只验判据不漂。
★ 每条结构断言都配一条可见文字 / 计数断言，避免空集合假绿。
"""
from django.template.loader import render_to_string

from apps.wiki import views as wv
from core import wiki as wstore
from core.tests.test_wiki_ui_step11 import UiBase, mk_cand


def _publish(slug, cand_ids):
    d = wstore.root() / "wiki" / "invest"
    d.mkdir(parents=True, exist_ok=True)
    (d / ("%s.md" % slug)).write_text(
        "---\ntitle: %s\ndomain: invest\nsources: [entry:9101]\n"
        "updated: 2026-11-09\nconfidence: medium\ntags: []\n"
        "candidates: [%s]\n---\n\n正文\n" % (slug, ", ".join(str(i) for i in cand_ids)),
        encoding="utf-8")


def _merge_draft(slug, target, cand_ids):
    (wstore.root() / ".drafts" / ("%s.md" % slug)).write_text(
        "---\ntitle: %s\ndomain: invest\nmerges: %s\nsources: [entry:9101]\n"
        "updated: 2026-11-09\nconfidence: medium\ntags: []\n"
        "candidates: [%s]\n---\n\n正文\n"
        % (slug, target, ", ".join(str(i) for i in cand_ids)),
        encoding="utf-8")


def _edit_draft(slug, target, cand_ids):
    (wstore.root() / ".drafts" / ("%s.md" % slug)).write_text(
        "---\ntitle: %s\ndomain: invest\nedits: %s\nsources: [entry:9101]\n"
        "updated: 2026-11-09\nconfidence: medium\ntags: []\n"
        "candidates: [%s]\n---\n\n正文\n"
        % (slug, target, ", ".join(str(i) for i in cand_ids)),
        encoding="utf-8")


class TestMergeCandidateSplit(UiBase):
    """并入草稿审批：新增候选必须标出来，历史候选不标，计数要对。"""

    def test_merge_marks_new_vs_historical(self):
        c1, c2, c3 = mk_cand("老1"), mk_cand("老2", sid="9102"), mk_cand("老3", sid="9103")
        c4, c5 = mk_cand("新4", sid="9104"), mk_cand("新5", sid="9105")
        _publish("tp", [c1.id, c2.id, c3.id])
        _merge_draft("md", "tp", [c1.id, c2.id, c3.id, c4.id, c5.id])

        ctx = wv._approval_workarea_ctx("md")
        self.assertTrue(ctx["ok"])
        # ★ 计数判据：本批新增 2、历史 3，且草稿候选总数 5（不重不漏）
        self.assertEqual(ctx["merge_new_n"], 2)
        self.assertEqual(ctx["merge_hist_n"], 3)
        self.assertEqual(len(ctx["candidate_detail"]), 5)
        by_id = {c["cand_id"]: c for c in ctx["candidate_detail"]}
        self.assertFalse(by_id[c1.id]["is_new"], "历史候选不该标新增")
        self.assertFalse(by_id[c2.id]["is_new"])
        self.assertFalse(by_id[c3.id]["is_new"])
        self.assertTrue(by_id[c4.id]["is_new"], "本批新来的必须标新增")
        self.assertTrue(by_id[c5.id]["is_new"])

    def test_template_renders_badge_count_for_merge(self):
        c1 = mk_cand("老1")
        c4, c5 = mk_cand("新4", sid="9104"), mk_cand("新5", sid="9105")
        _publish("tp", [c1.id])
        _merge_draft("md", "tp", [c1.id, c4.id, c5.id])
        ctx = wv._approval_workarea_ctx("md")
        html = render_to_string("wiki/_approval_workarea.html", ctx)
        # 两条新增 → 两个 badge；徽标文字「新增」必须出现且只出现两次
        self.assertEqual(html.count('class="badge-new"'), 2)
        self.assertEqual(html.count(">新增<"), 2)
        # 计数说明必须可见：本批新增 2 / 历史 1（数字在 <b> 内，断言断开的可见文字）
        self.assertIn("新增 2", html)
        self.assertIn("条历史候选", html)

    def test_plain_new_page_draft_has_no_split(self):
        """反证：普通新页草稿（无 merges / edits）不该出现新旧拆分噪音。"""
        c1 = mk_cand()
        (wstore.root() / ".drafts" / "fresh.md").write_text(
            "---\ntitle: 新页\ndomain: invest\nsources: [entry:9101]\n"
            "updated: 2026-11-09\nconfidence: medium\ntags: []\n"
            "candidates: [%d]\n---\n\n正文\n" % c1.id, encoding="utf-8")
        ctx = wv._approval_workarea_ctx("fresh")
        self.assertEqual(ctx["merge_new_n"], 0)
        self.assertEqual(ctx["merge_hist_n"], 0)
        html = render_to_string("wiki/_approval_workarea.html", ctx)
        self.assertNotIn("历史候选", html)
        self.assertEqual(html.count('class="badge-new"'), 0)

    def test_edit_draft_also_splits(self):
        """替换稿（edits）同样覆盖一页：新增候选也该标出来。"""
        c1 = mk_cand("老1")
        c4 = mk_cand("新4", sid="9104")
        _publish("ep", [c1.id])
        _edit_draft("ed", "ep", [c1.id, c4.id])
        ctx = wv._approval_workarea_ctx("ed")
        self.assertTrue(ctx["ok"])
        self.assertEqual(ctx["merge_new_n"], 1)
        by_id = {c["cand_id"]: c for c in ctx["candidate_detail"]}
        self.assertFalse(by_id[c1.id]["is_new"])
        self.assertTrue(by_id[c4.id]["is_new"])
