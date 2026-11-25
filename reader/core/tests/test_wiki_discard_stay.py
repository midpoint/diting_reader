"""2026-11-13：退稿不关浮层 + 找相关页不弹二次确认 —— 界面契约判据。

单测钉「响应里有几个面板、选中指向哪份草稿、模板属性长什么样」；
「点一下之后浮层真的还在 / 确认框真的没弹」由真浏览器 e2e 验。
★ 每条结构断言尽量配一条可见文字或属性断言，别让空集合判据假绿。
"""
import json
import re

from django.template.loader import get_template, render_to_string
from django.urls import reverse

from apps.wiki import views as wv
from core import wiki as wstore
from core.tests.test_wiki_ui_step11 import GUARD_WORD, UiBase, plain, write_draft


def _form_tag(src, marker):
    """切出某个 <form ...> 开标签：判据只看属性，别被注释里的字样骗。"""
    m = re.search(r"<form[^>]*" + re.escape(marker) + r"[^>]*>", src, re.S)
    return m.group(0) if m else None


def _tag_with_id(html, pid):
    m = re.search(r'<[^>]*id="%s"[^>]*>' % re.escape(pid), html)
    return m.group(0) if m else None


class TestDiscardKeepsOverlay(UiBase):
    """退稿成功后浮层必须原地留着，四个面板的选中/计数一起归真。"""

    def _discard(self, slug, extra=None):
        data = {"from": "approval", "slug": slug, "reason": "不要这版"}
        data.update(extra or {})
        return self.client.post(reverse("wiki_discard"), data)

    def test_four_panels_refreshed_and_selection_follows_next_draft(self):
        write_draft("one", [])
        write_draft("two", [])
        resp = self._discard("one")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("HX-Refresh", resp, "整页刷新 = 把浮层一起关掉")
        html = resp.content.decode()

        # 主响应是 ② 工作区；另外三个片段必须各自带 hx-swap-oob。
        main = _tag_with_id(html, "approval-workarea")
        self.assertIsNotNone(main)
        self.assertNotIn("hx-swap-oob", main, "主响应走 hx-target，不该自己 OOB")
        for pid in ("approval-list", "approval-head", "approval-chat"):
            tag = _tag_with_id(html, pid)
            self.assertIsNotNone(tag, "缺 %s 片段" % pid)
            self.assertIn('hx-swap-oob="true"', tag, "%s 没带 OOB 标记" % pid)

        # ③ 对话区的隐藏选中必须切到剩下的那份，否则接着发话会打到已归档草稿。
        self.assertIn('name="selected_slug" value="two"', html)
        self.assertNotIn('name="selected_slug" value="one"', html)
        self.assertIn("审批浮层 · 1 份草稿", plain(html))
        # HX-Trigger 是 json.dumps（中文被 ensure_ascii 转义），必须解析后判。
        flash = json.loads(resp.get("HX-Trigger", "{}"))
        self.assertIn("已退稿", flash.get("wikiFlash", {}).get("msg", ""),
                      "原地不刷新也要有可见回执")

    def test_last_draft_discarded_shows_empty_state_but_overlay_stays(self):
        write_draft("only", [])
        resp = self._discard("only")
        html = resp.content.decode()
        self.assertNotIn("HX-Refresh", resp)
        self.assertIsNotNone(_tag_with_id(html, "approval-workarea"))
        self.assertIn("没有可预览的草稿", plain(html))
        self.assertNotIn('name="selected_slug"', html, "没有草稿就不该留隐藏选中")
        self.assertIn("审批浮层 · 0 份草稿", plain(html))

    def test_failure_branch_never_refreshes_either(self):
        resp = self._discard("no-such")
        self.assertNotIn("HX-Refresh", resp)
        self.assertIn("退稿未生效", plain(resp.content.decode()))

    def test_compile_guard_never_refreshes_either(self):
        write_draft("one", [])
        self.running()
        resp = self._discard("one")
        self.assertNotIn("HX-Refresh", resp)
        self.assertIn(GUARD_WORD, plain(resp.content.decode()))

    def test_home_legacy_path_unchanged(self):
        """不带 from 的首页老路径仍走 _fragment，不能被这次改动带坏。"""
        write_draft("one", [])
        resp = self.client.post(reverse("wiki_discard"),
                                {"slug": "one", "reason": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("HX-Refresh", resp)
        self.assertFalse((wstore.root() / ".drafts" / "one.md").exists())


class TestRelatedButtonNoConfirm(UiBase):
    """查相关页是只读推荐 + 一次可重试模型调用，不是危险操作。"""

    def test_template_related_form_has_no_confirm_neighbours_keep_it(self):
        src = get_template("wiki/_approval_workarea.html").template.source
        rel = _form_tag(src, 'hx-post="/wiki/approval/related"')
        dis = _form_tag(src, 'class="inline-form approval-discard"')
        mrg = _form_tag(src, 'hx-post="/wiki/approval/merge-into"')
        self.assertIsNotNone(rel, "表单被删了，别让空集合判据假绿")
        self.assertIsNotNone(dis)
        self.assertIsNotNone(mrg)
        self.assertNotIn("hx-confirm", rel, "找相关页不该弹二次确认")
        self.assertIn("hx-confirm", dis, "退稿归档仍要确认")
        self.assertIn("hx-confirm", mrg, "改为并入会覆盖已入库页，仍要确认")

    def test_rendered_workarea_has_no_confirm_on_related_button(self):
        write_draft("one", [])
        html = render_to_string(
            "wiki/_approval_workarea.html", wv._approval_workarea_ctx("one"))
        rel = _form_tag(html, 'hx-post="/wiki/approval/related"')
        self.assertIsNotNone(rel, "ordinary 草稿必须真渲染出相关页表单")
        self.assertNotIn("hx-confirm", rel)
        self.assertIn('id="related-refresh"', html)

    def test_chat_oob_flag_is_opt_in(self):
        """③ 只有显式传 oob 才带 hx-swap-oob，首次壳渲染逐字节不变。"""
        ctx = wv._approval_chat_ctx(selected_slug="one")
        normal = render_to_string("wiki/_approval_chat.html", ctx)
        oob = render_to_string("wiki/_approval_chat.html", dict(ctx, oob=True))
        self.assertNotIn("hx-swap-oob", normal)
        self.assertIn('hx-swap-oob="true"', oob)
