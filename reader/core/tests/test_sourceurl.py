"""sourceblock.source_url 的映射测试 —— 收件箱/候选「跳回原文」的唯一链接来源。

★ 为什么要单独测：来源跳转的 URL 拼法散在收件箱、候选卡、已入库页反查三处，
  只在一处测、另一处改漏了，就是「日报能点开、文章点了没反应」。source_url
  是唯一实现，这里钉住每个 source_type 的映射。
"""
from unittest.mock import patch

from django.test import TestCase

from core import sourceblock
from core.models import ChatMessage, Conversation


class SourceUrlTest(TestCase):
    def test_report_daily(self):
        self.assertEqual(sourceblock.source_url("report", "daily-2026-10-31"),
                         "/summary/2026-10-31")

    def test_report_weekly(self):
        self.assertEqual(sourceblock.source_url("report", "weekly-2026-11-01"),
                         "/summary/weekly/2026-11-01")

    def test_report_unknown_sid(self):
        self.assertIsNone(sourceblock.source_url("report", "not-a-date"))

    def test_entry_deep_link(self):
        with patch.object(sourceblock, "_entry_feed_id", return_value=63):
            self.assertEqual(sourceblock.source_url("entry", "6803"),
                             "/?sel=feed:63&open=6803#row-6803")

    def test_entry_with_ai_before_fragment(self):
        # ★ ai=1 必须插在 #row-* 之前，否则成了锚点的一部分，前端读不到
        with patch.object(sourceblock, "_entry_feed_id", return_value=63):
            self.assertEqual(sourceblock.source_url("entry", "6803", ai=True),
                             "/?sel=feed:63&open=6803&ai=1#row-6803")

    def test_report_with_ai(self):
        self.assertEqual(sourceblock.source_url("report", "daily-2026-10-31", ai=True),
                         "/summary/2026-10-31?ai=1")

    def test_entry_without_feed_id(self):
        with patch.object(sourceblock, "_entry_feed_id", return_value=None):
            self.assertIsNone(sourceblock.source_url("entry", "6803"))

    def test_chat_resolves_to_conversation(self):
        conv, _ = Conversation.objects.get_or_create(source_type="entry", source_id="6803")
        msg = conv.messages.create(role="user", body="问一句")
        with patch.object(sourceblock, "_entry_feed_id", return_value=63):
            self.assertEqual(sourceblock.source_url("chat", str(msg.id)),
                             "/?sel=feed:63&open=6803#row-6803")

    def test_chat_missing_message(self):
        self.assertIsNone(sourceblock.source_url("chat", "999999"))

    def test_web_is_none(self):
        self.assertIsNone(sourceblock.source_url("web", "https://x"))

    def test_empty_is_none(self):
        self.assertIsNone(sourceblock.source_url("", ""))
        self.assertIsNone(sourceblock.source_url("entry", ""))
