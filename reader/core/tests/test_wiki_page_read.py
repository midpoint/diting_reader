"""第十三步（形态 A）：wiki 详情页阅读态（纯 polish 正文 / 折叠溯源 / 反链上下文 / 时间呈现）。

★ 单测盯「源码事实」，真浏览器 e2e 另验「页面没脏东西」（见 e2e_wiki_page_read.js）。
★ 形态 A 定案：正文 = **纯 polish 一段**（不含三块 / 双层 / 候选清单标题）；
  分组唯一真值是 frontmatter `candidates`；每条判断的精确出处由折叠「溯源」区
  读 IngestCandidate 结构化字段、按 quote_from 分组负责。
★ 最贵的一条不是「双层页对不对」，而是**老页 / 空结论**：真实库 32/32 页仍是
  三块旧正文、0 页含 `## 通顺话`，若把唯一可读的正文折叠掉、或正文空时不给
  可见说明，页面不会报错、日志干净、只是没人读得到 —— 静默降级比报错贵。
  所以「正文直显、不塞进 details」「空结论有可见说明」「折叠溯源区展开后必须
  能逐条读到分组 / 条数 / 原话 / 出处 / 发布时间」是三条等价承诺，一条不能删。
★ 反链索引形状 `{slug: [rel]}` 是断链判据 / commit / 删页的公共契约，
  本步只加读时上下文，一条断言专门钉住「形状没被顺手改掉」。
"""
import json
import re
import shutil
import tempfile
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from core import wiki as wstore
from core import wiki_agent as wa
from core.models import IngestCandidate

PAGE = """---
title: {title}
domain: ai
sources: [entry:1001]
updated: 2026-11-08
confidence: medium
tags: []
candidates: [1]
---

{body}
"""

TWO_LAYER = """## 候选清单

- `#1` 这是我的判断
  - 原话：我的原话值得沉淀
  - 出处：entry:1001 · 2026-11-01

## 通顺话

这是结论段，先读到它。
"""

OLD_BODY = """## 我的判断
- 老页正文一句话，必须原样看得见

## 事实 / 他人的观点
- 别人的观点也在这里

## 我的在意点
- （无）
"""

# 清零前「旧双层 / 候选清单」老页：含 BODY_HEAD、没有 POLISH_HEAD。
# 形态 A 的 views.page() 正是靠 BODY_HEAD/POLISH_HEAD 认老页、整段直显 + 可见说明。
LEGACY_BODY = """## 候选清单

- `#1` 老页里的一条判断，必须原样看得见
  - 原话：老页里的原话也在这一段
  - 出处：entry:1001 · 2026-11-01
"""


def plain(html):
    """剥标签后的可见文字（只验关键话有没有出现在屏幕上）。"""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


class PageReadBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-page12-")
        cls.ov = override_settings(WIKI_DIR=cls.tmp)
        cls.ov.enable()

    @classmethod
    def tearDownClass(cls):
        cls.ov.disable()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        wstore.ensure_dirs()
        self.rf_pages = {}

    def mk_page(self, slug, body, *, title=None, domain="ai", candidates=None):
        d = wstore.root() / "wiki" / domain
        d.mkdir(parents=True, exist_ok=True)
        text = PAGE.format(title=title or slug, body=body)
        if candidates is not None:
            # 只换这一行、保持其它 frontmatter 原样：溯源区读的就是这一份真值。
            text = text.replace(
                "candidates: [1]",
                "candidates: [%s]" % ", ".join(str(i) for i in candidates), 1)
        (d / (slug + ".md")).write_text(text, encoding="utf-8")
        rel = "wiki/%s/%s.md" % (domain, slug)
        self.rf_pages[slug] = rel
        return rel

    def mk_cand(self, *, claim, quote, quote_from="mine",
                source_type="report", source_id="daily-abc"):
        """造一条真实候选：折叠溯源区读的是 DB 结构化字段，不解析正文。"""
        return IngestCandidate.objects.create(
            batch="t", source_type=source_type, source_id=source_id,
            kind="decision", claim=claim, quote=quote, quote_from=quote_from,
            domain="ai", confidence="medium", status="pending",
            published_at=timezone.now())

    def reindex(self):
        return wstore.rebuild_backlinks_index(mark_log=False)

    def get_page(self, rel):
        return self.client.get("/wiki/page", {"p": rel})


