"""撞顶的第二种形态：**有正文但被切断**（半句话被当成摘要）。

`finish_reason=length` 有两种长相，处置必须分开：
  · 正文为空   → 推理吃光配额，翻倍重试能自愈（一直有）；
  · 有正文但断 → 默认只留证不重试（周报 32k 预算重打一次太贵），
                但对「输出有硬上限」的 S3 就是废稿 —— 649 次调用里发生过 4 次
                （2026-10-14 / 10-29 / 10-30 / 11-10 的 #8346）。

这里钉住四件事：
  1. 不开开关时行为不变（半截照用 + 只记一次）；
  2. 开开关后真的翻倍重试，且**成功后不许再记截断**（否则报告头重复点名）；
  3. 重试仍截断 / 重试连正文都没了，都只能记一次，且不许比不开更差；
  4. S3 那个调用点确实把开关打开了、entry_id 确实透传了 —— 光有开关没接线
     等于没修（本项目的老毛病）。
"""
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from core import citeguard
from core import costreport as cr
from core import summary


def fresh():
    return {"calls": 0, "tokens": 0, "reasoning_tokens": 0, "truncated": [],
            "errors": [], "budget_stop": False, "s3": 0}


def res(text, finish="length", ct=50):
    return cr.ChatResult(text, {"finish_reason": finish, "prompt_tokens": 100,
                                "completion_tokens": ct}, "", False, None)


class TruncatedRetryTest(SimpleTestCase):
    def test_default_keeps_partial_and_records_once(self):
        """反证：默认仍是「半截照用」，周报那条「重打太贵」的理由没被顺手改掉。"""
        s = fresh()
        with patch.object(cr, "chat_recorded", return_value=res("断在逗号上的半句")) as m:
            text, err = citeguard._call("summary_entry", "sys", "u", s, max_tokens=8000)
        self.assertEqual((text, err), ("断在逗号上的半句", ""))
        self.assertEqual(m.call_count, 1)
        self.assertEqual(len(s["truncated"]), 1)
        self.assertIn("预算 8000 tokens", s["truncated"][0])

    def test_flag_retries_with_doubled_budget(self):
        s = fresh()
        seq = [res("半句"), res("完整摘要", finish="stop")]
        with patch.object(cr, "chat_recorded", side_effect=seq) as m:
            text, err = citeguard._call("summary_entry", "sys", "u", s, max_tokens=8000,
                                        retry_on_truncated=True)
        self.assertEqual((text, err), ("完整摘要", ""))
        self.assertEqual([c.kwargs["max_tokens"] for c in m.call_args_list],
                         [8000, 16000], "第二次必须是翻倍后的预算")
        self.assertEqual(s["truncated"], [],
                         "重试成功了就不是截断，报告头不许再点名")

    def test_still_truncated_records_once_with_final_budget(self):
        s = fresh()
        with patch.object(cr, "chat_recorded", return_value=res("还是半句")) as m:
            text, _ = citeguard._call("summary_entry", "sys", "u", s, max_tokens=8000,
                                      retry_on_truncated=True)
        self.assertEqual(m.call_count, 2)
        self.assertEqual(text, "还是半句")
        self.assertEqual(len(s["truncated"]), 1, "只记一次；两次会让人以为两条都坏了")
        self.assertIn("16000", s["truncated"][0])

    def test_retry_empty_falls_back_to_partial(self):
        """重试连正文都没了 → 用第一次的半截兜底。

        没有这条，「开着开关」反而可能比「不开」输出更少：重试把推理全吃光、
        正文空，调用方就只剩空串。开关必须只增不减。
        """
        s = fresh()
        seq = [res("半句"), res("")]
        with patch.object(cr, "chat_recorded", side_effect=seq):
            text, err = citeguard._call("summary_entry", "sys", "u", s, max_tokens=8000,
                                        retry_on_truncated=True)
        self.assertEqual((text, err), ("半句", ""))
        self.assertEqual(len(s["truncated"]), 1)
        self.assertIn("预算 8000 tokens", s["truncated"][0], "记的是半截来自哪一档预算")

    def test_flag_does_not_enable_empty_retry(self):
        """只开 truncated 的调用，正文为空时不许悄悄多花一次钱。"""
        s = fresh()
        with patch.object(cr, "chat_recorded", return_value=res("")) as m:
            text, err = citeguard._call("summary_entry", "sys", "u", s, max_tokens=8000,
                                        retry_on_empty=False, retry_on_truncated=True)
        self.assertEqual(m.call_count, 1)
        self.assertEqual(text, "")
        self.assertIn("推理占满", err)

    def test_entry_id_reaches_the_ledger(self):
        """报告头写「S3 单篇 #8346」，成本页得能对回那篇文章。"""
        s = fresh()
        with patch.object(cr, "chat_recorded", return_value=res("正文", "stop")) as m:
            citeguard._call("summary_entry", "sys", "u", s, max_tokens=8000, entry_id=8346)
        self.assertEqual(m.call_args.kwargs["entry_id"], 8346)


class S3WiringTest(SimpleTestCase):
    """★ 开关在 `_s3_pass` 的调用点上；那里没接就是没修（本项目踩过一次）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.override = override_settings(SUMMARY_CACHE_DIR=Path(self.tmp.name),
                                          SUMMARY_CACHE_ENABLED=True)
        self.override.enable()
        self.addCleanup(self.override.disable)

    def _items(self):
        return [{"id": 8346, "kind": "normal", "raw_len": 5000,
                 "text": "很长的正文" * 200, "title": "AI毛绒翻车惨案频发",
                 "feed_id": 89, "category_id": None}]

    def test_s3_opens_truncated_retry_and_passes_entry_id(self):
        items = self._items()
        with patch.object(summary, "_call", return_value=("摘要正文" * 20, "")) as call:
            summary._s3_pass(items, fresh(), None, no_cache=True)
        kw = call.call_args.kwargs
        self.assertTrue(kw["retry_on_truncated"], "S3 必须开这个开关")
        self.assertEqual(kw["entry_id"], 8346)
        self.assertEqual(kw["max_tokens"], settings.SUMMARY_S3_MAX_TOKENS,
                         "S3 的预算必须来自 settings（别处写死就改不动了）")

    def test_s3_budget_covers_reasoning_not_just_output(self):
        """3000 是「100–200 字输出 × 10」的直觉值，实测被推理链打穿。

        2026-11-10 实测（`LlmUsage` 逐行）：649 次 S3 调用，29 次输出正好卡在
        3000（4.5%），其中 25 次正文为空、翻到 6000 全部写完（峰值 5877），
        另 4 次把半句话当摘要。这条只是**下限守卫**：谁把它调回 3000 会当场变红，
        不是让人照着这个数去调预算。
        """
        self.assertGreaterEqual(settings.SUMMARY_S3_MAX_TOKENS, 6000)
