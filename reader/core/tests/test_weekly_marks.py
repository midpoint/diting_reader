"""周报「我的痕迹」三源整合（report + entry + chat）的口径测试。

★ 这一组测的不是"能不能跑"，而是**两个时间不能混**：
  · `content_day`（内容属于哪份日报）决定"在不在本期"；
  · `marked_at`（我什么时候划的）决定"是不是本周回看"。
  用动作时间定窗口会同时制造两个 bug（真实数据都发生过）：
  ① 10-31 一次性补划 10-26~10-30 的旧日报被算成"一天的活动"；
  ② 11-01 09:36 划的文章（内容属于本周）要等到下一期。
  所以下面每条断言都尽量做成**两个口径会给出不同答案**的场景 ——
  否则绿了也说明不了问题。
"""
import datetime as dt
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from core import weekly
from core.models import Annotation, ChatMessage, Conversation, Highlight

#: 与默认 summary_window_hour=6 对齐：[10-25 06:00, 11-01 06:00)
WEEK_END = dt.date(2026, 11, 1)


def _aware(y, m, d, h=12, mi=0):
    return timezone.make_aware(dt.datetime(y, m, d, h, mi))


def _set_created(obj, ts):
    """把 auto_now_add 的 created_at 改成指定时间（模拟"什么时候划的"）。"""
    type(obj).objects.filter(pk=obj.pk).update(created_at=ts)
    obj.refresh_from_db()
    return obj


class WeekMarksWindowTest(TestCase):
    """窗口归属：内容日 vs 动作时间。"""

    def setUp(self):
        weekly._entry_meta.cache_clear()

    def tearDown(self):
        weekly._entry_meta.cache_clear()

    def test_report_marks_keyed_by_daily_date(self):
        with patch.object(weekly.mk, "list_marks") as lm:
            lm.side_effect = lambda st, sid: (
                {"highlights": [{"id": 7, "quoted_text": "日报上的划线",
                                 "note": "", "created_at": _aware(2026, 10, 28)}],
                 "annotations": [], "err": ""}
                if sid == "daily-2026-10-28" else
                {"highlights": [], "annotations": [], "err": ""})
            got = weekly.week_marks(WEEK_END)
        self.assertEqual(got["n"], 1)
        self.assertIn("— 10-28 —", got["text"])
        self.assertIn("日报上的划线", got["text"])

    def test_entry_content_day_from_published_at(self):
        """文章 10-28 10:00 发布 ⇒ 归属 daily-10-29（10-28 那份 06:00 就截稿了）。"""
        h = Highlight.objects.create(source_type="entry", source_id="6810",
                                     quoted_text="文章里的句子", note="")
        _set_created(h, _aware(2026, 10, 28, 10))
        with patch.object(weekly, "_entry_meta",
                          return_value={"published_at": _aware(2026, 10, 28, 10),
                                        "feed_id": 3, "title": "某篇文章"}):
            got = weekly.week_marks(WEEK_END)
        self.assertEqual(got["n"], 1)
        self.assertIn("— 10-29 —", got["text"], "内容归属日应是 10-29，不是划线日")
        self.assertIn("文章里的句子", got["text"])

    def test_chat_uses_message_time_not_mark_time(self):
        """★ 关键反证：消息在窗口内、划线在窗口外 ⇒ 仍算本期。

        chat 划线划的是**消息里的话**，话是消息产生时写的。取划线时间就会跨窗口。
        """
        conv = Conversation.objects.create(source_type="entry", source_id="6810")
        msg = ChatMessage.objects.create(conversation=conv, role="assistant",
                                         body="AI 的回答")
        # ★ created_at 是 auto_now_add，构造参数会被忽略 —— 必须建完再改，
        #   否则消息时间永远是"现在"，这条测试就变成测不到东西的恒绿。
        _set_created(msg, _aware(2026, 10, 30, 12))
        h = Highlight.objects.create(source_type="chat", source_id=str(msg.id),
                                     quoted_text="AI 说的一句话", note="")
        _set_created(h, _aware(2026, 11, 1, 18))     # 划线动作在窗口外
        with patch.object(weekly, "_entry_meta",
                          return_value={"published_at": _aware(2026, 10, 30),
                                        "feed_id": 3, "title": "某篇文章"}):
            got = weekly.week_marks(WEEK_END)
        self.assertEqual(got["n"], 1, "取消息时间就该在内；取划线时间会漏掉")
        self.assertIn("— 10-31 —", got["text"])
        self.assertIn("[对话", got["text"])

    def test_review_bucket_for_old_content_marked_this_week(self):
        """内容属于 10-21（不在本期），但本周回头划了 ⇒ 进「回看」段，不进主线。"""
        h = Highlight.objects.create(source_type="entry", source_id="6000",
                                     quoted_text="旧文章里的一句话", note="")
        _set_created(h, _aware(2026, 10, 30, 15))
        with patch.object(weekly, "_entry_meta",
                          return_value={"published_at": _aware(2026, 10, 20, 9),
                                        "feed_id": 3, "title": "上上周的文章"}):
            got = weekly.week_marks(WEEK_END)
        self.assertEqual(got["n"], 1)
        self.assertEqual(got["n_review"], 1)
        self.assertIn("本周回看", got["text"])
        # ★ 反证：不能既进回看段又混进按天排的主线
        self.assertNotIn("— 10-21 —\n", got["text"])

    def test_unrelated_marks_dropped(self):
        """内容不在本期、也不是本周划的 ⇒ 与本期无关，不该进素材。"""
        h = Highlight.objects.create(source_type="entry", source_id="6001",
                                     quoted_text="很久以前划的", note="")
        _set_created(h, _aware(2026, 9, 1, 9))
        with patch.object(weekly, "_entry_meta",
                          return_value={"published_at": _aware(2026, 8, 20, 9),
                                        "feed_id": 3, "title": "老文章"}):
            got = weekly.week_marks(WEEK_END)
        self.assertEqual(got["n"], 0)
        self.assertNotIn("很久以前划的", got["text"])

    def test_counts_match_material(self):
        """★ 概览的 N 必须等于素材里真出现的条数。

        曾经拿 items 全量数，被丢掉的那些也算进去 ⇒「概览写 45、素材只有 42」，
        读者一对就发现数对不上。
        """
        h1 = Highlight.objects.create(source_type="entry", source_id="6811",
                                      quoted_text="在期内的一条", note="")
        _set_created(h1, _aware(2026, 10, 28, 10))
        h2 = Highlight.objects.create(source_type="entry", source_id="6002",
                                      quoted_text="早就不相干的", note="")
        _set_created(h2, _aware(2026, 9, 1, 9))
        metas = {6811: {"published_at": _aware(2026, 10, 28, 10),
                        "feed_id": 3, "title": "在期文章"},
                 6002: {"published_at": _aware(2026, 8, 20, 9),
                        "feed_id": 3, "title": "老文章"}}
        with patch.object(weekly, "_entry_meta",
                          side_effect=lambda eid: metas.get(eid)):
            got = weekly.week_marks(WEEK_END)
        self.assertEqual(got["n"], 1)
        self.assertEqual(got["n_hl"], 1)
        self.assertEqual(got["text"].count("划线："), got["n"])

    def test_annotation_counts_as_annotation_not_highlight(self):
        a = Annotation.objects.create(source_type="entry", source_id="6812",
                                      body="我自己写的批注")
        _set_created(a, _aware(2026, 10, 29, 10))
        with patch.object(weekly, "_entry_meta",
                          return_value={"published_at": _aware(2026, 10, 29, 9),
                                        "feed_id": 3, "title": "文章"}):
            got = weekly.week_marks(WEEK_END)
        self.assertEqual((got["n_hl"], got["n_an"]), (0, 1))
        self.assertIn("批注：我自己写的批注", got["text"])

    def test_error_is_visible_not_silent(self):
        """读失败必须能看出来 —— 否则「本周没划线」和「读失败」同一副样子。"""
        with patch.object(weekly.mk, "list_marks", side_effect=RuntimeError("db down")):
            got = weekly.week_marks(WEEK_END)
        self.assertIn("RuntimeError", got["err"])
        self.assertEqual(got["n"], 0)
        self.assertIn("失败", got["text"])
        self.assertNotIn("本周没有划线", got["text"], "读失败不能说成'没有划线'")


