"""划线/批注软删除与复活（改动 D）的反证（2026-11-05）。

语义只有一条：**软删 = 不想要**。不想要的东西，既不在页面展示、也不该再
拿去提炼溯源 —— 两者是同一个意思，判据收在 `LiveMarkQuerySet.live()` 一处。

★ 每条测试都冲着「静默」去：删了还留在列表里、删了又被喂给模型、
  复活却新建了一条、两表同 id 误报引用 —— 这些都不会报错，只会悄悄错。
"""
import json
import shutil
import tempfile

from django.test import Client, TestCase, override_settings

from core import ingest, marks
from core import wiki as wstore
from core.models import Annotation, Highlight, IngestCandidate

SID = "daily-2026-10-31"


def add_h(quoted="我划出的原文", note="", sid=SID):
    ok, payload = marks.add_highlight("report", sid, quoted, note=note)
    assert ok, payload
    return payload["id"]


def add_a(body="我的判断", sid=SID):
    ok, payload = marks.add_annotation("report", sid, body)
    assert ok, payload
    return payload["id"]


def mk_cand(quote, mark_id):
    return IngestCandidate.objects.create(
        batch="b", source_type="report", source_id=SID, kind="decision",
        claim=quote, quote=quote, quote_from="mine", domain="invest",
        confidence="high", mark_id=mark_id)


class SoftDeleteBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-softdelete-")
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

    def flash_of(self, kind, mark_id, sid=SID):
        """走真端点，把 HX-Trigger 里的 toast 文案解出来（中文是 \\uXXXX）。"""
        r = Client().post("/wiki/mark/delete", {
            "source_type": "report", "source_id": sid,
            "kind": kind, "id": str(mark_id),
        })
        self.assertEqual(r.status_code, 200)
        return json.loads(r["HX-Trigger"])["wikiFlash"]["msg"]


class SoftDeleteTest(SoftDeleteBase):
    """软删本身：不物理删、不显示、不重复计成功。"""

    def test_delete_keeps_the_row_and_stamps_deleted_at(self):
        hid = add_h()
        self.assertTrue(marks.delete_highlight(hid))
        h = Highlight.objects.get(pk=hid)
        self.assertIsNotNone(h.deleted_at, "软删要留行，溯源才不会断")
        self.assertFalse(Highlight.objects.live().filter(pk=hid).exists())

    def test_soft_deleted_highlight_is_not_listed(self):
        hid = add_h()
        marks.delete_highlight(hid)
        self.assertEqual(marks.list_marks("report", SID)["highlights"], [])

    def test_soft_deleted_annotation_is_not_listed(self):
        aid = add_a()
        marks.delete_annotation(aid)
        self.assertEqual(marks.list_marks("report", SID)["annotations"], [])

    def test_deleting_twice_does_not_pretend_success(self):
        hid = add_h()
        self.assertTrue(marks.delete_highlight(hid))
        self.assertFalse(marks.delete_highlight(hid), "重复点删除不算一次成功")

    def test_edit_mark_cannot_touch_a_soft_deleted_row(self):
        hid = add_h(note="旧备注")
        marks.delete_highlight(hid)
        ok, err = marks.edit_mark("highlight", hid, "新备注")
        self.assertFalse(ok)
        self.assertIn("不存在", err)


