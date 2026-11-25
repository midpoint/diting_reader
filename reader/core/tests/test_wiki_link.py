"""wiki 正文 `[[slug]]` 可点（2026-11-11）。

背景：真实库清零重来后，新页正文真的开始写内链了（7 页 / 5 页含 11 处
`[[...]]`），但 `mdrender` 不认识它，屏幕上就是字面方括号，点不动。

★ 本文件的判据分三层，缺一层就会静默：
  1. **渲染器**：不传 resolver 时行为一字不变（日报 / 划线 / 页面规范示例
     都靠这个「不传」保护）；label / href 必须 escape；断链不许做成 404 死链。
  2. **解析器**：显示目标页中文标题而不是 slug；目标不存在 / 重名要看得见；
     `missing` 去重保序，面板才能一句话说清。
  3. **视图**：详情页新页 / 老页回退**两支**都要接，预览两个端点也要接
     （`draft_preview` 的 docstring 本来就承诺「预览和落盘一致」）。

★ 正则唯一性：`core.mdlines.WIKILINK` 全项目一份。`wiki_loop` 用 `findall()`，
  多一个捕获组就会把返回值从 slug 列表变成 tuple 列表 —— 有一条判据专门钉住。
"""
import re
from unittest.mock import patch

from django.test import SimpleTestCase

from core import mdrender, wiki as wstore
from core.mdlines import WIKILINK, wikilink_parts

from .test_wiki_page_read import PageReadBase, plain


def _ok(label="乙页", href="/wiki/page?p=wiki/ai/beta.md"):
    def resolve(slug, alias=""):
        # 与真实 `WikilinkResolver` 同口径：写了别名就优先用别名。
        return {"href": href, "label": alias or label,
                "missing": False, "why": ""}
    return resolve


def _missing(why="目标页不存在"):
    return lambda slug, alias="": {"label": alias or slug, "missing": True,
                                   "why": why}


# ------------------------------------------------------------------ 渲染器
class RendererTest(SimpleTestCase):
    def test_no_resolver_keeps_literal_text(self):
        """反证：日报 / 划线 / 页面规范示例不传 resolver，`[[...]]` 必须仍是字面。"""
        html = mdrender.render("这句提到 [[beta]] 和 [[alpha|甲页]]。")
        self.assertIn("[[beta]]", html)
        self.assertIn("[[alpha|甲页]]", html)
        self.assertNotIn('class="wikilink"', html)

    def test_link_uses_target_title_and_keeps_slug_in_title_attr(self):
        html = mdrender.render("这句提到 [[beta]]。", wikilinks=_ok())
        self.assertIn('<a class="wikilink" href="/wiki/page?p=wiki/ai/beta.md" '
                      'title="beta">乙页</a>', html)
        self.assertNotIn("[[beta]]", html)

    def test_alias_is_passed_to_resolver(self):
        """别名解析只在 `mdlines.wikilink_parts` 一处，渲染器负责把它带过去。"""
        seen = []

        def resolve(slug, alias=""):
            seen.append((slug, alias))
            return {"href": "/wiki/page?p=wiki/ai/beta.md", "label": alias,
                    "missing": False, "why": ""}

        mdrender.render("对照 [[beta|那个页]] 看。", wikilinks=resolve)
        self.assertEqual(seen, [("beta", "那个页")])

    def test_alias_beats_target_title(self):
        html = mdrender.render("对照 [[beta|那个页]] 看。", wikilinks=_ok())
        self.assertIn('>那个页</a>', html)
        self.assertNotIn(">乙页</a>", html)

    def test_missing_target_is_span_not_dead_anchor(self):
        html = mdrender.render("指向 [[ghost]] 的那一处。", wikilinks=_missing())
        self.assertNotIn("<a ", html)
        self.assertIn('class="wikilink is-missing"', html)
        self.assertIn("目标页不存在", html)
        self.assertIn("[[ghost]]", plain(html))

    def test_label_and_href_are_escaped(self):
        """frontmatter 的标题来自模型，`|safe` 输出前必须在这里挡一次。"""
        html = mdrender.render("看 [[beta]]。", wikilinks=_ok(
            label='<script>alert(1)</script>',
            href='/wiki/page?p=x" onclick="alert(1)'))
        self.assertNotIn("<script>", html)
        self.assertNotIn('onclick="alert(1)', html)
        self.assertIn("&lt;script&gt;", html)

    def test_non_local_href_is_refused(self):
        """渲染器最后一道闸：resolver 拼了外链也不许带进 `|safe`。"""
        html = mdrender.render("看 [[beta]]。", wikilinks=_ok(href="https://evil.example/x"))
        self.assertNotIn("<a ", html)
        self.assertIn("[[beta]]", html)

    def test_resolver_returning_none_keeps_literal(self):
        html = mdrender.render("看 [[beta]]。", wikilinks=lambda s, a: None)
        self.assertIn("[[beta]]", html)

    def test_code_block_is_not_linkified(self):
        html = mdrender.render("```\n[[beta]]\n```\n\n正文 [[beta]]。",
                               wikilinks=_ok())
        self.assertIn("<pre><code>[[beta]]", html)
        self.assertIn('title="beta">乙页</a>', html)

    def test_all_inline_contexts_are_linkified(self):
        """同类扫全集：标题 / 列表 / 引用 / 表格都走 `_inline`，不许漏一支。"""
        md = ("# 标题 [[beta]]\n\n"
              "- 列表 [[beta]]\n\n"
              "> 引用 [[beta]]\n\n"
              "| 列 | 值 |\n| --- | --- |\n| x | [[beta]] |\n")
        html = mdrender.render(md, wikilinks=_ok())
        self.assertEqual(html.count('<a class="wikilink"'), 4)

    def test_ampersand_in_slug_is_unescaped_before_lookup(self):
        """`html.escape` 先跑 ⇒ slug 里的 & 会变成 &amp;，查表前必须还原。"""
        seen = []

        def resolve(slug, alias=""):
            seen.append(slug)
            return {"href": "/wiki/page?p=wiki/ai/a.md", "label": "A&B",
                    "missing": False, "why": ""}

        html = mdrender.render("看 [[a&b]]。", wikilinks=resolve)
        self.assertEqual(seen, ["a&b"])
        self.assertIn("A&amp;B", html)

    def test_slug_with_spaces_is_stripped(self):
        seen = []

        def resolve(slug, alias=""):
            seen.append(slug)
            return {"href": "/wiki/page?p=wiki/ai/beta.md", "label": "乙页",
                    "missing": False, "why": ""}

        mdrender.render("看 [[ beta ]]。", wikilinks=resolve)
        self.assertEqual(seen, ["beta"])


