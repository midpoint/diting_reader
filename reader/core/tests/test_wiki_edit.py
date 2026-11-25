"""草稿编辑（阶段 2.3b，2026-11-02）：润色正文，不动 frontmatter。

这个文件盯两件事：
  1. **save_draft 的行为**：正文替换、frontmatter 保留、log 记账、slug 安全校验；
  2. **视图层**：draft_preview 返回渲染 HTML、draft_save 写文件并返回片段。

★ 编辑语义和入库页编辑（2.3c）不同：草稿还没入 wiki，怎么改都不影响反向映射，
  所以 save_draft 只写文件 + 记 log，不触发 sources.json 重建。
"""
import shutil
import tempfile

from django.test import Client, TestCase, override_settings

from core import wiki as wstore


class SaveDraftBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-wiki-edit-")
        cls.ov = override_settings(WIKI_DIR=cls.tmp)
        cls.ov.enable()

    @classmethod
    def tearDownClass(cls):
        cls.ov.disable()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        # ★ 每个用例都要空的 wiki 目录：草稿是磁盘文件，不会随事务回滚
        shutil.rmtree(self.tmp, ignore_errors=True)
        wstore.ensure_dirs()

    def write_draft(self, slug, body, title="测试草稿", domain="invest"):
        """写一份草稿到 .drafts/，返回完整文件内容。"""
        text = f"""---
title: {title}
domain: {domain}
sources: [entry:1234]
updated: 2026-11-02
confidence: medium
---

{body}
"""
        (wstore.root() / ".drafts" / f"{slug}.md").write_text(
            text, encoding="utf-8")
        return text

    def read_draft(self, slug):
        return (wstore.root() / ".drafts" / f"{slug}.md").read_text(encoding="utf-8")


class SaveDraftTest(SaveDraftBase):
    """save_draft：正文替换 + frontmatter 保留 + log 记账。"""

    def test_saves_body_and_preserves_frontmatter(self):
        self.write_draft("test-draft", "原始正文")
        ok, payload = wstore.save_draft("test-draft", "新正文")
        self.assertTrue(ok, payload)
        self.assertEqual(payload["chars"], 3)
        self.assertEqual(payload["slug"], "test-draft")

        text = self.read_draft("test-draft")
        fm, body = wstore.parse_front(text)
        self.assertEqual(fm["title"], "测试草稿")
        self.assertEqual(fm["domain"], "invest")
        self.assertIn("新正文", body)
        self.assertNotIn("原始正文", body)

    def test_logs_edit_draft_action(self):
        self.write_draft("test-draft", "正文")
        wstore.save_draft("test-draft", "新正文")

        log_rows = wstore.read_log(limit=10)
        edit_rows = [r for r in log_rows if r["action"] == "edit-draft"]
        self.assertEqual(len(edit_rows), 1)
        self.assertIn(".drafts/test-draft.md", edit_rows[0]["target"])

    def test_rejects_unsafe_slug(self):
        self.write_draft("safe-draft", "正文")
        ok, err = wstore.save_draft("../escape", "新正文")
        self.assertFalse(ok)
        self.assertIn("非法", err)

    def test_rejects_nonexistent_draft(self):
        ok, err = wstore.save_draft("no-such-draft", "正文")
        self.assertFalse(ok)
        self.assertIn("不存在", err)

    def test_handles_empty_body(self):
        self.write_draft("test-draft", "原始正文")
        ok, payload = wstore.save_draft("test-draft", "")
        self.assertTrue(ok, payload)
        self.assertEqual(payload["chars"], 0)

        text = self.read_draft("test-draft")
        fm, body = wstore.parse_front(text)
        self.assertEqual(body.strip(), "")


class DraftPreviewViewTest(SaveDraftBase):
    """draft_preview 视图：返回渲染后的 HTML 片段。"""

    def test_returns_rendered_html(self):
        client = Client()
        resp = client.post("/wiki/draft/preview", {"body": "**粗体**"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("<strong>粗体</strong>", resp.content.decode())

    def test_handles_empty_body(self):
        client = Client()
        resp = client.post("/wiki/draft/preview", {"body": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content.decode(), "")


class DraftSaveViewTest(SaveDraftBase):
    """draft_save 视图：写文件 + 返回更新后的片段。"""

    def test_saves_and_returns_fragment(self):
        self.write_draft("test-draft", "原始正文")
        client = Client()
        resp = client.post("/wiki/draft/save", {
            "slug": "test-draft",
            "body": "新正文",
        })
        self.assertEqual(resp.status_code, 200)
        # ★ 返回的是 #wiki-body 片段（htmx outerHTML 替换）
        content = resp.content.decode("utf-8")
        self.assertIn('id="wiki-body"', content)

        text = self.read_draft("test-draft")
        fm, body = wstore.parse_front(text)
        self.assertIn("新正文", body)
        self.assertNotIn("原始正文", body)

    def test_rejects_unsafe_slug(self):
        self.write_draft("safe-draft", "正文")
        client = Client()
        resp = client.post("/wiki/draft/save", {
            "slug": "../escape",
            "body": "新正文",
        })
        self.assertEqual(resp.status_code, 200)
        # ★ 错误通过 toast（HX-Trigger）显示，检查响应头
        self.assertIn("HX-Trigger", resp.headers)
        import json
        trigger = json.loads(resp.headers["HX-Trigger"])
        self.assertIn("非法", trigger["wikiFlash"]["msg"])

    def test_rejects_nonexistent_draft(self):
        client = Client()
        resp = client.post("/wiki/draft/save", {
            "slug": "no-such-draft",
            "body": "正文",
        })
        self.assertEqual(resp.status_code, 200)
        # ★ 错误通过 toast（HX-Trigger）显示
        self.assertIn("HX-Trigger", resp.headers)
        import json
        trigger = json.loads(resp.headers["HX-Trigger"])
        self.assertIn("不存在", trigger["wikiFlash"]["msg"])
