"""标签可点 + 总览页（2026-11-16 晚）。

用户把 32 页的标签逐页过了一遍之后问「接下来呢」。实测现状：93 个实例 / 68 个词，
其中 56 个词只用过 1 次。两件事同时缺：
  1. 93 个 chip 全是死文本 —— 标签唯一的职能是**横切聚合**，点不动就只剩装饰；
  2. 看不见「68 个词各覆盖几页」—— 想继续合也没有着力点（逐页翻 32 次才知道
     「AI风险 和 安全 是不是同一批页」）。

这一组钉的判据：
  A. `?tag=` 真过滤（含与 `?src=` 叠加、strip、空值不过滤、不存在时给空态）
  B. `pages_all_total` 与 `pages_total` 同时给（筛选时能说「7 / 32」）
  C. 总览页按 `n >= 2` / `n == 1` 分组，顺序按页数降序
  D. 总览页每个词挂着「哪些页」—— 判断该不该合靠它
  E. 空库不崩（复用率除零）
"""
import shutil
import tempfile

from django.test import Client, TestCase, override_settings

from core import wiki as wstore

PAGE = """---
title: {title}
domain: ai
sources: [{sources}]
updated: 2026-11-01
confidence: medium
tags: [{tags}]
candidates: []
---

正文一句话。
"""


class _TagFixture(TestCase):
    """两个用例类共用的临时库夹具（`put` 属于这里，不属于某一个测试类）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-tag-filter-")
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
        self.c = Client()

    def put(self, slug, title, tags, sources="entry:1"):
        p = wstore.root() / "wiki" / "ai" / ("%s.md" % slug)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(PAGE.format(title=title, tags=tags, sources=sources),
                     encoding="utf-8")


class TagFilterTest(_TagFixture):
    def seed(self):
        """A 和 B 共用「现金流」，C 只有「估值」，D 没标签。"""
        self.put("a", "甲页", "现金流, 估值", "entry:1")
        self.put("b", "乙页", "现金流", "entry:2")
        self.put("c", "丙页", "估值", "entry:3")
        self.put("d", "丁页", "", "entry:1")

    # ---------------- A. 过滤 ----------------

    def test_tag_filters_pages(self):
        self.seed()
        html = self.c.get("/wiki", {"tag": "现金流"}).content.decode()
        self.assertIn("甲页", html)
        self.assertIn("乙页", html)
        self.assertNotIn("丙页", html, "没这个标签的页也被筛出来了")
        self.assertNotIn("丁页", html)

    def test_tag_and_src_intersect(self):
        """两重过滤取交集 —— 与既有 `?src=` 同一套语义，不新造规则。"""
        self.seed()
        html = self.c.get("/wiki", {"tag": "现金流", "src": "entry:1"}).content.decode()
        self.assertIn("甲页", html)
        self.assertNotIn("乙页", html, "src 那一半没生效（交集变成了并集）")

    def test_tag_is_stripped(self):
        """URL 里手敲的空格不该筛出 0 页。"""
        self.seed()
        html = self.c.get("/wiki", {"tag": "  现金流  "}).content.decode()
        self.assertIn("甲页", html)

    def test_empty_tag_does_not_filter(self):
        """`?tag=` 空值 = 没筛选，不是「筛一个空标签」。"""
        self.seed()
        html = self.c.get("/wiki", {"tag": "  "}).content.decode()
        for t in ("甲页", "乙页", "丙页", "丁页"):
            self.assertIn(t, html, "空 tag 把列表筛空了")

    def test_unknown_tag_explains_itself(self):
        """0 页要说清是「没页带这个词」，不是「库空了」。"""
        self.seed()
        html = self.c.get("/wiki", {"tag": "根本没这个"}).content.decode()
        self.assertIn("没有页带「根本没这个」这个标签了", html)
        self.assertNotIn("还没有入库的页", html, "把「没匹配」说成了「库是空的」")

    def test_empty_library_says_empty(self):
        html = self.c.get("/wiki", {"tag": "任何"}).content.decode()
        self.assertIn("库还是空的", html)

    # ---------------- B. 两个总数 ----------------

    def test_total_shows_filtered_over_all(self):
        self.seed()
        html = self.c.get("/wiki", {"tag": "现金流"}).content.decode()
        self.assertIn("已入库 2", html, "筛出来的页数不对")
        self.assertIn("/ 共 4", html, "没说全库几页，人会以为库变小了")

    def test_no_filter_hides_total(self):
        self.seed()
        html = self.c.get("/wiki").content.decode()
        self.assertNotIn("/ 共 4", html, "没筛选时不该出现「/ 共」")


class TagOverviewTest(_TagFixture):

    def test_groups_split_at_two(self):
        self.put("a", "甲页", "常用词, 唯一词")
        self.put("b", "乙页", "常用词")
        ctx = self.c.get("/wiki/tags").context
        multi = [r["tag"] for r in ctx["multi"]]
        single = [r["tag"] for r in ctx["single"]]
        self.assertEqual(multi, ["常用词"], "出现 2 次的词没进「共用」组")
        self.assertEqual(single, ["唯一词"], "出现 1 次的词没进「单次」组")
        self.assertEqual(ctx["vocab"], 2)
        self.assertEqual(ctx["inst"], 3)

    def test_sorted_by_page_count(self):
        """顺序就是信号：用得多的在前。"""
        self.put("a", "甲页", "三, 二")
        self.put("b", "乙页", "三, 二")
        self.put("c", "丙页", "三")
        rows = self.c.get("/wiki/tags").context["rows"]
        self.assertEqual([r["tag"] for r in rows], ["三", "二"])
        self.assertEqual([r["n"] for r in rows], [3, 2])

    def test_each_tag_carries_its_pages(self):
        self.put("a", "甲页", "共用")
        self.put("b", "乙页", "共用")
        ctx = self.c.get("/wiki/tags").context
        row = ctx["multi"][0]
        # ★ 用 set 不比顺序：`sorted()` 按 Unicode 码点排，`乙`(U+4E59) 会在
        #   `甲`(U+7532) 前面 —— 这不是 bug，是中文没有默认排序。这条判据问的是
        #   「两个页都挂上了吗」，不是「按什么顺序」。
        self.assertEqual({p["title"] for p in row["pages"]}, {"甲页", "乙页"})
        html = self.c.get("/wiki/tags").content.decode()
        self.assertIn("甲页", html, "总览页没把「哪些页」渲染出来")
        self.assertIn("乙页", html)

    def test_reuse_pct(self):
        self.put("a", "甲页", "共用")
        self.put("b", "乙页", "共用, 单次")
        ctx = self.c.get("/wiki/tags").context
        # 2 词 / 3 实例 ⇒ 1 - 2/3 = 33%
        self.assertEqual(ctx["reuse_pct"], 33)

    def test_empty_library_does_not_crash(self):
        """0 实例时复用率不能除零 —— 一打开就 500 最容易被当成功能坏了。"""
        r = self.c.get("/wiki/tags")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.context["reuse_pct"])
        html = r.content.decode()
        self.assertIn("复用率 <b>—</b>", html)

    def test_all_single_has_no_multi(self):
        self.put("a", "甲页", "独一")
        self.put("b", "乙页", "独二")
        ctx = self.c.get("/wiki/tags").context
        self.assertEqual(ctx["multi"], [])
        html = self.c.get("/wiki/tags").content.decode()
        self.assertIn("还没有被两页以上共用的词", html)
