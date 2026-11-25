"""标签在界面上的可见性（2026-11-16）。

背景：标签从 2026-13 步就写进 frontmatter 了，真实库 32 页全都带 3–4 个，
但**界面上一个都没显示** —— 写了三个月的字段没人看见过，也就无从判断这些标签
到底有没有用。这一组钉「看得见」，不钉筛选（筛选的判据在
`test_wiki_tag_filter.py`）。

五条判据：
  1. 首页已入库页表每行显示标签；
  2. 单页详情头部的 meta 行显示标签；
  3. 没有标签的页**不渲染空壳**（宁可什么都不长，也不要一排空 chip）；
  4. 超过 4 个只显示 4 个 + `+N`，且那个 N 必须真实（截断要说出口）；
  5. 标签是**链接**（2026-11-16 晚改的：上一版这里写的是 `assertNotIn`，
     因为那时 `?tag=` 还不存在、做成链接就是 404 空壳。筛选做了，判据就得
     反过来 —— 旧判据留着不删，改产品的人会被它挡住；删了不换，就没人守新行为）。
"""
import shutil
import tempfile

from django.test import Client, TestCase, override_settings

from core import wiki as wstore

PAGE = """---
title: {title}
domain: {domain}
sources: [entry:1234]
updated: 2026-11-01
confidence: medium
tags: [{tags}]
candidates: []
---

正文一句话。
"""


class TagRenderTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-tag-render-")
        cls.ov = override_settings(WIKI_DIR=cls.tmp,
                                   ALLOWED_HOSTS=["testserver"])
        cls.ov.enable()

    @classmethod
    def tearDownClass(cls):
        cls.ov.disable()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        wstore.ensure_dirs()
        self.c = Client()

    def write_page(self, rel, title, tags):
        p = wstore.root() / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(PAGE.format(title=title, domain="ai", tags=tags),
                     encoding="utf-8")
        return p

    def test_index_row_shows_tags(self):
        self.write_page("wiki/ai/foo.md", "测试页", "现金流, 估值")
        html = self.c.get("/wiki").content.decode()
        # chip 是 `<a class="wiki-tag" href="/wiki?tag=…">词</a>`：断言「词可见」
        # 不能锚在 `">词` 上（中间现在隔着 href），否则一改标签形态就假红。
        self.assertIn("现金流", html)
        self.assertIn("估值", html)
        self.assertIn('<a class="wiki-tag" href="/wiki?tag=', html)

    def test_page_head_shows_tags(self):
        self.write_page("wiki/ai/foo.md", "测试页", "现金流, 估值")
        html = self.c.get("/wiki/page", {"p": "wiki/ai/foo.md"}).content.decode()
        head = html.split('class="page-actions"')[1][:900]
        self.assertIn("wiki-tag", head, "标签没进详情页头部")
        self.assertIn("现金流", head)

    def test_no_tags_renders_no_shell(self):
        """空 tags 不许渲染空 chip 容器 —— 那会让每页看起来都有标签。"""
        self.write_page("wiki/ai/empty.md", "无标签页", "")
        for url, kw in (("/wiki", {}),
                        ("/wiki/page", {"p": "wiki/ai/empty.md"})):
            html = self.c.get(url, kw).content.decode()
            self.assertNotIn('class="wiki-tag"', html, "%s 渲染出了空标签壳" % url)

    def test_more_than_four_truncated_with_honest_count(self):
        """6 个标签 → 显示 4 个 + 「+2」；少显示的那两个必须有交代。"""
        six = "一, 二, 三, 四, 五, 六"
        self.write_page("wiki/ai/many.md", "多标签页", six)
        html = self.c.get("/wiki").content.decode()
        self.assertEqual(html.count('class="wiki-tag"'), 4,
                         "首页超过了 4 个 chip，会把其它列挤出屏幕")
        self.assertIn("+2", html, "截断了却没说是几条 —— 静默少显示")

    def test_tags_are_filter_links(self):
        """标签必须是链接，且 href 指到真存在的 `?tag=` 筛选。

        ★ 反向判据（2026-11-16 晚）：`/wiki?tag=X` 当时不存在，所以那时守的是
          `assertNotIn('href="/wiki?tag=')`。现在路由有了、`_ctx` 真过滤了，
          「不是链接」就从正确变成缺陷 —— 一排点不动的 chip 等于把标签的
          唯一职能（横切聚合）阉掉。
        """
        self.write_page("wiki/ai/foo.md", "测试页", "现金流")
        html = self.c.get("/wiki").content.decode()
        self.assertIn('href="/wiki?tag=%E7%8E%B0%E9%87%91%E6%B5%81"', html,
                      "标签不是筛选链接（urlencode 后的 href 没出现）")
        # 点进去必须真有结果，不是 200 的空白页
        filtered = self.c.get("/wiki", {"tag": "现金流"}).content.decode()
        self.assertIn("测试页", filtered)
        self.assertIn("已入库 1", filtered)
