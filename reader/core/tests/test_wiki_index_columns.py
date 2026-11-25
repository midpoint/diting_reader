"""wiki 首页页表：来源列换成标签列（2026-11-16 用户提）。

原话：「我觉得 wiki 首页的来源列意义不大，换成 tags 列可能更好一些。或者可以先
在来源前加一个 tags 列。」

核对的真实库事实（只读）：`sources` 是 `entry:7703` / `report:daily-2026-10-30`
这类 id，单页最多 9 个 —— 一列 chip 全是噪声；而标签是人自己看过一遍收敛出来的
词，2026-11-16 起还能点（`?tag=` 真过滤）。所以**换列**而不是加列：加列会让同一
组标签在标题格里也出现一遍，两处显示 = 两处口径。

判据五条：
  1. 表头顺序 = 页 / 更新 / 置信 / 标签（来源列不再存在）；
  2. 标签在**独立格**（`td.tag-cell`），第一格不重复显示（防两处都渲染）；
  3. 溯源降级但不丢：列表里「N 源」+ hover 看全，`src-chip` 不再出现在列表；
  4. 反向映射照旧：`?src=` 仍能筛出同源页（入口挪走 ≠ 功能下线）；
  5. 没标签的页给「—」，不是空白格（空白会被当成渲染坏了）。
"""
from django.test import Client

from core.tests.test_wiki_tag_filter import _TagFixture


def body(html):
    import re
    return re.sub(r"\s+", " ", html)


class IndexColumnsTest(_TagFixture):
    def setUp(self):
        super().setUp()
        self.put("jia", "页甲", "选股, 市值", sources="entry:11, entry:12")
        self.put("yi", "页乙", "", sources="entry:11")
        self.put("bing", "页丙", "安全", sources="report:daily-2026-11-01")

    def index(self):
        resp = self.c.get("/wiki")
        self.assertEqual(resp.status_code, 200)
        return body(resp.content.decode("utf-8"))

    # ------------------------------------------------------------ 1. 表头顺序
    def test_headers_are_page_upd_conf_tags(self):
        html = self.index()
        order = [html.find(">%s</th>" % x) for x in ("页", "更新", "置信", "标签")]
        self.assertTrue(all(i > 0 for i in order), order)
        self.assertEqual(order, sorted(order), "四列表头顺序变了")
        self.assertNotIn(">来源</th>", html, "来源列应已下线")

    # ------------------------------------------------------------ 2. 标签独立格
    def test_tags_live_in_their_own_cell_only(self):
        html = self.index()
        self.assertIn('<td class="tag-cell">', html)
        # 第一格（标题 + 字数 + N 源）里不该再出现标签 chip —— 两处显示会漂
        first_td = html.split("<tbody>")[1].split("</td>")[0]
        self.assertNotIn('class="wiki-tag"', first_td, "标签不该在标题格里再来一份")
        self.assertIn("选股", html)
        self.assertIn('href="/wiki?tag=%E9%80%89%E8%82%A1"', html, "chip 必须可点")

    # -------------------------------------------------------- 3. 溯源降级不丢
    def test_source_column_gone_but_count_kept(self):
        html = self.index()
        self.assertNotIn("src-chip", html, "来源 chip 不该再堆在列表里")
        self.assertIn("· 2 源", html, "两源要给数量")
        self.assertIn("· 1 源", html)
        self.assertIn('title="来源：entry:11、entry:12"', html,
                      "数量之外还要能 hover 看全（降级不是删除）")

    # -------------------------------------------------------- 4. 反查能力不变
    def test_src_filter_still_works(self):
        resp = self.c.get("/wiki", {"src": "entry:11"})
        html = body(resp.content.decode("utf-8"))
        self.assertIn("正在按来源筛选", html)
        self.assertIn("页甲", html)
        self.assertIn("页乙", html)
        self.assertNotIn("页丙", html, "别的来源的页不该混进来")

    # ------------------------------------------------------------ 5. 无标签空态
    def test_page_without_tags_shows_dash_not_blank(self):
        html = self.index()
        row = next((r for r in html.split("<tr>") if "页乙" in r), "")
        self.assertTrue(row, "没找到「页乙」那一行")
        cell = row.split('class="tag-cell">', 1)[1].split("</td>")[0]
        self.assertNotIn('class="wiki-tag"', cell, "这页本来就没标签")
        self.assertIn("—", cell, "没有标签要给一个可见占位")
