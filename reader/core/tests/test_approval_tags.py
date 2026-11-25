"""审批页就地改标签（2026-11-16）。

背景：标签从第十三步起就写在 frontmatter 里，但**只有模型能写** —— 人在审批页
看见标签（2026-11-16 上午刚让它可见）却改不了，存量 32 页只能靠对话说「把标签
改成…」。用户要求：「审核页面应该有编辑 tags 的地方，参考微信好友 tag ——
输入 tag 的时候要能选择已有的 tag」。

这一组钉五件事：
  1. 收敛判据只有一份（`clean_tags`）：逗号/顿号分号都算分隔符、去重、超限**拒收**；
  2. 两条写盘路径（`set("tags")` 与 `set("frontmatter")` 的 fields）都过同一道校验
     —— 只拦一条等于留了个后门；
  3. 审批页真的渲染出编辑器，且词表来自**已入库页**（草稿里的标签不算）；
  4. POST 只写 `.drafts/`（绝不碰 `wiki/`），拒收时**磁盘一个字节都不变**，
     回包带可见的「未保存：…」；
  5. 删页提案不显示编辑器（页都要没了，改标签没有去处）。
"""
import json
import shutil
import tempfile

from django.test import Client, TestCase, override_settings

from core import wiki_agent as wa
from core import wiki as wstore

PAGE = """---
title: {title}
domain: ai
sources: [entry:1234]
updated: 2026-11-01
confidence: medium
tags: [{tags}]
candidates: []
---

正文一句话。
"""

DRAFT = """---
title: 草稿页
domain: ai
sources: [entry:9001]
updated: 2026-11-16
confidence: medium
tags: [{tags}]
candidates: []
---

草稿正文。
"""


class CleanTagsTest(TestCase):
    """纯函数：收敛与拒收（与视图无关，先把口径钉死）。"""

    def test_splits_on_all_separators_and_dedupes(self):
        got, err = wa.clean_tags("现金流, 估值、供需；现金流")
        self.assertEqual(err, "")
        self.assertEqual(got, ["现金流", "估值", "供需"])

    def test_list_input_keeps_order(self):
        got, err = wa.clean_tags(["b", "a", "b", " "])
        self.assertEqual((got, err), (["b", "a"], ""))

    def test_empty_list_is_legal(self):
        """`tags: []` 是合法的无标签页（FM_KEY_ONLY），不能当错误。"""
        self.assertEqual(wa.clean_tags([]), ([], ""))

    def test_too_many_rejected_not_truncated(self):
        """第 9 个标签必须**拒收**：静默截断会让人以为它生效了。"""
        got, err = wa.clean_tags([str(i) for i in range(9)])
        self.assertIsNone(got, "超限应该拒收，实际被截断成 %r" % (got,))
        self.assertIn("最多", err)

    def test_too_long_rejected(self):
        got, err = wa.clean_tags(["一" * (wa.TAG_MAX_CHARS + 1)])
        self.assertIsNone(got)
        self.assertIn("太长", err)

    def test_bracket_rejected(self):
        """方括号会写坏 `tags: [a, b]` 的内联列表格式，读回来是另一个词。"""
        got, err = wa.clean_tags(["现[金]流"])
        self.assertIsNone(got)
        self.assertIn("方括号", err)


