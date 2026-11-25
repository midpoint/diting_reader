"""入库页编辑（阶段 2.3c，2026-11-02）：改判断 / 更新结论。

这个文件盯三件事：
  1. **save_page 的行为**：正文替换、`updated` 字段更新、sources.json 重建、log 记账；
  2. **视图层**：page_preview 返回渲染 HTML、page_save 写文件并跳回详情页；
  3. **与草稿编辑的语义差异**：入库页编辑会触发 sources.json 重建（草稿不会）。

★ 设计文档 §5.1：草稿编辑是「润色」（还没入 wiki，怎么改都不影响反向映射），
  入库页编辑是「改判断」（已沉淀进知识体系，updated 和 sources 都要跟着变）。
"""
import json
import shutil
import tempfile

from django.test import Client, TestCase, override_settings

from core import wiki as wstore


class SavePageBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-wiki-page-edit-")
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

    def write_page(self, rel_path, body, title="测试页", domain="invest",
                   updated="2026-11-01", sources=None):
        """写一份入库页到 wiki/<domain>/，返回完整文件内容。"""
        sources = sources or ["entry:1234"]
        text = f"""---
title: {title}
domain: {domain}
sources: [{', '.join(sources)}]
updated: {updated}
confidence: medium
---

{body}
"""
        p = wstore.root() / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return text

    def read_page(self, rel_path):
        return (wstore.root() / rel_path).read_text(encoding="utf-8")


class SavePageTest(SavePageBase):
    """save_page：正文替换 + updated 更新 + sources.json 重建 + log 记账。"""

    def test_saves_body_and_updates_updated_field(self):
        self.write_page("wiki/invest/test-page.md", "原始正文", updated="2026-11-01")
        ok, payload = wstore.save_page("wiki/invest/test-page.md", "新正文")
        self.assertTrue(ok, payload)
        self.assertEqual(payload["chars"], 3)
        self.assertEqual(payload["page"], "wiki/invest/test-page.md")

        text = self.read_page("wiki/invest/test-page.md")
        fm, body = wstore.parse_front(text)
        self.assertEqual(fm["title"], "测试页")
        self.assertIn("新正文", body)
        self.assertNotIn("原始正文", body)
        # ★ updated 应该被更新为**今天** —— 期望值必须现算，不许写死。
        #   2026-11-03 踩到：昨天写的 "2026-11-02" 今天一到点就红，而代码没改。
        #   这类"写死当前状态当期望"的测试，坏的时候看起来像产品坏了。
        from django.utils import timezone
        self.assertEqual(fm["updated"], timezone.localdate().isoformat())

    def test_rebuilds_sources_index(self):
        """入库页编辑后 sources.json 应该被重建。"""
        self.write_page("wiki/invest/test-page.md", "正文", sources=["entry:5678"])
        # 先确保 sources.json 存在
        wstore.rebuild_sources_index(mark_log=False)

        # 编辑页面
        wstore.save_page("wiki/invest/test-page.md", "新正文")

        # 检查 sources.json 包含这个来源
        sources_json = (wstore.root() / "meta" / "sources.json").read_text(encoding="utf-8")
        sources = json.loads(sources_json)
        self.assertIn("entry:5678", sources)
        self.assertIn("wiki/invest/test-page.md", sources["entry:5678"])

    def test_logs_edit_page_action(self):
        self.write_page("wiki/invest/test-page.md", "正文")
        wstore.save_page("wiki/invest/test-page.md", "新正文")

        log_rows = wstore.read_log(limit=10)
        edit_rows = [r for r in log_rows if r["action"] == "edit-page"]
        self.assertEqual(len(edit_rows), 1)
        self.assertIn("wiki/invest/test-page.md", edit_rows[0]["target"])

    def test_rejects_unsafe_path(self):
        self.write_page("wiki/invest/safe-page.md", "正文")
        ok, err = wstore.save_page("../escape.md", "新正文")
        self.assertFalse(ok)
        self.assertIn("非法", err)

    def test_rejects_nonexistent_page(self):
        ok, err = wstore.save_page("wiki/invest/no-such-page.md", "正文")
        self.assertFalse(ok)
        self.assertIn("不存在", err)

    def test_handles_empty_body(self):
        self.write_page("wiki/invest/test-page.md", "原始正文")
        ok, payload = wstore.save_page("wiki/invest/test-page.md", "")
        self.assertTrue(ok, payload)
        self.assertEqual(payload["chars"], 0)

        text = self.read_page("wiki/invest/test-page.md")
        fm, body = wstore.parse_front(text)
        self.assertEqual(body.strip(), "")


class PagePreviewViewTest(SavePageBase):
    """page_preview 视图：返回渲染后的 HTML 片段。"""

    def test_returns_rendered_html(self):
        client = Client()
        resp = client.post("/wiki/page/preview", {"body": "**粗体**"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("<strong>粗体</strong>", resp.content.decode())

    def test_handles_empty_body(self):
        client = Client()
        resp = client.post("/wiki/page/preview", {"body": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content.decode(), "")


class PageSaveViewTest(SavePageBase):
    """page_save 视图：写文件 + 跳回详情页。"""

    def test_saves_and_redirects(self):
        self.write_page("wiki/invest/test-page.md", "原始正文")
        client = Client()
        resp = client.post("/wiki/page/save", {
            "page": "wiki/invest/test-page.md",
            "body": "新正文",
        })
        # ★ 保存后跳回详情页（302 重定向）
        self.assertEqual(resp.status_code, 302)
        self.assertIn("p=wiki/invest/test-page.md", resp.url)

        text = self.read_page("wiki/invest/test-page.md")
        fm, body = wstore.parse_front(text)
        self.assertIn("新正文", body)
        self.assertNotIn("原始正文", body)

    def test_rejects_unsafe_path(self):
        self.write_page("wiki/invest/safe-page.md", "正文")
        client = Client()
        resp = client.post("/wiki/page/save", {
            "page": "../escape.md",
            "body": "新正文",
        })
        # ★ 即使失败也跳回（带错误 toast）
        self.assertEqual(resp.status_code, 302)

    def test_rejects_nonexistent_page(self):
        client = Client()
        resp = client.post("/wiki/page/save", {
            "page": "wiki/invest/no-such-page.md",
            "body": "正文",
        })
        self.assertEqual(resp.status_code, 302)
