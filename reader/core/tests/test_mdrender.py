"""mdrender 的单测（此前只有零散脚本断言，没有真正的单元测试）。

★ 第一条的来由（2026-10-31 用户报障「要点全是 1. 开头」）：
    模型输出的是规范的 `1.` `2.` `3.`（项与项之间**空一行**，即 CommonMark 的
    「松散列表」），而 mdrender 只吃**连续**的列表行 ⇒ 5 条要点渲染成 5 个独立
    的 `<ol>`，每个 `<ol>` 各含一项，浏览器各自从 1 数起 —— 页面上每一行都写着
    「1.」。用户判断「是 md 渲染侧的问题」，判断正确。
    ★ 断言打在 render() 的输出上，不是打「模板里有没有 safe」那种错靶子。
"""
from django.test import SimpleTestCase

from core import mdrender


def R(md):
    return mdrender.render(md)


class LooseListTest(SimpleTestCase):
    def test_ordered_items_separated_by_blank_lines_are_one_list(self):
        # 真实故障形状：每项之间一个空行
        html = R("## 要点\n\n1. **甲**：一\n\n2. **乙**：二\n\n3. **丙**：三\n")
        self.assertEqual(html.count("<ol"), 1, html)
        self.assertEqual(html.count("<li>"), 3, html)
        self.assertIn("1 个 ol", "%d 个 ol" % html.count("<ol"))

    def test_bulleted_items_separated_by_blank_lines_are_one_list(self):
        html = R("- 甲\n\n- 乙\n\n- 丙\n")
        self.assertEqual(html.count("<ul>"), 1, html)
        self.assertEqual(html.count("<li>"), 3, html)

    def test_tight_list_still_one_block(self):
        # 不回归：连续的列表行本来就是一块
        self.assertEqual(R("1. 甲\n2. 乙\n3. 丙\n").count("<ol"), 1)
        self.assertEqual(R("1. 甲\n2. 乙\n3. 丙\n").count("<li>"), 3)

    def test_blank_line_then_other_list_type_is_a_new_list(self):
        # `1.` 之后空行接 `- `：语义不同，不能揉成一个列表
        html = R("1. 甲\n2. 乙\n\n- 丙\n")
        self.assertEqual(html.count("<ol"), 1, html)
        self.assertEqual(html.count("<ul>"), 1, html)
        self.assertEqual(html.count("<li>"), 3, html)

    def test_paragraph_ends_the_list(self):
        # 列表 → 空行 → 普通段落：段落不能被吞进 <li>
        html = R("1. 甲\n2. 乙\n\n这是段落。\n")
        self.assertEqual(html.count("<li>"), 2, html)
        self.assertIn("<p>这是段落。</p>", html)
        self.assertNotIn("这是段落。</li>", html)

    def test_heading_ends_the_list(self):
        html = R("1. 甲\n2. 乙\n\n## 下一节\n\n正文\n")
        self.assertEqual(html.count("<li>"), 2, html)
        self.assertIn("<h2>下一节</h2>", html)

    def test_start_number_is_preserved(self):
        # 模型常从 3. 续写（前文已经 1. 2.），<ol> 默认从 1 数起会错位
        html = R("3. 丙\n4. 丁\n")
        self.assertIn('<ol start="3">', html)
        self.assertEqual(html.count("<li>"), 2)

    def test_start_number_one_has_no_attribute(self):
        # start=1 时不写属性（写了也无害，但让输出稳定、好断言）
        self.assertNotIn("start=", R("1. 甲\n"))

    def test_bogus_start_falls_back_to_one(self):
        for md in ("0. 甲\n", "99999. 甲\n"):
            self.assertNotIn("start=", R(md), md)


class ListInlineTest(SimpleTestCase):
    def test_inline_marks_inside_loose_list(self):
        html = R("1. **粗**：一\n\n2. `码`：二\n")
        self.assertIn("<strong>粗</strong>", html)
        self.assertIn("<code>码</code>", html)
        self.assertEqual(html.count("<ol"), 1)

    def test_html_is_escaped_in_list_items(self):
        # 先 escape 再套标签：模型输出 <script> 只能显示成文本
        html = R("1. <script>alert(1)</script>\n")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