class TagSaveCoreTest(TestCase):
    """写盘路径：两条入口都过 clean_tags。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-approval-tags-")
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
        (wstore.root() / ".drafts").mkdir(parents=True, exist_ok=True)
        self.draft = wstore.root() / ".drafts" / "demo.md"
        self.draft.write_text(DRAFT.format(tags="旧标签"), encoding="utf-8")

    def fm_tags(self):
        fm, _ = wstore.parse_front(self.draft.read_text(encoding="utf-8"))
        return fm.get("tags")

    def test_set_tags_writes_and_normalizes(self):
        ok, payload = wa.set("tags", {"slug": "demo", "tags": "现金流, 估值"})
        self.assertTrue(ok, payload)
        self.assertEqual(self.fm_tags(), ["现金流", "估值"])
        self.assertEqual(payload["value"], ["现金流", "估值"])

    def test_set_tags_rejects_nine(self):
        ok, err = wa.set("tags", {"slug": "demo", "tags": [str(i) for i in range(9)]})
        self.assertFalse(ok)
        self.assertEqual(self.fm_tags(), ["旧标签"], "拒收时磁盘不该被改")

    def test_frontmatter_action_also_validated(self):
        """第二条写 tags 的路（fields）必须与第一条同口径，否则就是个后门。"""
        ok, err = wa.set("frontmatter",
                         {"slug": "demo", "fields": {"tags": ["a,b", "c"]}})
        self.assertTrue(ok, err)
        self.assertEqual(self.fm_tags(), ["a", "b", "c"])

    def test_frontmatter_action_rejects_nine(self):
        ok, err = wa.set("frontmatter",
                         {"slug": "demo", "fields": {"tags": [str(i) for i in range(9)]}})
        self.assertFalse(ok)
        self.assertEqual(self.fm_tags(), ["旧标签"])


class ApprovalTagViewTest(TestCase):
    """审批页：渲染 + POST + 拒收 + 删页提案。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-approval-tags-ui-")
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
        (wstore.root() / ".drafts").mkdir(parents=True, exist_ok=True)
        self.c = Client()
        self.write_page("wiki/ai/other.md", "别的页", "现金流, 估值")
        self.draft = self.write_draft("demo.md", "旧标签")

    def write_page(self, rel, title, tags):
        p = wstore.root() / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(PAGE.format(title=title, tags=tags), encoding="utf-8")
        return p

    def write_draft(self, name, tags, extra=""):
        p = wstore.root() / ".drafts" / name
        p.write_text(DRAFT.format(tags=tags).replace("---\n\n草稿正文",
                                                     extra + "---\n\n草稿正文"),
                     encoding="utf-8")
        return p

    def draft_tags(self, path=None):
        fm, _ = wstore.parse_front((path or self.draft).read_text(encoding="utf-8"))
        return fm.get("tags")

    def test_editor_rendered_with_draft_tags(self):
        html = self.c.get("/wiki/approval").content.decode()
        self.assertIn('id="tag-editor"', html, "审批页没有标签编辑器")
        self.assertIn('data-tag="旧标签"', html, "当前草稿的标签没渲染成 chip")

    def test_vocab_comes_from_pages_not_drafts(self):
        """词表只数已入库页：草稿里的标签还没被人点头，不能当既定用词。"""
        self.write_draft("extra.md", "只在草稿里")
        html = self.c.get("/wiki/approval").content.decode()
        raw = html.split('data-vocab="', 1)[1].split('"', 1)[0]
        vocab = json.loads(raw.replace("&quot;", '"'))
        tags = [v["tag"] for v in vocab]
        self.assertIn("现金流", tags)
        self.assertNotIn("只在草稿里", tags)
        self.assertEqual(vocab[0]["n"] >= vocab[-1]["n"], True, "词表没按次数降序")

    def test_post_adds_tag(self):
        r = self.c.post("/wiki/approval/tags",
                        {"slug": "demo", "tags": ["旧标签", "现金流"]})
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn('data-tag="现金流"', body, "回包里没有新标签")
        self.assertNotIn("未保存", body)
        self.assertEqual(self.draft_tags(), ["旧标签", "现金流"])

    def test_post_removes_last_tag(self):
        r = self.c.post("/wiki/approval/tags", {"slug": "demo"})
        self.assertIn("还没有标签", r.content.decode(), "删空后没有说出口")
        self.assertEqual(self.draft_tags(), [])

    def test_post_rejects_nine_and_keeps_disk(self):
        """拒收：磁盘不动，回包显示刚提交的那批 + 「未保存」，不是静默吞掉。"""
        want = ["t%d" % i for i in range(9)]
        r = self.c.post("/wiki/approval/tags", {"slug": "demo", "tags": want})
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("未保存", body, "拒收没有可见提示")
        self.assertIn("最多", body)
        self.assertIn('data-tag="t8"', body, "被拒的那批没回显，人不知道该删哪个")
        self.assertEqual(self.draft_tags(), ["旧标签"], "拒收时磁盘被改了")

    def test_post_unknown_draft_404(self):
        r = self.c.post("/wiki/approval/tags",
                        {"slug": "nope", "tags": ["x"]})
        self.assertEqual(r.status_code, 404,
                         "草稿不存在时回 200 会让前端渲染成「标签被清空」")

    def test_post_never_writes_wiki_dir(self):
        before = {p: p.read_bytes() for p in (wstore.root() / "wiki").rglob("*.md")}
        self.c.post("/wiki/approval/tags", {"slug": "demo", "tags": ["现金流"]})
        after = {p: p.read_bytes() for p in (wstore.root() / "wiki").rglob("*.md")}
        self.assertEqual(before, after, "改标签动了 wiki/ —— 入库只能走审批")

    def test_delete_draft_has_no_editor(self):
        """删页提案不显示编辑器：页要没了，改标签没有去处。"""
        self.draft.unlink()
        self.write_draft("gone.md", "无意义", extra="edits: gone\n")
        html = self.c.get("/wiki/approval").content.decode()
        self.assertIn('id="approval-workarea"', html)
        self.assertNotIn('id="tag-editor"', html)


