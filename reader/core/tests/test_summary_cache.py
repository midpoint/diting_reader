"""S3/S2/S1 文件缓存：输入指纹不变才复用，过期/坏文件不命中。"""
import json
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, override_settings

from core import summarycache


class SummaryCacheTest(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.override = override_settings(
            SUMMARY_CACHE_DIR=Path(self.tmp.name),
            SUMMARY_CACHE_ENABLED=True,
            SUMMARY_CACHE_TTL_DAYS=7)
        self.override.enable()
        self.addCleanup(self.override.disable)

    def test_same_input_hits_even_if_dict_order_differs(self):
        summarycache.put("s3", 1, {"a": 1, "b": [2, 3]}, "hello")
        self.assertEqual(summarycache.get("s3", 1, {"b": [2, 3], "a": 1}), "hello")

    def test_changed_input_misses(self):
        summarycache.put("s2", 7, {"material": "old"}, "old text")
        self.assertIsNone(summarycache.get("s2", 7, {"material": "new"}))

    def test_scope_and_layer_are_part_of_key(self):
        summarycache.put("s2", 7, {"material": "x"}, "text")
        self.assertIsNone(summarycache.get("s1", 7, {"material": "x"}))
        self.assertIsNone(summarycache.get("s2", 8, {"material": "x"}))

    def test_empty_text_not_cached(self):
        summarycache.put("s3", 1, {"x": 1}, "")
        self.assertIsNone(summarycache.get("s3", 1, {"x": 1}))

    def test_disabled_returns_none(self):
        summarycache.put("s3", 1, {"x": 1}, "text")
        with override_settings(SUMMARY_CACHE_ENABLED=False):
            self.assertIsNone(summarycache.get("s3", 1, {"x": 1}))

    def test_expired_file_is_removed(self):
        summarycache.put("s3", 1, {"x": 1}, "text")
        p = next(Path(self.tmp.name).glob("*.json"))
        data = json.loads(p.read_text(encoding="utf-8"))
        data["expires_at"] = "2000-01-01T00:00:00+00:00"
        p.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(summarycache.get("s3", 1, {"x": 1}))
        self.assertFalse(p.exists())

    def test_purge_expired(self):
        summarycache.put("s3", 1, {"x": 1}, "text")
        p = next(Path(self.tmp.name).glob("*.json"))
        data = json.loads(p.read_text(encoding="utf-8"))
        data["expires_at"] = "2000-01-01T00:00:00+00:00"
        p.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(summarycache.purge_expired(), 1)
        self.assertFalse(p.exists())


class CachedCallTest(SimpleTestCase):
    """缓存命中要先于调用闸；否则调用上限到了，有缓存也复用不了。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.override = override_settings(
            SUMMARY_CACHE_DIR=Path(self.tmp.name),
            SUMMARY_CACHE_ENABLED=True,
            SUMMARY_CACHE_TTL_DAYS=7)
        self.override.enable()
        self.addCleanup(self.override.disable)

    def _stats(self):
        return {"calls": 999, "tokens": 0, "reasoning_tokens": 0,
                "s3": 0, "s2": 0, "s1": 0, "s0": 0,
                "reused": {"s3": 0, "s2": 0, "s1": 0},
                "truncated": [], "errors": [], "budget_stop": False,
                "gate_stop": False, "cite_missing": []}

    def test_hit_does_not_call_or_count(self):
        from core import summary
        payload = {"x": 1}
        summarycache.put("s3", 1, payload, "cached text")
        stats = self._stats()
        called = []
        text, err, hit = summary._cached(
            "s3", 1, payload, stats, lambda: called.append(1) or ("new", ""))
        self.assertEqual((text, err, hit), ("cached text", "", True))
        self.assertEqual(called, [])
        self.assertEqual(stats["reused"]["s3"], 1)

    def test_no_cache_bypasses_read(self):
        from core import summary
        payload = {"x": 1}
        summarycache.put("s3", 1, payload, "cached text")
        stats = self._stats()
        text, err, hit = summary._cached(
            "s3", 1, payload, stats, lambda: ("new text", ""), no_cache=True)
        self.assertEqual((text, err, hit), ("new text", "", False))


class GenerationParamsNotInKeyTest(SimpleTestCase):
    """改预算/温度只影响下一次生成，不该让已成功的摘要重跑。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.override = override_settings(
            SUMMARY_CACHE_DIR=Path(self.tmp.name),
            SUMMARY_CACHE_ENABLED=True,
            SUMMARY_CACHE_TTL_DAYS=7)
        self.override.enable()
        self.addCleanup(self.override.disable)

    def _stats(self):
        return {"calls": 0, "tokens": 0, "reasoning_tokens": 0,
                "s3": 0, "s2": 0, "s1": 0, "s0": 0,
                "reused": {"s3": 0, "s2": 0, "s1": 0},
                "truncated": [], "errors": [], "budget_stop": False,
                "gate_stop": False, "cite_missing": []}

    def test_budget_change_keeps_s3_cache(self):
        from unittest.mock import patch
        from core import summary
        item = {"id": 9, "feed_id": 1, "category_id": 2, "title": "长文",
                "text": "正文" * 2000, "raw_len": 4000, "kind": "normal",
                "summary": "", "truncated": False, "url": "https://e.com/9",
                "rated": True, "level": 2, "published_at": None}
        stats = self._stats()
        with patch.object(summary, "_call",
                          return_value=("摘要" * 30, "")) as call:
            summary._s3_pass([item], stats, None)
            self.assertEqual(call.call_count, 1)
        item["summary"] = ""
        stats2 = self._stats()
        with override_settings(SUMMARY_S3_MAX_TOKENS=9999):
            with patch.object(summary, "_call",
                              return_value=("不应调用", "")) as call2:
                summary._s3_pass([item], stats2, None)
                self.assertEqual(call2.call_count, 0)
        self.assertEqual(stats2["reused"]["s3"], 1)