class WeekMarksLinkTest(TestCase):
    """深链：mk / msg / ai 必须在 `#` 之前，否则前端读不到。"""

    def setUp(self):
        weekly._entry_meta.cache_clear()

    def tearDown(self):
        weekly._entry_meta.cache_clear()

    def test_report_link_has_mk_and_src(self):
        with patch.object(weekly.mk, "list_marks") as lm:
            lm.side_effect = lambda st, sid: (
                {"highlights": [{"id": 41, "quoted_text": "x", "note": "",
                                 "created_at": _aware(2026, 10, 28)}],
                 "annotations": [], "err": ""}
                if sid == "daily-2026-10-28" else
                {"highlights": [], "annotations": [], "err": ""})
            got = weekly.week_marks(WEEK_END)
        self.assertIn("/summary/2026-10-28?mk=41&src=weekly", got["text"])

    def test_chat_link_locates_message(self):
        conv = Conversation.objects.create(source_type="entry", source_id="6810")
        msg = ChatMessage.objects.create(conversation=conv, role="assistant",
                                         body="回答")
        _set_created(msg, _aware(2026, 10, 30, 12))
        h = Highlight.objects.create(source_type="chat", source_id=str(msg.id),
                                     quoted_text="一句 AI 的话", note="")
        _set_created(h, _aware(2026, 10, 30, 13))
        with patch.object(weekly, "_entry_meta",
                          return_value={"published_at": _aware(2026, 10, 30),
                                        "feed_id": 3, "title": "文章"}):
            got = weekly.week_marks(WEEK_END)
        url = [ln for ln in got["text"].split("\n") if "对话" in ln][0]
        self.assertIn("ai=1", url)
        self.assertIn("msg=%d" % msg.id, url)
        self.assertIn("mk=%d" % h.id, url)
        # ★ msg/mk 都在 fragment 之前
        self.assertLess(url.index("msg="), url.index("#row-"))


