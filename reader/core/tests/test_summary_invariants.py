"""日报的质量闸：数字审计 / 引用覆盖 / 素材分类 / 源名瘦身。

这些不变量都来自真实事故（见各函数注释里的日期），是「日报可信」的地基：
  · 数字必须与素材一致（208→209 那次编数）；
  · 每个要点都要点得回原文，但异常/观察段和（推断）不算失引（假警报教训）；
  · 整段失引要按分类暴露，不能被总数淹没；
  · 单位换算（billion→亿）只挪小数点，不算编造。
"""
from datetime import date, timedelta

from django.conf import settings
from django.test import SimpleTestCase

from core import summary


class ClassifyKindTest(SimpleTestCase):
    def test_title_only_when_no_body(self):
        self.assertEqual(summary.classify_kind("标题", ""), "title_only")
        self.assertEqual(summary.classify_kind("标题", None), "title_only")

    def test_quote_flag_wins(self):
        self.assertEqual(summary.classify_kind("t", "正文", is_quote=True), "quote")

    def test_english_retweet_recognized(self):
        # 中文正则认不出 RT @user: —— 曾经 Twitter 源 70/70 全判 normal 的根因
        self.assertEqual(summary.classify_kind("RT @alex_chen: great news", "x"),
                         "repost")
        self.assertEqual(summary.classify_kind("t", "RT @alex_chen: great news"),
                         "repost")

    def test_chinese_reply(self):
        self.assertEqual(summary.classify_kind("回复@张三：谢谢", "正文"), "reply")
        self.assertEqual(summary.classify_kind("@张三: 内容", "正文"), "reply")

    def test_chinese_repost(self):
        self.assertEqual(summary.classify_kind("转：分享一篇文章", "短内容"), "repost")

    def test_normal(self):
        self.assertEqual(summary.classify_kind("标题", "成段正文"), "normal")


class AnnotateQuotesTest(SimpleTestCase):
    """引用块只标边界，不替模型判断归属。"""

    def test_boundary_marked_in_order(self):
        raw = "我的回答。<blockquote>提问者: 一个假设</blockquote>"
        out = summary.annotate_quotes(raw)
        self.assertLess(out.index("我的回答"), out.index("【引文】"))
        self.assertIn("【引文】提问者: 一个假设【/引文】", out)

    def test_marker_is_structural_not_attribution(self):
        # 引用块也可能只是 README 的 Note —— 规则层不能断言"他人观点"
        out = summary.annotate_quotes("<blockquote>Note: 安装说明</blockquote>")
        self.assertIn("【引文】", out)
        self.assertNotIn("他人", out)

    def test_no_quote_is_plain(self):
        out = summary.annotate_quotes("<p>第一段</p><p>第二段</p>")
        self.assertEqual([ln.strip() for ln in out.splitlines()],
                         ["第一段", "第二段"])


class AttributionPromptTest(SimpleTestCase):
    """"提问不是判断"必须写进每一层 prompt，不能只写在注释里。"""

    def test_every_summary_layer_mentions_quote_attribution(self):
        for prompt in (summary.S3_SYSTEM, summary.S3_USER, summary.S2_USER,
                       summary.S1_SYSTEM, summary.S0_USER):
            self.assertIn("引文", prompt)

    def test_hedge_must_be_preserved(self):
        # 2026-11-02：素材「都有可能」被 S0 写成「才是」——对冲词必须在规则里
        self.assertIn("对冲", summary.S2_USER)
        self.assertIn("对冲", summary.S1_SYSTEM)
        self.assertIn("都有可能", summary.S0_USER)


class CondenseTest(SimpleTestCase):
    def test_short_untouched(self):
        self.assertEqual(summary.condense("短文", 100), ("短文", False))

    def test_long_keeps_head_and_tail_with_marker(self):
        text, truncated = summary.condense("字" * 300, 100)
        self.assertTrue(truncated)
        self.assertIn("此处省略 200 字", text)     # 显式标注，别让模型当完整正文
        self.assertTrue(text.startswith("字" * 70))
        self.assertTrue(text.endswith("字" * 30))


