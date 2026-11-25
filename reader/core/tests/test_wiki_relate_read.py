"""阅读时露出知识库（2026-11-16 晚）。

用户把标签过完、标签这条线收口之后问「接下来呢」。只读盘点摆出的事实：
32 页 / 6,858 字 / 中位 220 字，18 页只有 1 个来源，而知识库**只有主动点进去
才看得见** —— 日常读文章时碰不到它。于是「有资格不读」这个命题的落点（读之前
先知道自己已有判断）一直是空的。

这一组钉的判据（每条都对应一个真实踩过的误判）：
  A. 页标题出现在**文章标题**里 ≠ 只出现在正文某处（理由文案要分开说实话）
  B. 标签只在**开头**找 —— 长文尾巴提一句「游戏」「安全」不算讲这个话题
  C. 拉丁字母按整词取 token —— `in`/`em`/`mi` 不能让英文文章白嫖 `Gemini相关`
  D. 极短正文（`💯`/`Yup`）不判 —— 字越少越容易把偶然重合当主题重合
  E. 低于门槛 / 说不出理由的一律不返回（宁缺勿滥）
  F. 索引缓存按 root 分开、且随文件变动失效（测试用临时库，串味会给出假绿）
  G. 只读：`wiki/` 之外的 `.drafts/` 不参与
  H. 详情视图接线：计算失败不拖垮正文；模板空则不渲染；链接开新标签页

★★ 写这批用例时踩到的坑（第二条最重要）：
  `MIN_TEXT = 50` 是**前置**门槛 —— 正文不够长就直接返回 `[]`。于是所有
  「断言结果为空」的用例都会**假绿**：它空是因为被长度挡了，不是因为判据生效。
  所以下面的正文一律补到 60 字以上，且补的是不含主题词的填充句；`_filler()`
  专门干这件事，改判据时别顺手把它删掉。
"""
import shutil
import tempfile

from django.test import RequestFactory, TestCase, override_settings
from django.template.loader import render_to_string

from core import wiki as wstore
from core import wiki_relate

PAGE = """---
title: {title}
domain: ai
sources: [entry:1]
updated: 2026-11-01
confidence: medium
tags: [{tags}]
candidates: []
---

{body}
"""

#: 只补长度、不含任何主题词的填充句。断言「结果为空」的用例必须靠它越过
#: `MIN_TEXT`，否则测得的是长度门槛而不是被测判据。
FILLER = "这里只是把上下文补足，让正文长度越过短文本门槛，不引入任何主题词。"


def _long(text: str, times: int = 2) -> str:
    """把正文补到足够长（默认 2 遍填充 ≈ 80 字）。"""
    return text + FILLER * times


class _RelateFixture(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-relate-")
        cls.ov = override_settings(WIKI_DIR=cls.tmp, ALLOWED_HOSTS=["testserver"])
        cls.ov.enable()

    @classmethod
    def tearDownClass(cls):
        cls.ov.disable()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        wstore.ensure_dirs()
        wiki_relate.reset_cache()

    def put(self, slug, title, tags="", body="这个页面的一句话结论。"):
        p = wstore.root() / "wiki" / "ai" / ("%s.md" % slug)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(PAGE.format(title=title, tags=tags, body=body),
                     encoding="utf-8")
        wiki_relate.reset_cache()      # 让下一次 relate 重建索引
        return p


class TitleHitTest(_RelateFixture):
    """A：标题命中 vs 正文提及，两档两说法。"""

    def test_title_in_article_title_is_strongest(self):
        self.put("ai-pao-mo", "AI泡沫")
        rows = wiki_relate.relate(
            _long("AI泡沫要来了吗？先看几个估值和资本开支的数据。"), "AI泡沫要来了吗")
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["score"], 10.0)
        self.assertIn("文章标题就是", rows[0]["why"][0])

    def test_title_only_in_body_says_so(self):
        """★ 真实库事故：一篇讲机房运维的文章，只因正文有一句「实战经验分享」，
        第一版就写成「标题里就有实战经验」—— 人点进去会发现被骗。"""
        self.put("shi-zhan", "实战经验")
        body = _long("年轻人去乌兰察布守机房，真能月入三万吗。这块我没什么"
                     "实战经验，只能把听到的说法摆出来，供各位自己判断。")
        rows = wiki_relate.relate(body, "年轻人去乌兰察布守机房")
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("文章提到", rows[0]["why"][0])
        self.assertNotIn("标题就是", rows[0]["why"][0])
        # 正文提及比标题命中弱一档
        self.assertEqual(rows[0]["score"], 6.0)

    def test_title_match_ignores_spacing_and_punctuation(self):
        """「AI 泡沫」与「AI泡沫」必须互相命中：整串归一化后做子串判断。"""
        self.put("ai-pao-mo", "AI泡沫")
        rows = wiki_relate.relate(_long("今天聊聊 AI 泡沫这件事，先看几个数据。"), "AI 泡沫")
        self.assertEqual(len(rows), 1, rows)