class StarBriefTest(TestCase):
    """第五节摘要行：star 与痕迹并成一行，且三种组合都要有合理输出。"""

    STAR = {"feed_name": "但斌", "published_at": None, "feed_id": 3,
            "entry_id": 5477, "title": "t", "category": "投资"}

    def test_both(self):
        s = weekly.star_brief([self.STAR], {"n": 42, "n_hl": 42, "n_an": 0,
                                            "n_review": 3})
        self.assertIn("共加星 **1** 篇", s)
        self.assertIn("划线与批注 **42** 条", s)
        self.assertIn("本周回看 3", s)

    def test_marks_only(self):
        """一条星都没有、但划了 20 条 —— 原来这里返回空串，第五节开头就秃了。"""
        s = weekly.star_brief([], {"n": 20, "n_hl": 18, "n_an": 2, "n_review": 0})
        self.assertIn("划线与批注 **20** 条", s)
        self.assertIn("批注 2", s)

    def test_nothing(self):
        self.assertEqual(weekly.star_brief([], None), "")

    def test_new_headline_is_matched(self):
        """改标题后摘要行仍要插得进去（正则要认「划线/批注」）。"""
        body = "## 五、star、划线、批注里的线索\n\n正文第一句\n"
        out = weekly.insert_star_brief(body, "摘要行")
        self.assertIn("摘要行\n\n正文第一句", out)


class DigestTest(TestCase):
    """痕迹过多时走分片：同池、带 run_id、条数超阈值才触发。"""

    def _items(self, n):
        return [{"kind": "hl", "body": "句子%d" % i, "note": "", "src": "report",
                 "content_day": dt.date(2026, 10, 28), "link": "/x?mk=%d" % i,
                 "title": "", "ctx": ""} for i in range(n)]

    def test_threshold_value_is_sane(self):
        self.assertGreater(weekly.MARK_CHUNK_SIZE, 0)
        self.assertGreater(weekly.MARK_CHUNK_THRESHOLD, weekly.MARK_CHUNK_SIZE - 1)

    def test_threshold_not_too_eager(self):
        """★ 实测教训（2026-11-01）：阈值设 40 时本周 42 条就触发分片 ——
        多花 ¥0.19、跑了 4 分钟，还因为推理吃光分片预算把整期周报拖成
        「达到单次调用上限」。几十条是日常量级，必须直连 W0。"""
        self.assertGreaterEqual(weekly.MARK_CHUNK_THRESHOLD, 150,
                                "阈值太小会让日常几十条就走分片")

    def test_digest_splits_and_shares_pool(self):
        from core import citeguard
        calls = []

        def fake(kind, system, user, stats, **kw):
            calls.append(kw)
            stats["calls"] = stats.get("calls", 0) + 1
            return "[但斌 · 10:00](http://x) 摘要", ""

        stats = {"calls": 0, "errors": [], "tokens": 0}
        n = weekly.MARK_CHUNK_SIZE * 3
        with patch.object(citeguard, "_call", side_effect=fake):
            out = weekly.digest_marks(self._items(n), stats,
                                      run_id=99, week_end=WEEK_END)
        self.assertEqual(len(calls), 3, "%d 条 / 每片 %d ⇒ 3 片"
                         % (n, weekly.MARK_CHUNK_SIZE))
        self.assertTrue(all(c["pool"] == weekly.cr.POOL_WEEKLY for c in calls),
                        "★ 必须同一个池：独立池会在周日静默 refuse")
        self.assertTrue(all(c["run_id"] == 99 for c in calls),
                        "★ 漏 run_id 会让 LlmUsage 关联为空、页面成本恒 0")
        # ★ 推理模型会吃光预算 ⇒ 分片也必须给够 max_tokens（1600 实测不够）
        self.assertTrue(all(c["max_tokens"] >= 4096 for c in calls),
                        "分片 max_tokens 太小会「推理吃光预算、正文空串」")
        self.assertIn("摘要", out)

    def test_digest_falls_back_to_raw_on_failure(self):
        from core import citeguard
        stats = {"calls": 0, "errors": [], "tokens": 0}
        with patch.object(citeguard, "_call", return_value=("", "额度用完")):
            out = weekly.digest_marks(self._items(weekly.MARK_CHUNK_SIZE),
                                      stats, run_id=1, week_end=WEEK_END)
        self.assertIn("提炼失败", out)
        self.assertIn("句子0", out, "★ 失败要降级成原文，不能整片丢失")