class NumberAuditTest(SimpleTestCase):
    def test_fabricated_number_caught(self):
        # 2026-10-17 实测事故：208 家 → 209 家、878 场 → 880 场
        unknown = summary.number_audit("今天 209 家影院放映 880 场",
                                       "今天 208 家影院放映 878 场")
        self.assertIn("209", unknown)
        self.assertIn("880", unknown)

    def test_unit_conversion_not_flagged(self):
        # 13.43 billion → 134.3 亿：只挪小数点，有效数字未变
        self.assertEqual(
            summary.number_audit("营收 134.3 亿美元",
                                 "revenue of 13.43 BILLION dollars"), [])

    def test_link_targets_ignored(self):
        # 链接目标里的用户 ID / 推文 ID 不是正文数字
        self.assertEqual(
            summary.number_audit("见 [详情](https://xueqiu.com/123456/412537742)",
                                 "正文没有任何数字"), [])

    def test_wikilink_target_numbers_ignored(self):
        # 2026-11-22：改名页 slug 带时间戳，照抄正确 slug 不能被判「编造数字」。
        # 这条与 wiki_loop._check_links「slug 逐字照抄清单」是同一份契约的两半。
        self.assertEqual(
            summary.number_audit(
                "相关页 [[zhong-mei-mo-xing-cha-ju-1794620890]]，另有 [[a-123456|别名]]",
                "候选明细：没有这些数字"), [])
        # 别名是给人看的正文，里面的数字照核；裸写在正文里的数字也照核。
        self.assertEqual(
            summary.number_audit("见 [[a|209 家影院]]", "素材：208 家影院"), ["209"])
        self.assertEqual(
            summary.number_audit("时间戳 1794620890 很重要", "素材无数字"),
            ["1794620890"])

    def test_small_aggregates_ignored(self):
        # 模型自己加总的两位数（11+2+6=19）不逐个核
        self.assertEqual(
            summary.number_audit("互动回复与转贴合计 19 条",
                                 "互动回复 11 条，转贴 2 条，回复 6 条"), [])

    def test_material_number_passes(self):
        self.assertEqual(summary.number_audit("208 家", "今天 208 家"), [])


class PointCoverageTest(SimpleTestCase):
    # ★ 顶级 `## ` 小节只负责开/关计数作用域，要点本身挂在 `### ` 子段下
    #   （或无标题段）——与真实日报的结构一致。
    BODY = (
        "## 一、内容概览\n\n"
        "### 市场与投资\n\n"
        "- 大道加仓苹果 [大道 · 08:53](https://e1.com/a)\n"
        "- 票房站票房走高 [票房站 · 00:12](https://e2.com/b)\n\n"
        "票房站今日票房继续走高（推断）\n\n"
        "## 三、异常告警\n\n"
        "### 详情\n\n"
        "- 某源 500 错误，无引用\n\n"
        "## 四、谛听札记\n\n"
        "- 判断意见，无引用\n"
    )

    def test_only_content_points_counted(self):
        # 异常段与札记段不由素材支撑、（推断）是模型自己的判断：都不进分母
        hit, total, links = summary._point_coverage(self.BODY)
        self.assertEqual((hit, total), (2, 2))

    def test_legacy_observation_heading_still_skipped(self):
        # 2026-10-27 改名「谛听札记」前的旧标题「我的观察与建议」仍要豁免：
        # 历史报告还在磁盘上，改个关键词就会让它们全部假红。
        hit, total, _ = summary._point_coverage(
            self.BODY.replace("## 四、谛听札记", "## 四、我的观察与建议"))
        self.assertEqual((hit, total), (2, 2))

    def test_list_block_counts_each_line(self):
        # 整块算一个点会掩盖"某一条漏引用"——逐行才是对口径
        hit, total, _ = summary._point_coverage(
            "- 有引用 [x](https://e.com/1)\n- 没有引用")
        self.assertEqual((hit, total), (1, 2))

    def test_heading_glued_to_points_still_counted(self):
        # 模型有时不留空行（### 投资\n- 要点）：以前会整段丢掉、覆盖率静默归零
        hit, total, _ = summary._point_coverage(
            "### 投资\n- 要点 [x](https://e.com/2)")
        self.assertEqual((hit, total), (1, 1))

    def test_infer_marker_excluded(self):
        hit, total, _ = summary._point_coverage("票房站继续走高（推断）")
        self.assertEqual((hit, total), (0, 0))

    def test_count_cites_only_http(self):
        self.assertEqual(summary.count_cites(self.BODY), 2)