class TagWindowTest(_RelateFixture):
    """B：标签只看开头。"""

    def test_tag_in_head_hits(self):
        """两个稀有标签同时命中才过门槛（单个标签只有约 2.5 分，这是刻意的）。"""
        self.put("yun", "云厂商定价权", tags="云厂商, 数据中心")
        rows = wiki_relate.relate(
            _long("云厂商开始涨价了，数据中心的紧缺还在继续。"), "云厂商涨价")
        self.assertEqual(len(rows), 1, rows)
        self.assertTrue(any("云厂商" in w for w in rows[0]["why"]), rows[0]["why"])

    def test_tag_only_in_tail_does_not_hit(self):
        """★ 真实库实测：讲 NVDA 持仓的长文，尾巴提了「游戏」「安全」，于是同时
        命中「腾讯游戏」和「网络安全风险」—— 两条都是噪声。"""
        self.put("you-xi", "腾讯游戏", tags="游戏, 长期投资")
        lead = "关于 NVDA 的持仓，我的理由都还在。" + "这段是正文。" * 120
        tail = lead + "另外顺带说一句，游戏行业最近也有些变化，安全方面也一样。"
        self.assertEqual(wiki_relate.relate(lead, "NVDA 持仓"), [])
        self.assertEqual(wiki_relate.relate(tail, "NVDA 持仓"), [],
                         "正文尾部的标签词不该算「讲同一个话题」")

    def test_window_is_head_plus_800(self):
        """窗口判据写成常量：改小会漏掉真正在开头讲的话题。"""
        self.assertGreaterEqual(wiki_relate.TAG_WINDOW, 400)


class LatinTokenTest(_RelateFixture):
    """C：拉丁字母按整词。"""

    def test_english_article_does_not_hit_by_letter_bigrams(self):
        """★ 真实库实测：`LVMH's Media Division announces strategic partnership`
        命中 `Gemini相关`，因为 gemini 的 gram 里 in/em/mi 在任何英文里都有。

        ★★ 正文必须够长（越过 MIN_TEXT），否则这条是**假绿**：空是因为长度门槛，
           不是因为整词判据。
        """
        self.put("gemini", "Gemini相关")
        body = ("LVMH's Media Division announces strategic partnership with a "
                "global media platform, bringing media planning and buying into "
                "one division, and the group said the deal will close next "
                "quarter subject to regulatory approval in several markets.")
        self.assertGreater(len(wiki_relate._norm(body)), wiki_relate.MIN_TEXT)
        self.assertEqual(wiki_relate.relate(body, "LVMH media partnership"), [])

    def test_latin_word_still_hits_as_whole_word(self):
        """整词判据不能把该命中的人干掉：`Gemini` 作为整词出现就该算。"""
        self.put("gemini", "Gemini相关", tags="Gemini, 云厂商")
        rows = wiki_relate.relate(
            _long("Google 这次把 Gemini 的能力往下放了一档，价格也跟着降。"
                  "对开发者来说这是好事，对云厂商来说是利润压力。"),
            "Gemini 降价")
        self.assertEqual(len(rows), 1, rows)

    def test_tokens_split_latin_and_cjk(self):
        toks = wiki_relate._tokens(wiki_relate._norm("Gemini相关 v2"))
        self.assertIn("gemini", toks)
        self.assertIn("相关", toks)
        self.assertIn("v2", toks)
        self.assertNotIn("in", toks, "拉丁不许拆 2 字组合")