# ------------------------------------------------------------------ 正则唯一性
class RegexSingleSourceTest(SimpleTestCase):
    def test_wiki_module_uses_the_shared_regex_object(self):
        """运行时确实共享一份。

        ⚠️ 这条**单独不够**：`re.compile` 对相同 pattern 会返回缓存里的同一个
        对象，有人在 wiki.py 里重新写一份一模一样的正则，这里照样通过。
        所以下面还有一条扫源码全集的判据（2026-11-11 变异反证 M9 抓到的坑）。
        """
        self.assertIs(wstore._WIKILINK_RE, WIKILINK)

    def test_only_one_pattern_literal_in_production_code(self):
        """静态全集守卫：通用解析式 `r"\\[\\[([^\\]\\|]+?)..."` 只能 mdlines 一处。

        ★ 论域必须收窄到**通用解析式**（带任意 slug 的捕获组）：
          `wiki_agent.py` 为「改某个 slug」动态拼的 `r"\\[\\[" + re.escape(x)`
          是合法的第二类用法，把它们算进来守卫会永远红。
        ★ 不扫 tests / scripts / migrations：判据自己和测试夹具里出现这个
          字面量是合法的（「守卫的论域」比公式更易错）。
        """
        from pathlib import Path
        root = Path(wstore.__file__).resolve().parent.parent  # reader/
        files = list((root / "core").glob("*.py")) + list((root / "apps").rglob("*.py"))
        needle = r"\[\[([^\]\|]+?)"
        hits = []
        for p in sorted(files):
            if "__pycache__" in p.parts:
                continue
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                # ★ 只认正则**形状**，不挑 `re.compile` 的调用写法：
                #   `_re.compile(...)` / `re.compile(...)` 都算，写守卫时靠
                #   `"re.compile("` 判断曾被一个别名调用绕过（M9 第一次没响）。
                if needle in line:
                    hits.append("%s:%d" % (p.relative_to(root), i))
        self.assertEqual(len(hits), 1, hits)
        self.assertTrue(hits[0].startswith("core/mdlines.py:"), hits)

    def test_findall_still_returns_plain_slugs(self):
        """`wiki_loop` 用 findall() 拿 slug 列表；多一个捕获组就悄悄变 tuple。"""
        self.assertEqual(WIKILINK.findall("[[a]] 和 [[b|别名]]"), ["a", "b"])

    def test_alias_parts_are_parsed_once(self):
        m = WIKILINK.search("看 [[beta|那个页]] 去")
        self.assertEqual(wikilink_parts(m), ("beta", "那个页"))
        m2 = WIKILINK.search("看 [[beta]] 去")
        self.assertEqual(wikilink_parts(m2), ("beta", ""))