class CiteCoverageTest(SimpleTestCase):
    def test_url_dedup_across_sources(self):
        # 两个源收录同一条：分母按 URL 去重，口径要与正文匹配一致
        items = [{"url": "https://e.com/same"}, {"url": "https://e.com/same"},
                 {"url": "https://e.com/other"}]
        hit, total = summary._cite_coverage("提及 [一](https://e.com/same)", items)
        self.assertEqual((hit, total), (1, 2))

    def test_no_urls_zero_denominator(self):
        self.assertEqual(summary._cite_coverage("正文", [{"url": ""}]), (0, 0))


class UncitedCategoriesTest(SimpleTestCase):
    def test_whole_category_exposed(self):
        # 2026-10-17：AI 段 15 条零引用，被总数 33/67 淹掉 —— 必须按分类点名
        by_cat = {1: {"title": "AI", "items": [{"url": "https://a.com/1"}]},
                  2: {"title": "投资", "items": [{"url": "https://b.com/2"}]}}
        body = "只有投资内容 [x](https://b.com/2)"
        self.assertEqual(summary._uncited_categories(body, by_cat), [("AI", 1)])

    def test_all_cited_empty(self):
        by_cat = {1: {"title": "AI", "items": [{"url": "https://a.com/1"}]}}
        self.assertEqual(summary._uncited_categories("[x](https://a.com/1)", by_cat),
                         [])


class FeedNameTest(SimpleTestCase):
    def test_slim_rules(self):
        self.assertEqual(summary.feed_name("甲源 的雪球全部动态"), "甲源")
        self.assertEqual(summary.feed_name("Twitter @Alex Chen"), "Alex Chen")
        self.assertEqual(summary.feed_name("NVIDIA Blog | NVIDIA"), "NVIDIA Blog")
        self.assertEqual(summary.feed_name("票房站 - 正在热映 - 完整版"), "票房站")

    def test_word_boundary_cut_for_ascii(self):
        # 18 是放下 "Anthropic Research" 的最短长度；超长按词边界切
        self.assertEqual(summary.feed_name("Anthropic Research"), "Anthropic Research")
        cut = summary.feed_name("Anthropic Research Announces Claude")
        self.assertLessEqual(len(cut), 18)
        self.assertFalse(cut.endswith(" "))

    def test_name_map_disambiguates_by_tail(self):
        # 重名不是加 #id（读者看不懂），而是取公共前缀 + 各自尾缀
        meta = {101: {"title": "票房站 - 即将上映"},
                102: {"title": "票房站 - 正在热映"}}
        m = summary._name_map(meta)
        self.assertNotEqual(m[101], m[102])
        self.assertTrue(m[101].startswith("票房站"))

    def test_name_map_identical_titles_fall_back_to_id(self):
        meta = {201: {"title": "Same"}, 202: {"title": "Same"}}
        m = summary._name_map(meta)
        self.assertEqual(m[201], "Same#201")
        self.assertEqual(m[202], "Same#202")


class WindowTest(SimpleTestCase):
    def test_window_is_24h_ending_at_boundary_hour(self):
        start, end = summary.window_for(date(2026, 10, 20))
        self.assertEqual(end - start, timedelta(hours=24))
        self.assertEqual((end.hour, end.minute),
                         (settings.SUMMARY_WINDOW_HOUR, 0))