class ShortTextTest(_RelateFixture):
    """D：极短正文不判。"""

    def test_very_short_text_returns_nothing(self):
        self.put("spacex", "SpaceX相关", tags="SpaceX, 火星")
        for text in ("💯", "Yup", "Important", "SpaceX"):
            self.assertEqual(wiki_relate.relate(text, text), [],
                             "正文 %r 太短，不该给人任何推荐" % text)

    def test_threshold_is_a_constant_at_least_20(self):
        self.assertGreaterEqual(wiki_relate.MIN_TEXT, 20)


class ScoringTest(_RelateFixture):
    """E：门槛与理由。"""

    def test_single_common_tag_alone_is_not_enough(self):
        """一个标签的权重约 2.5（df 越大越小），单独命中到不了 3 分门槛。

        ★ 正文必须够长：短了就是长度门槛在挡，测不到权重衰减。
        """
        self.put("a", "页甲", tags="通用词")
        self.put("b", "页乙", tags="通用词")
        self.put("c", "页丙", tags="通用词")
        body = _long("这里提到了通用词，但只提了一次，别的地方再没有出现过。")
        self.assertGreater(len(wiki_relate._norm(body)), wiki_relate.MIN_TEXT)
        self.assertEqual(wiki_relate.relate(body, "一篇短文"), [])

    def test_two_rare_tags_hit(self):
        self.put("a", "甲作者晒单", tags="甲作者, 英伟达")
        body = _long("甲作者又晒单了，这次主要加的是英伟达。他说自己看不懂别的，"
                     "只买能看懂的生意，剩下的时间用来读书。")
        rows = wiki_relate.relate(body, "甲作者最新持仓")
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["title"], "甲作者晒单")

    def test_rare_tag_outweighs_common_tag(self):
        """df 衰减：只在 1 页出现的词比 7 页都有的词更有指向性。"""
        for i in range(7):
            self.put("common%d" % i, "通用页%d" % i, tags="AI应用")
        self.put("rare", "稀有页", tags="算力")
        idx = {it["title"]: it for it in wiki_relate._get_index(str(wstore.root()))}
        w_common = idx["通用页0"]["tags"][0][2]
        w_rare = idx["稀有页"]["tags"][0][2]
        self.assertGreater(w_rare, w_common)

    def test_every_row_has_a_reason(self):
        self.put("a", "AI泡沫", tags="AI泡沫, 算力")
        self.put("b", "云厂商定价权", tags="云厂商")
        body = _long("AI泡沫这件事，我现在的判断是资本开支还没到拐点。算力依然"
                     "紧张，云厂商的定价权也还在。")
        rows = wiki_relate.relate(body, "AI泡沫与云厂商")
        self.assertTrue(rows, "先确认这条路真有产出，否则下面的断言是空集合假绿")
        for r in rows:
            self.assertTrue(r["why"], "说不出理由的推荐不许返回：%r" % r)

    def test_limit_caps_rows(self):
        """6 页共用 3 个词：df=6 ⇒ 每个约 1.33 分，三个加起来刚过门槛。

        ★ 只用 1 个共用词时 df=6、单分 1.33，谁也过不了门槛 —— 那是
          `test_single_common_tag_alone_is_not_enough` 钉的，别在这里重复。
        """
        for i in range(6):
            self.put("p%d" % i, "主题页%d" % i, tags="同一主题, 共同词, 第三词")
        body = _long("同一主题、共同词、第三词，这几个都在开头出现。")
        self.assertEqual(len(wiki_relate.relate(body, "同一主题", limit=2)), 2)

    def test_empty_library_returns_nothing(self):
        self.assertEqual(wiki_relate.relate(_long("随便一段足够长的正文。"), "标题"), [])