class TagPoolReuseTest(TestCase):
    """词表只有一份：编译期 `_tag_pool` 与审批页下拉同源。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-tag-pool-")
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

    def test_counts_and_order(self):
        for name, tags in (("a", "现金流, 估值"), ("b", "现金流")):
            p = wstore.root() / "wiki" / "ai" / (name + ".md")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(PAGE.format(title=name, tags=tags), encoding="utf-8")
        vocab = wstore.tag_counts()
        self.assertEqual(vocab[0], {"tag": "现金流", "n": 2})
        self.assertEqual([v["tag"] for v in vocab], ["现金流", "估值"])

    def test_ingest_pool_uses_same_source(self):
        from core import ingest
        for name, tags in (("a", "长期"), ("b", "长期, 现金流")):
            p = wstore.root() / "wiki" / "ai" / (name + ".md")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(PAGE.format(title=name, tags=tags), encoding="utf-8")
        pages = wstore.pages()
        self.assertEqual(ingest._tag_pool(pages), ["长期", "现金流"])
        self.assertEqual(ingest._tag_pool(pages, limit=1), ["长期"])


class TagEditCommitFlowTest(TestCase):
    """存量页「过一遍」的完整动作序列：拉入审批 → 就地改标签 → 提交入库 → 页上是新标签。

    ★ 这条链路正是用户说的「支持 tag 编辑的话，我就可以把存量 wiki 过一遍了」。
      少了最后一环，前面全对也只是改了草稿 —— 页上仍是旧标签，而人以为改完了。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-tag-flow-")
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
        p = wstore.root() / "wiki" / "ai" / "tagged.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(PAGE.format(title="待改标签的页", tags="旧甲, 旧乙")
                     .replace("candidates: []", "candidates: [42]"),
                     encoding="utf-8")

    def test_pull_edit_tags_commit(self):
        ok, payload = wstore.pull_page("tagged")
        self.assertTrue(ok, payload)
        # 拉出来的副本带着原页标签（人是在这个基础上改，不是从空白造）
        fm, _ = wstore.parse_front(
            (wstore.root() / ".drafts" / "tagged.md").read_text(encoding="utf-8"))
        self.assertEqual(fm.get("tags"), ["旧甲", "旧乙"])

        ok, payload = wa.set("tags", {"slug": "tagged",
                                      "tags": ["现金流", "估值"]})
        self.assertTrue(ok, payload)
        ok, info = wstore.approve("tagged")
        self.assertTrue(ok, info)

        page = wstore.root() / "wiki" / "ai" / "tagged.md"
        fm, body = wstore.parse_front(page.read_text(encoding="utf-8"))
        self.assertEqual(fm.get("tags"), ["现金流", "估值"], "入库后页上还是旧标签")
        self.assertIn("正文一句话", body, "入库把正文弄丢了")
        self.assertFalse((wstore.root() / ".drafts" / "tagged.md").exists(),
                         "入库后草稿还留着（会重复提交）")