class BuildItemsLevelTest(SimpleTestCase):
    """未评级按 feed 默认级别；素材必须标「未定级」而不是伪装 L2。"""

    def _entry(self, eid=1, content="正文"):
        from datetime import datetime
        from django.utils import timezone
        from types import SimpleNamespace
        return SimpleNamespace(
            id=eid, feed_id=7, title="标题", url=f"https://e.com/{eid}",
            content=content, feed_title="源",
            published_at=timezone.make_aware(datetime(2026, 10, 28, 8, 0)),
            created_at=timezone.make_aware(datetime(2026, 10, 28, 8, 0)))

    def test_unrated_uses_feed_default_not_l2(self):
        meta = {7: {"title": "低价值源", "category_id": 1, "category_title": "杂项",
                    "default_level": 1}}
        items = summary.build_items([self._entry()], {}, meta)
        self.assertEqual(items[0]["level"], 1)
        self.assertFalse(items[0]["rated"])
        self.assertEqual(summary.level_label(items[0]), "未定级·L1")

    def test_unrated_default_zero_is_excluded(self):
        meta = {7: {"title": "垃圾源", "category_id": 1, "category_title": "杂项",
                    "default_level": 0}}
        self.assertEqual(summary.build_items([self._entry()], {}, meta), [])

    def test_rated_level_is_kept(self):
        from core.models import EntryRating
        meta = {7: {"title": "源", "category_id": 1, "category_title": "杂项",
                    "default_level": 1}}
        rating = EntryRating(entry_id=1, feed_id=7, level=3, reason="llm")
        items = summary.build_items([self._entry()], {1: rating}, meta)
        self.assertEqual(items[0]["level"], 3)
        self.assertTrue(items[0]["rated"])
        self.assertEqual(summary.level_label(items[0]), "L3")

    def test_blockquote_keeps_boundary_but_not_quote_kind(self):
        # 长文里的一段引用不是"整条引用转发"：要留边界，但不能因此丢掉 S3 压缩
        meta = {7: {"title": "源", "category_id": 1, "category_title": "杂项",
                    "default_level": 1}}
        content = "我的回答。<blockquote>提问者: 一个假设</blockquote>"
        item = summary.build_items([self._entry(content=content)], {}, meta)[0]
        self.assertIn("【引文】提问者: 一个假设【/引文】", item["text"])
        self.assertFalse(item["is_quote"])
        self.assertEqual(item["kind"], "normal")


class CapFeedL1Test(SimpleTestCase):
    """单源过多只丢 L1；L2/L3 一条不丢；保留最近 K 条。"""

    def _items(self, n_l1=50, n_l2=5, n_l3=0):
        from datetime import datetime, timedelta
        from django.utils import timezone
        base = timezone.make_aware(datetime(2026, 10, 28, 0, 0))
        out = []
        for i in range(n_l1):
            out.append({"id": i + 1, "feed_id": 1, "level": 1, "rated": True,
                        "published_at": base + timedelta(minutes=i)})
        for i in range(n_l2):
            out.append({"id": 1000 + i, "feed_id": 1, "level": 2, "rated": True,
                        "published_at": base + timedelta(minutes=100 + i)})
        for i in range(n_l3):
            out.append({"id": 2000 + i, "feed_id": 1, "level": 3, "rated": True,
                        "published_at": base + timedelta(minutes=200 + i)})
        return out

    def test_caps_l1_keeps_all_l2_l3(self):
        items = self._items(n_l1=50, n_l2=5, n_l3=2)
        kept, dropped = summary.cap_feed_l1(items, max_items=40, keep_l1=20)
        self.assertEqual(dropped, 30)
        l1 = [it for it in kept if it["level"] == 1]
        l2 = [it for it in kept if it["level"] == 2]
        l3 = [it for it in kept if it["level"] == 3]
        self.assertEqual(len(l1), 20)
        self.assertEqual(len(l2), 5)
        self.assertEqual(len(l3), 2)
        # 保留的是最近的 20 条 L1（id 31..50）
        self.assertEqual(sorted(it["id"] for it in l1), list(range(31, 51)))

    def test_no_drop_when_under_cap(self):
        items = self._items(n_l1=30, n_l2=5)
        kept, dropped = summary.cap_feed_l1(items, max_items=40, keep_l1=20)
        self.assertEqual(dropped, 0)
        self.assertEqual(len(kept), len(items))

    def test_l2_not_dropped_even_over_cap(self):
        items = self._items(n_l1=5, n_l2=50)
        kept, dropped = summary.cap_feed_l1(items, max_items=40, keep_l1=20)
        self.assertEqual(dropped, 0)
        self.assertEqual(len([it for it in kept if it["level"] == 2]), 50)
