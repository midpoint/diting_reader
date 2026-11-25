"""对话消息上的划线/批注（2026-10-31 新增）。

为什么值得单独一组：
  · 这里最容易出的错是**记到别人头上**：选区在浮窗里，但页面默认的可标记区域是
    文章正文。一旦来源解析错，划线会写进 `entry/report`，然后文章渲染器去找一句
    根本不在正文里的话 —— 全部落位失败，用户看到的是「划了但没高亮」。
  · 第二容易的是**来源校验放水**：`chat` 若只校验「id 是数字」，前端就能造一个
    指向不存在消息的引用，页面上永远看不见，也永远删不掉。
"""
from django.test import TestCase

from core import marks as marks_mod
from core.models import ChatMessage, Conversation, Highlight


def make_msg(body="最要紧的是成本结构：推理价格下降的速度快于需求增长的速度。",
             role="assistant"):
    conv, _ = Conversation.objects.get_or_create(source_type="report",
                                                 source_id="daily-2026-10-31")
    return conv.messages.create(role=role, body=body)


class ChatSourceTest(TestCase):
    def test_existing_message_is_allowed_and_normalised(self):
        m = make_msg()
        t, sid, err = marks_mod.validate_source("chat", "%04d" % m.id)
        self.assertEqual((t, err), ("chat", ""))
        self.assertEqual(sid, str(m.id))          # 去前导零，与 entry 同一套规范

    def test_missing_message_is_rejected(self):
        # ★ 判据是「消息真的在库里」，不是「id 长得像数字」
        _, _, err = marks_mod.validate_source("chat", "99999999")
        self.assertIn("不存在", err)

    def test_non_numeric_rejected(self):
        _, _, err = marks_mod.validate_source("chat", "abc")
        self.assertIn("正整数", err)

    def test_web_still_reserved(self):
        # 剪藏对象阶段 4 才出现，别顺手放行
        _, _, err = marks_mod.validate_source("web", "1")
        self.assertIn("尚未启用", err)


class ChatMarkRenderTest(TestCase):
    def test_article_chat_message_keeps_mark_anchors(self):
        """审批对话隐藏痕迹不影响文章对话：默认渲染仍带锚点与容器。"""
        from django.template.loader import render_to_string
        from apps.wiki.views import _wc_msg_ctx
        m = make_msg()
        html = render_to_string("wiki/_wc_msg.html", _wc_msg_ctx(m))
        self.assertIn('data-mark-type="chat"', html)
        self.assertIn("data-mark-body", html)
        self.assertIn('id="marks-chat-%s"' % m.id, html)

    def test_mark_is_rendered_into_the_message(self):
        m = make_msg()
        ok, _ = marks_mod.add_highlight("chat", str(m.id), "成本结构",
                                        anchor='{"text":"成本结构","prefix":"最要紧的是",'
                                               '"suffix":"：推理价格","offset":5,"nth":0}')
        self.assertTrue(ok)
        from apps.wiki.views import _chat_ctx
        ctx = _chat_ctx("report", "daily-2026-10-31")
        html = ctx["messages"][0]["m"]["html"]
        self.assertIn("<mark", html)
        self.assertIn("成本结构", html)
        self.assertNotIn("1 条未在正文中定位", html)   # 落位了就别再进兜底列表

    def test_article_marks_do_not_leak_into_chat(self):
        # 同一段文字在文章里也有一份划线：聊天消息不该把文章的划线也贴上来
        m = make_msg()
        marks_mod.add_highlight("report", "daily-2026-10-31", "成本结构",
                                anchor='{"text":"成本结构","offset":0,"nth":0}')
        from apps.wiki.views import _chat_ctx
        html = _chat_ctx("report", "daily-2026-10-31")["messages"][0]["m"]["html"]
        self.assertNotIn("<mark", html)


class ChatMarkEndpointTest(TestCase):
    def test_highlight_on_chat_message_swaps_only_that_message(self):
        m = make_msg()
        res = self.client.post("/wiki/highlight", {
            "source_type": "chat", "source_id": str(m.id),
            "quoted_text": "成本结构",
            "anchor": '{"text":"成本结构","prefix":"最要紧的是","suffix":"：推理价格",'
                      '"offset":5,"nth":0}',
        })
        html = res.content.decode()
        self.assertEqual(res.status_code, 200)
        # ★ 只换这一条消息：主响应不换任何东西，靠 OOB 元素替换 #wc-msg-<id>
        self.assertEqual(res["HX-Reswap"], "none")
        self.assertIn('id="wc-msg-%d"' % m.id, html)
        self.assertIn('hx-swap-oob="true"', html)
        self.assertIn("<mark", html)
        # ★ 不再渲染「我 / AI」身份标签（2026-11-19）：右对齐+气泡底色已自明，
        #   wc-who 白占一行。这条真渲染断言防止模板被人加回标签。
        self.assertNotIn("wc-who", html)
        self.assertIn('class="wc-msg wc-assistant"', html)   # 身份改由 class 承载
        self.assertTrue(Highlight.objects.filter(source_type="chat",
                                                  source_id=str(m.id)).exists())
        # 不能顺手在文章上留一条
        self.assertFalse(Highlight.objects.filter(source_type="report").exists())

    def test_deleted_message_reports_instead_of_silently_passing(self):
        m = make_msg()
        mid = m.id
        m.delete()
        res = self.client.post("/wiki/highlight", {
            "source_type": "chat", "source_id": str(mid), "quoted_text": "x"})
        self.assertIn("不存在", res.content.decode())
        self.assertEqual(ChatMessage.objects.count(), 0)

    def test_chat_mark_edit_keeps_message_intact(self):
        m = make_msg()
        ok, payload = marks_mod.add_highlight("chat", str(m.id), "成本结构")
        hid = payload["id"] if isinstance(payload, dict) else payload
        res = self.client.post("/wiki/mark/edit", {
            "source_type": "chat", "source_id": str(m.id),
            "kind": "highlight", "id": hid, "body": "这里是我的备注"})
        self.assertIn("这里是我的备注", res.content.decode())
        self.assertEqual(Highlight.objects.get(pk=hid).note, "这里是我的备注")