# ------------------------------------------------------------------ 核心层
class BacklinkContextsTest(PageReadBase):
    def test_no_backlinks_returns_empty(self):
        self.mk_page("alpha", TWO_LAYER)
        self.reindex()
        self.assertEqual(wstore.backlink_contexts("alpha"), [])

    def test_one_backlink_carries_the_quoting_sentence(self):
        self.mk_page("alpha", TWO_LAYER)
        self.mk_page("beta", "## 通顺话\n\n甲页的结论要看 [[alpha]] 才完整。\n")
        self.reindex()
        rows = wstore.backlink_contexts("alpha")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["page"], self.rf_pages["beta"])
        self.assertEqual(rows[0]["quotes"], ["甲页的结论要看 alpha 才完整。"])
        self.assertFalse(rows[0]["missing"])

    def test_alias_link_is_normalized_to_display_name(self):
        self.mk_page("alpha", TWO_LAYER)
        self.mk_page("beta", "## 通顺话\n\n对照 [[alpha|甲页]] 才看得懂。\n")
        self.reindex()
        rows = wstore.backlink_contexts("alpha")
        self.assertEqual(rows[0]["quotes"], ["对照 甲页 才看得懂。"])

    def test_multiple_sentences_and_visible_more_count(self):
        self.mk_page("alpha", TWO_LAYER)
        self.mk_page("beta", "## 通顺话\n\n"
                            "第一句 [[alpha]]。\n"
                            "第二句 [[alpha]]。\n"
                            "第三句 [[alpha]]。\n"
                            "第四句 [[alpha]]。\n")
        self.reindex()
        rows = wstore.backlink_contexts("alpha")
        self.assertEqual(len(rows[0]["quotes"]), 3)
        self.assertEqual(rows[0]["more"], 1,
                         "静默截断最贵：多出来的句子必须报数")

    def test_links_to_other_slugs_do_not_leak_into_this_page(self):
        self.mk_page("alpha", TWO_LAYER)
        self.mk_page("beta", TWO_LAYER)
        self.mk_page("gamma", "## 通顺话\n\n这句只提 [[beta]]，不提 alpha。\n"
                              "这句才提 [[alpha]]。\n")
        self.reindex()
        rows = wstore.backlink_contexts("alpha")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["page"], self.rf_pages["gamma"])
        self.assertEqual(rows[0]["quotes"], ["这句才提 alpha。"])

    def test_body_layer_links_are_scanned_too(self):
        """wikilink 主要出现在 polish，但正文层写了也要抓到（同一个正则口径）。"""
        self.mk_page("alpha", TWO_LAYER)
        self.mk_page("beta", "## 候选清单\n\n- `#1` 判断\n"
                            "  - 原话：提到 [[alpha]] 的这句在 body 层\n")
        self.reindex()
        rows = wstore.backlink_contexts("alpha")
        self.assertEqual(len(rows), 1)
        self.assertTrue(any("body 层" in q for q in rows[0]["quotes"]))

    def test_stale_index_marks_missing_instead_of_blank(self):
        """索引记着这页引用过，但正文里已经改没了 —— 必须可见，不能空白。"""
        self.mk_page("alpha", TWO_LAYER)
        beta = self.mk_page("beta", "## 通顺话\n\n原先写了 [[alpha]]。\n")
        self.reindex()
        (wstore.root() / beta).write_text(
            PAGE.format(title="beta", body="## 通顺话\n\n后来把链接删了。\n"),
            encoding="utf-8")
        rows = wstore.backlink_contexts("alpha")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["missing"])
        self.assertEqual(rows[0]["quotes"], [])

    def test_deleted_referring_page_is_still_listed(self):
        self.mk_page("alpha", TWO_LAYER)
        beta = self.mk_page("beta", "## 通顺话\n\n提到 [[alpha]]。\n")
        self.reindex()
        (wstore.root() / beta).unlink()
        rows = wstore.backlink_contexts("alpha")
        self.assertEqual(rows[0]["page"], beta)
        self.assertTrue(rows[0]["missing"])

    def test_self_link_is_not_a_backlink(self):
        self.mk_page("alpha", "## 通顺话\n\n自夸 [[alpha]] 不算反链。\n")
        self.reindex()
        self.assertEqual(wstore.backlinks_of("alpha"), [])
        self.assertEqual(wstore.backlink_contexts("alpha"), [])

    def test_index_shape_is_unchanged(self):
        """下游 broken_links / commit / 删页 / 两条 e2e 都吃字符串数组。"""
        self.mk_page("alpha", TWO_LAYER)
        self.mk_page("beta", "## 通顺话\n\n提到 [[alpha]]。\n")
        self.reindex()
        raw = json.loads((wstore.root() / "wiki/backlinks.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["alpha"], [self.rf_pages["beta"]])
        self.assertEqual(wstore.backlinks_of("alpha"), [self.rf_pages["beta"]])
        self.assertTrue(all(isinstance(x, str) for x in raw["alpha"]))

    def test_per_page_argument_is_honored(self):
        self.mk_page("alpha", TWO_LAYER)
        self.mk_page("beta", "## 通顺话\n\n一 [[alpha]]。\n二 [[alpha]]。\n三 [[alpha]]。\n")
        self.reindex()
        rows = wstore.backlink_contexts("alpha", per_page=1)
        self.assertEqual(len(rows[0]["quotes"]), 1)
        self.assertEqual(rows[0]["more"], 2)


# ------------------------------------------------------------------ 视图 / 模板
class PageReadViewTest(PageReadBase):
    def test_two_layer_page_shows_polish_and_folds_body(self):
        """形态 A：正文 = 纯 polish 一段，直接可见、不进折叠区；精确出处收进
        折叠「溯源」区。旧「双层页 polish + 折叠权威清单」的承诺改写为：
        ① 结论可见且不含任何结构标题；② <details> 真的存在，且「展开后」
        逐条可见分组 / 条数 / 原话 / 出处 / 发布时间 / 看原文；③ 位置序反证
        「结论没被折进溯源区、溯源明细确实在 details 里」。"""
        c = self.mk_cand(claim="这是我的判断", quote="我的原话值得沉淀",
                         quote_from="mine")
        rel = self.mk_page("alpha", "这是结论段，先读到它。", candidates=[c.id])
        html = self.get_page(rel).content.decode("utf-8")
        txt = plain(html)

        # ① 正文 = 纯 polish：frontmatter candidates 是分组唯一真值
        self.assertIn("这是结论段，先读到它。", txt)
        body = wstore.read_page(rel)["body"].strip()
        self.assertEqual(body, "这是结论段，先读到它。")
        for h in ("## 候选清单", "## 通顺话", "## 我的判断",
                  "## 事实 / 他人的观点", "## 我的在意点"):
            self.assertNotIn(h, body)
        fm = wstore.parse_front(
            (wstore.root() / rel).read_text(encoding="utf-8"))[0]
        self.assertEqual(wstore._cand_ids(fm), [c.id])

        # ② 「折叠」是产品承诺：<details> 折叠时 innerText 恒空，所以
        #    直接断言 <details> 存在 + 取 details 块内的可见文字（展开后读）。
        self.assertIn("<details", html)
        i_details = html.index("<details")
        i_end = html.index("</details>", i_details)
        details_txt = plain(html[i_details:i_end])
        self.assertIn("溯源 1 条", details_txt)              # 条数（summary 恒可见）
        self.assertIn("我的判断 1", details_txt)             # 分组 + 组内条数
        self.assertIn("这是我的判断", details_txt)           # claim
        self.assertIn("我的原话值得沉淀", details_txt)       # 原话
        self.assertIn("report:daily-abc", details_txt)      # 出处
        self.assertIn("看原文", details_txt)                 # link 的可见文字
        # 结构化字段的精确值也钉一遍（不许只断言 DOM 结构）
        trace = wa.trace_rows([c.id])
        self.assertEqual(trace["total"], 1)
        self.assertEqual(trace["groups"][0]["key"], "mine")
        self.assertEqual(trace["groups"][0]["label"], "我的判断")
        row = trace["groups"][0]["rows"][0]
        self.assertEqual(row["source"], "report:daily-abc")
        self.assertTrue(row["published_at"],
                        "候选没有发布时间 ⇒ 下面这条会变成空集合假绿")
        self.assertIn(row["published_at"][:10], details_txt)  # 发布时间
        self.assertIn("/summary/", row["link"])               # 精确出处深链
        self.assertIn("src=wiki", row["link"])

        # ③ 位置序反证：结论在 <details> 之前，溯源明细在 <details> 里
        i_polish = html.index("这是结论段，先读到它。")
        i_quote = html.index("我的原话值得沉淀")
        self.assertLess(i_polish, i_details, "结论段必须在折叠溯源区之前")
        self.assertLess(i_details, i_quote, "原话必须真的在折叠溯源区里")
        self.assertNotIn("这是结论段", details_txt, "结论不该被折进溯源区")

    def test_old_page_without_polish_shows_full_body_unfolded(self):
        """真实库 32/32 页走这条分支：正文一字不少、不塞进 <details>。形态 A
        把「没有结论」的可见说明放在面板里（不是塞进正文），分别覆盖：
        ① 真实三块老页整段直显；② 含 BODY_HEAD 的老页有「清零前的老页」
        可见说明；③ 正文空有「结论还没生成」可见说明。"""
        # ① 真实库老页（三块旧正文，无 BODY_HEAD/POLISH_HEAD）：一字不少
        rel = self.mk_page("old", OLD_BODY)
        r = self.get_page(rel)
        self.assertEqual(r.status_code, 200)
        html = r.content.decode("utf-8")
        txt = plain(html)
        self.assertIn("老页正文一句话，必须原样看得见", txt)
        self.assertIn("别人的观点也在这里", txt)
        self.assertIn("（无）", txt)
        # `## 标题` 经 mdrender 变成 <h2>，可见文字里没有 `##`；
        # 用结构断言钉住三个老标题都还在，一个没被吞。
        for h in ("我的判断", "事实 / 他人的观点", "我的在意点"):
            self.assertIn("<h2>" + h + "</h2>", html)
        self.assertEqual(wstore.read_page(rel)["body"].strip(), OLD_BODY.strip(),
                         "老页正文一个字节都没被改写")
        # ★ 老页不该把唯一可读的内容塞进 <details>（溯源区在最后）。
        self.assertLess(html.index("老页正文一句话，必须原样看得见"),
                        html.index("<details"))

        # ② 含 BODY_HEAD 的老页：整段直显 + 「清零前的老页」可见说明
        rel2 = self.mk_page("legacy", LEGACY_BODY)
        html2 = self.get_page(rel2).content.decode("utf-8")
        txt2 = plain(html2)
        self.assertIn("清零前的老页", txt2)
        self.assertIn("沿用旧版正文直显", txt2)
        self.assertIn("老页里的一条判断，必须原样看得见", txt2)
        self.assertIn("老页里的原话也在这一段", txt2)
        self.assertLess(html2.index("老页里的一条判断"),
                        html2.index("<details"), "老页正文不塞进折叠区")

        # ③ 正文空 = 结论还没重编成功：必须给可见说明，不白屏、不藏进 details
        rel3 = self.mk_page("blank", "")
        html3 = self.get_page(rel3).content.decode("utf-8")
        txt3 = plain(html3)
        self.assertIn("结论还没生成", txt3)
        self.assertIn("当前没有可读的结论", txt3)
        self.assertLess(html3.index("结论还没生成"), html3.index("<details"),
                        "空结论说明不该藏进折叠区")

    def test_backlink_panel_renders_quote_and_link(self):
        rel = self.mk_page("alpha", TWO_LAYER)
        beta = self.mk_page("beta", "## 通顺话\n\n对照 [[alpha|甲页]] 才完整。\n")
        self.reindex()
        html = self.get_page(rel).content.decode("utf-8")
        self.assertIn("谁引用了我", html)
        self.assertIn("对照 甲页 才完整。", plain(html))
        # 不猜 urlencode 对 `/` 的编码口径，只钉「反链页真的可点」。
        hrefs = re.findall(r'href="(/wiki/page\?p=[^"]+)"', html)
        self.assertTrue(any("beta.md" in h for h in hrefs), hrefs)

    def test_no_backlink_panel_when_there_is_none(self):
        rel = self.mk_page("alpha", TWO_LAYER)
        self.reindex()
        html = self.get_page(rel).content.decode("utf-8")
        self.assertNotIn("谁引用了我", html)

    def test_missing_context_shows_visible_hint(self):
        rel = self.mk_page("alpha", TWO_LAYER)
        beta = self.mk_page("beta", "## 通顺话\n\n原先写了 [[alpha]]。\n")
        self.reindex()
        (wstore.root() / beta).write_text(
            PAGE.format(title="beta", body="## 通顺话\n\n后来把链接删了。\n"),
            encoding="utf-8")
        html = self.get_page(rel).content.decode("utf-8")
        self.assertIn("找不到引用句", plain(html))

    def test_view_really_calls_backlink_contexts(self):
        """桩真被用上的反证：不打在被调用点就等于没测。"""
        rel = self.mk_page("alpha", TWO_LAYER)
        fake = [{"page": "wiki/ai/probe.md", "title": "探针页",
                 "quotes": ["探针引用句"], "more": 0, "missing": False}]
        with patch.object(wstore, "backlink_contexts", return_value=fake) as p:
            html = self.get_page(rel).content.decode("utf-8")
        p.assert_called_once()
        self.assertIn("探针引用句", plain(html))

    def test_existing_anchors_still_present(self):
        rel = self.mk_page("alpha", TWO_LAYER)
        html = self.get_page(rel).content.decode("utf-8")
        self.assertIn('href="/wiki?src=', html)
        self.assertIn("拉入审批编辑", html)
        self.assertIn("提交删除请求", html)
        self.assertIn("frontmatter 原文", html)

    def test_traversal_still_404(self):
        self.assertEqual(
            self.get_page("../../Meta/ontology.md").status_code, 404)
        self.assertEqual(self.get_page("wiki/x/none.md").status_code, 404)

    def test_visible_text_has_no_template_comments(self):
        rel = self.mk_page("alpha", TWO_LAYER)
        txt = plain(self.get_page(rel).content.decode("utf-8"))
        for junk in ("{%", "endcomment", "body_html|safe", "POLISH_HEAD"):
            self.assertNotIn(junk, txt)
