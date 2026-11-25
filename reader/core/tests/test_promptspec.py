"""prompt 四层配置的合并规则：拼接不覆盖 / 本源优先 / 备注不是判据。

「拼接而非覆盖」与「note 不参与 has_any」都是设计决定（覆盖会丢分类主判据；
只写备注的源不该花钱调模型）。这组测试防止后续改动无意破坏分层。
"""
from django.test import TestCase

from core import promptspec
from core.models import CategoryTriageConfig, FeedTriageConfig


class MergeLevelTest(TestCase):
    def test_concat_not_override(self):
        merged = promptspec.merge_level("分类判据", "本源判据")
        self.assertEqual(merged["text"], "分类判据\n本源判据")
        self.assertEqual([p["from"] for p in merged["parts"]], ["category", "feed"])

    def test_same_line_deduped(self):
        merged = promptspec.merge_level("同一句。", "同一句。")
        self.assertEqual(merged["text"], "同一句。")

    def test_empty_returns_none(self):
        self.assertIsNone(promptspec.merge_level("", "  "))
        self.assertIsNone(promptspec.merge_level(None, None))


class BuildSpecTest(TestCase):
    def test_no_config_is_zero_cost_path(self):
        spec = promptspec.build_spec(1, feed_title="t")
        self.assertFalse(spec["has_any"])
        self.assertEqual(spec["default_level"], 2)      # feed 不配即全局 L2

    def test_note_alone_is_not_criteria(self):
        FeedTriageConfig.objects.create(feed_id=2, note="正文是三层复合文本")
        spec = promptspec.build_spec(2)
        self.assertFalse(spec["has_any"])               # 只有备注 ⇒ 依然不调 LLM
        self.assertEqual(spec["note"], "正文是三层复合文本")

    def test_category_and_feed_concatenated(self):
        CategoryTriageConfig.objects.create(category_id=5, l2_text="分类的 L2")
        FeedTriageConfig.objects.create(feed_id=3, l2_text="本源的 L2",
                                        l3_text="本源的 L3", default_level=1)
        spec = promptspec.build_spec(3, category_id=5)
        self.assertTrue(spec["has_any"])
        self.assertIn("分类的 L2", spec["l2"]["text"])
        self.assertIn("本源的 L2", spec["l2"]["text"])
        self.assertEqual([p["from"] for p in spec["l2"]["parts"]],
                         ["category", "feed"])
        self.assertEqual(spec["l3"]["parts"][0]["from"], "feed")
        self.assertEqual(spec["default_level"], 1)      # 默认级别只看 feed

    def test_default_level_always_2_without_feed_config(self):
        CategoryTriageConfig.objects.create(category_id=6, l1_text="分类判据")
        spec = promptspec.build_spec(9, category_id=6)
        self.assertEqual(spec["default_level"], 2)      # 分类没有 default_level

    def test_source_hash_changes_only_when_inputs_change(self):
        FeedTriageConfig.objects.create(feed_id=4, l1_text="判据")
        a = promptspec.build_spec(4)
        self.assertEqual(promptspec.source_hash(a),
                         promptspec.source_hash(promptspec.build_spec(4)))
        FeedTriageConfig.objects.filter(feed_id=4).update(note="新备注")
        self.assertNotEqual(promptspec.source_hash(a),
                            promptspec.source_hash(promptspec.build_spec(4)))


class ChangeNoteTest(TestCase):
    def test_diff_names_the_level(self):
        note = promptspec.change_note({"l2": {"text": "旧"}},
                                      {"l2": {"text": "新"}, "default_level": 3})
        self.assertIn("L2", note)
        self.assertIn("default", note)

    def test_first_version(self):
        self.assertEqual(promptspec.change_note(None, {"l2": {"text": "x"}}), "首版")


class RenderRuleTest(TestCase):
    def test_rule_fallback_mentions_default_and_scope(self):
        # 0 token 兜底版也必须写清默认级别兜底与判据区（AI 抽风时的下界）
        spec = promptspec.build_spec(9)
        text = promptspec.render_rule(spec)
        self.assertIn("L2", text)
        self.assertIn("本源未配置任何判据", text)
        self.assertIn("各级判据", text)