class RestoreTest(SoftDeleteBase):
    """复活：三者全同复用旧 id，任一不同就新建。"""

    def test_readd_same_highlight_restores_the_same_id(self):
        hid = add_h("同一句原文", note="旧备注")
        marks.delete_highlight(hid)
        ok, payload = marks.add_highlight("report", SID, "同一句原文", note="新备注")
        self.assertTrue(ok)
        self.assertTrue(payload.get("restored"))
        self.assertEqual(payload["id"], hid, "必须复用旧 id，别新建一条")
        self.assertEqual(Highlight.objects.filter(quoted_text="同一句原文").count(), 1)
        h = Highlight.objects.get(pk=hid)
        self.assertIsNone(h.deleted_at)
        self.assertEqual(h.note, "新备注", "重新写的备注不许静默丢掉")

    def test_different_quote_creates_a_new_row(self):
        hid = add_h("原文A")
        marks.delete_highlight(hid)
        ok, payload = marks.add_highlight("report", SID, "原文B")
        self.assertTrue(ok)
        self.assertNotEqual(payload["id"], hid)
        self.assertEqual(Highlight.objects.count(), 2)

    def test_same_quote_other_source_does_not_merge(self):
        hid = add_h("同一句", sid=SID)
        marks.delete_highlight(hid)
        ok, payload = marks.add_highlight("report", "daily-2026-11-01", "同一句")
        self.assertTrue(ok)
        self.assertNotEqual(payload["id"], hid, "来源不同就是独立痕迹")

    def test_annotation_restores_too(self):
        aid = add_a("我的判断")
        marks.delete_annotation(aid)
        ok, payload = marks.add_annotation("report", SID, "我的判断")
        self.assertTrue(ok)
        self.assertTrue(payload.get("restored"))
        self.assertEqual(payload["id"], aid)
        self.assertEqual(Annotation.objects.count(), 1)

    def test_restoring_an_undeleted_row_still_creates_a_new_one(self):
        """复活只针对软删记录；正常存在的同内容划线照旧新建（不误合并）。"""
        hid = add_h("同一句")
        ok, payload = marks.add_highlight("report", SID, "同一句")
        self.assertTrue(ok)
        self.assertNotEqual(payload["id"], hid)


class RefCountTest(SoftDeleteBase):
    """删除被引用痕迹时必须提示；两表同 id 不许误报。"""

    def test_delete_endpoint_tells_when_a_candidate_references_it(self):
        hid = add_h("被引用的一句")
        mk_cand("被引用的一句", hid)
        msg = self.flash_of("highlight", hid)
        self.assertIn("已删除", msg)
        self.assertIn("1 条候选引用", msg)

    def test_delete_endpoint_is_quiet_when_nothing_references_it(self):
        hid = add_h("孤立的一句")
        msg = self.flash_of("highlight", hid)
        self.assertNotIn("条候选引用", msg)

    def test_same_id_in_two_tables_does_not_false_positive(self):
        """★ Highlight 与 Annotation 是两张表、id 各自独立。

        造出两表同 pk=7777，只有 Annotation 的候选被引用；删 Highlight 时
        若只按 mark_id 数，会误报「已被 1 条候选引用」，让人以为删错了东西。
        """
        Highlight.objects.create(
            id=7777, source_type="report", source_id=SID,
            quoted_text="我划的原文", note="")
        Annotation.objects.create(
            id=7777, source_type="report", source_id=SID, body="完全无关的批注")
        mk_cand("完全无关的批注", 7777)
        self.assertEqual(marks.mark_ref_count("highlight", 7777), 0)
        self.assertEqual(marks.mark_ref_count("annotation", 7777), 1)

    def test_ref_count_matches_the_right_content(self):
        hid = add_h("我划的原文")
        mk_cand("我划的原文", hid)
        self.assertEqual(marks.mark_ref_count("highlight", hid), 1)


class NotWantedTest(SoftDeleteBase):
    """★ 软删 = 不想要：不进提炼素材，这条断了就是「删了还在喂给模型」。"""

    def test_soft_deleted_note_is_not_in_my_words(self):
        hid = add_h("原文", note="我的备注")
        self.assertIn("我的备注", [x["text"] for x in ingest.my_words("report", SID)["items"]])
        marks.delete_highlight(hid)
        self.assertNotIn("我的备注", [x["text"] for x in ingest.my_words("report", SID)["items"]])

    def test_soft_deleted_annotation_is_not_in_my_words(self):
        aid = add_a("我删掉的判断")
        marks.delete_annotation(aid)
        self.assertNotIn("我删掉的判断",
                         [x["text"] for x in ingest.my_words("report", SID)["items"]])

    def test_soft_deleted_highlight_is_not_in_signal_texts(self):
        hid = add_h("不想要的摘录")
        marks.delete_highlight(hid)
        self.assertNotIn("不想要的摘录", ingest.signal_texts("report", SID))

    def test_soft_deleted_mark_is_not_resolved_as_a_deep_link_anchor(self):
        """深链不该指向页面上根本不存在的痕迹（跳过去只会静默落空）。"""
        cand = mk_cand("不想要的摘录", None)
        hid = add_h("不想要的摘录")
        cand.quote = "不想要的摘录"
        self.assertEqual(ingest._mark_id_for(cand), hid)
        marks.delete_highlight(hid)
        self.assertIsNone(ingest._mark_id_for(cand))