# ------------------------------------------------------------------ 解析器 + 视图
class ResolverTest(PageReadBase):
    def test_existing_page_gives_href_title_and_no_missing(self):
        self.mk_page("beta", "乙页的结论。", title="乙页")
        lk = wstore.wikilink_resolver()
        got = lk("beta", "")
        self.assertFalse(got["missing"])
        self.assertEqual(got["href"], "/wiki/page?p=wiki/ai/beta.md")
        self.assertEqual(got["label"], "乙页")
        self.assertEqual(lk.missing, [])

    def test_missing_slug_recorded_once_and_in_order(self):
        self.mk_page("beta", "乙页的结论。", title="乙页")
        lk = wstore.wikilink_resolver()
        self.assertTrue(lk("ghost")["missing"])
        self.assertTrue(lk("ghost2")["missing"])
        self.assertTrue(lk("ghost")["missing"])
        self.assertEqual(lk.missing, ["ghost", "ghost2"])

    def test_duplicate_slug_is_missing_with_a_reason(self):
        """与 find_page 同口径：猜一个等于静默把人带到错的页。"""
        self.mk_page("same", "A 域的页", domain="ai")
        self.mk_page("same", "I 域的页", domain="invest")
        lk = wstore.wikilink_resolver()
        got = lk("same")
        self.assertTrue(got["missing"])
        self.assertIn("重名", got["why"])
        self.assertIn("same", lk.missing)

    def test_alias_is_honoured_by_resolver(self):
        self.mk_page("beta", "乙页的结论。", title="乙页")
        self.assertEqual(wstore.wikilink_resolver()("beta", "那个页")["label"], "那个页")


class DetailViewTest(PageReadBase):
    def mk_linked(self, body=None):
        """A 页正文引用已入库的 B 和一个不存在的 ghost。"""
        self.mk_page("beta", "乙页的结论。", title="乙页")
        rel = self.mk_page("alpha", body or "先看 [[beta]]，再看 [[ghost]]。",
                           title="甲页")
        self.reindex()
        return rel

    def test_detail_page_renders_clickable_link_and_visible_notice(self):
        resp = self.get_page(self.mk_linked())
        html = resp.content.decode("utf-8")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('<a class="wikilink" href="/wiki/page?p=wiki/ai/beta.md" '
                      'title="beta">乙页</a>', html)
        self.assertNotIn("[[beta]]", plain(html))
        self.assertIn("内链没找到唯一的目标页", plain(html))
        self.assertIn("ghost", plain(html))
        # 断链不许变成 <a>：链接总数只能是 1（加上页面上其它按钮不算 wikilink）
        self.assertEqual(html.count('<a class="wikilink"'), 1)

    def test_legacy_branch_also_linkifies(self):
        """★ 同一能力两支分支：老页回退走 `body_layer or body`，漏传就永远点不动。"""
        rel = self.mk_linked("## 我的判断\n\n老页也要能点 [[beta]]。\n")
        html = self.get_page(rel).content.decode("utf-8")
        self.assertIn('title="beta">乙页</a>', html)
        self.assertNotIn("[[beta]]", plain(html))

    def test_no_missing_link_no_noise(self):
        """反证：全都能对上时，不许冒一句无意义的提示。"""
        self.mk_page("beta", "乙页的结论。", title="乙页")
        rel = self.mk_page("alpha", "只有 [[beta]]。", title="甲页")
        self.reindex()
        text = plain(self.get_page(rel).content.decode("utf-8"))
        self.assertNotIn("内链没找到唯一的目标页", text)

    def test_both_preview_endpoints_use_the_same_renderer(self):
        self.mk_page("beta", "乙页的结论。", title="乙页")
        for url in ("/wiki/draft/preview", "/wiki/page/preview"):
            with self.subTest(url=url):
                html = self.client.post(url, {"body": "预览 [[beta]] 也要可点。"})
                self.assertEqual(html.status_code, 200)
                self.assertIn('title="beta">乙页</a>', html.content.decode("utf-8"))

    def test_page_context_does_not_hardcode_library_state(self):
        """判据不许拿「当前库有几页」当期望：新造一页也能立刻被链上。"""
        self.mk_page("beta", "乙页的结论。", title="乙页")
        rel = self.mk_page("alpha", "先看 [[beta]]。", title="甲页")
        self.assertEqual(self.get_page(rel).context["link_missing"], [])
        self.mk_page("gamma", "丙页的结论。", title="丙页")
        rel2 = self.mk_page("delta", "看看 [[gamma]]。", title="丁页")
        html = self.get_page(rel2).content.decode("utf-8")
        self.assertIn('>丙页</a>', html)