class IndexCacheTest(_RelateFixture):
    """F：缓存按 root 分开、随文件变动失效。"""

    BODY = _long("AI泡沫怎么看，这是一整段正文。")

    def test_cache_invalidated_when_page_changes(self):
        self.put("a", "AI泡沫")
        self.assertEqual(len(wiki_relate.relate(self.BODY, "AI泡沫")), 1)
        # 改标题 → 指纹变 → 不该再命中旧标题
        self.put("a", "云厂商定价权")
        self.assertEqual(len(wiki_relate.relate(self.BODY, "AI泡沫")), 0)

    def test_two_roots_do_not_share_index(self):
        """★ 测试都在临时库，缓存共用一个槽会让 A 库的索引答 B 库的问题。"""
        self.put("a", "AI泡沫")
        other = tempfile.mkdtemp(prefix="diting-relate-2-")
        try:
            with override_settings(WIKI_DIR=other):
                wstore.ensure_dirs()
                self.assertEqual(wiki_relate.relate(self.BODY, "AI泡沫"), [])
            # 回到原库，原索引仍在（没被别的 root 挤掉语义）
            self.assertEqual(len(wiki_relate.relate(self.BODY, "AI泡沫")), 1)
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_drafts_not_indexed(self):
        """G：只读 wiki/。草稿在 .drafts/，索引不许吃它。"""
        wstore.ensure_dirs()
        d = wstore.root() / ".drafts" / "draft.md"
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_text(PAGE.format(title="AI泡沫", tags="AI泡沫", body="草稿正文。"),
                     encoding="utf-8")
        self.assertEqual(wiki_relate.relate(self.BODY, "AI泡沫"), [])


class ViewWiringTest(_RelateFixture):
    """H：详情视图接线。"""

    def test_detail_view_failure_does_not_break_article(self):
        """计算抛错 → 仍然 200，只是没有那一块。正文是主任务。"""
        from unittest import mock

        from apps.reader import views as rv_mod

        class FakeEntry:
            id, title, content = 1, "随便一篇", "<p>正文</p>"
            feed_title, author, url, reading_time = "源", "", "", 1
            published_at, unread, feed_id = None, False, 1
            status = "unread"

        class FakeDS:
            def get_entry(self, _i):
                return FakeEntry()

            def mark_read(self, _ids):
                return None

            def __getattr__(self, k):
                return lambda *a, **kw: []

        req = RequestFactory().post("/hx/entry/1", {})
        with mock.patch.object(rv_mod, "get_datasource", lambda: FakeDS()), \
                mock.patch.object(rv_mod, "_tree_context", lambda *a: {}), \
                mock.patch.object(wiki_relate, "relate",
                                  side_effect=RuntimeError("boom")):
            resp = rv_mod.hx_entry_detail(req, 1)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("read-relate", resp.content.decode())

    def test_template_renders_rows_and_opens_new_tab(self):
        """★ 用户 2026-11-16 指定：点进 wiki 页要开新标签页，别把正在读的书关掉。"""
        html = render_to_string("wiki/_relate_read.html", {"wiki_relate": [{
            "slug": "ai-pao-mo", "title": "AI泡沫", "page": "wiki/ai/ai-pao-mo.md",
            "domain": "ai", "summary": "", "score": 10.0,
            "why": ["文章标题就是「AI泡沫」", "都提到「算力」"],
        }]})
        self.assertIn('class="read-relate-row"', html)
        self.assertIn('target="_blank"', html)
        self.assertIn('rel="noopener"', html)
        # `?p=` 的值就是相对路径：`/` 在 query 里不需要转义（Django urlencode
        # 默认 safe="/"），中文 slug 会被编成 %XX，这是我们要的形态。
        self.assertIn("?p=wiki/ai/ai-pao-mo.md", html)
        self.assertIn("文章标题就是", html)

    def test_template_renders_nothing_when_empty(self):
        html = render_to_string("wiki/_relate_read.html", {"wiki_relate": []})
        self.assertNotIn("read-relate", html)
