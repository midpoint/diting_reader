"""内容块对话（阶段 2.1）：上下文组装、落库、失败留痕、预算超限提示、CSRF。

为什么这些断言值得存在：
  · 对话最容易悄悄坏的两处 —— **用户的问题没落库**（刷新就没了，用户以为白问）
    和 **材料没进去**（模型开始自由发挥，回答看着很顺但和文章无关）。
  · 预算超限必须**说人话**：wiki 是按钮驱动的，点了没反应比报错糟得多。
  · **CSRF**：Django 的测试客户端默认 `enforce_csrf_checks=False`，所以「页面上的
    form 忘了 {% csrf_token %}」能一路全绿上线，真实点击却 403（2026-10-31 实测
    撞到 `/wiki/chat/send` 403 "CSRF token missing"）。这一组必须显式开校验。
"""
import re
from unittest import mock

from django.test import Client, TestCase, override_settings

from core import conversation
from core import costreport as cr
from core import marks as marks_mod
from core.models import Annotation, ChatMessage, Conversation, Highlight
from core.textutil import to_plain

BLK = {"title": "测试文章", "text": "第一段正文。\n第二段正文。",
       "html": "<p>第一段正文。</p><p>第二段正文。</p>"}


def patch_blk(blk=BLK):
    return mock.patch("core.conversation.sourceblock.load", return_value=blk)


def patch_call(ret=("这是回答。", "")):
    """打桩带守卫的调用层（不花钱、不联网），返回 (text, err)。"""
    return mock.patch("core.conversation.citeguard._call", return_value=ret)


class MaterialTest(TestCase):
    def test_includes_title_body_marks(self):
        Highlight.objects.create(source_type="entry", source_id="12",
                                 quoted_text="被划的一句", note="我的备注")
        data = marks_mod.list_marks("entry", "12")
        out = conversation._material(BLK, data["highlights"], data["annotations"])
        self.assertIn("测试文章", out)
        self.assertIn("第一段正文", out)
        self.assertIn("被划的一句", out)
        self.assertIn("我的备注", out)

    @override_settings(WIKI_CHAT_ARTICLE_CHARS=500)
    def test_body_is_never_truncated(self):
        """★ 2026-11-15 用户报的 bug：长文总结不可用，因为正文超 6000 字被截断。

        这条断言原来是反的（`assertIn("超过 500 字")` + `assertNotIn("字"*600)`），
        等于把「后半篇丢掉」当成正确行为钉住。现在判据翻过来：材料里必须能读到
        正文的**最后一个字**。
        """
        long_blk = dict(BLK, text="字" * 900)
        out = conversation._material(long_blk, [], [])
        self.assertIn("字" * 900, out)          # 全文都在，不是只看前 500
        self.assertNotIn("超过 500 字", out)     # 也不许再出现「以上为前 N 字」的说明

    def test_source_line_in_material(self):
        # 2026-11-03：材料抬头必须写清「这条是谁发布的」。对话里问「他这话什么
        # 意思」时，模型同样需要知道作者，而不是靠订阅源名的先验知识猜。
        out = conversation._material(dict(BLK, feed_title="某源"), [], [])
        self.assertIn("来源：某源", out)

    def test_no_dangling_source_label(self):
        out = conversation._material(BLK, [], [])      # BLK 没有 feed_title
        self.assertNotIn("来源：", out)

    def test_highlight_is_labelled_as_attention_not_judgement(self):
        # ★ 用户 2026-10-31 纠正：划线只表示「我在意」，可能是重要事实，也可能是
        #   我认可的他人判断 —— **不是我的判断**。标签写错，模型就会替他表态。
        Highlight.objects.create(source_type="entry", source_id="12",
                                 quoted_text="被划的一句")
        Annotation.objects.create(source_type="entry", source_id="12",
                                  body="我觉得这个结论站不住")
        data = marks_mod.list_marks("entry", "12")
        out = conversation._material(BLK, data["highlights"], data["annotations"])
        self.assertIn("## 我划出的原文（1 条；这些是原文片段，只表示我在意，"
                      "不是我的结论）", out)
        self.assertIn("## 我的批注（1 条；我自己敲的字，是我的思考）", out)
        self.assertIn("我觉得这个结论站不住", out)

    def test_note_is_marked_as_my_own_words(self):
        # 划线的备注是我自己敲的 ⇒ 唯一能算「我的判断」的那一层
        Highlight.objects.create(source_type="entry", source_id="12",
                                 quoted_text="被划的一句", note="这条和上周的数据矛盾")
        data = marks_mod.list_marks("entry", "12")
        out = conversation._material(BLK, data["highlights"], data["annotations"])
        self.assertIn("└ 我的备注（我自己写的）：这条和上周的数据矛盾", out)

    def test_highlights_without_note_are_flagged_as_no_opinion(self):
        # 只划不写是最常见的情况：不点明「我没留下看法」，模型必然替我编一个
        Highlight.objects.create(source_type="entry", source_id="12",
                                 quoted_text="只划了一句")
        data = marks_mod.list_marks("entry", "12")
        out = conversation._material(BLK, data["highlights"], data["annotations"])
        self.assertIn("没留下看法", out)
        self.assertNotIn("我的备注", out)      # 不要留一个空的「备注：」

    def test_system_prompt_separates_attention_from_thinking(self):
        # 三层语义必须写进 prompt 本身，不能只写在注释里（注释模型看不到）
        self.assertIn("划线不是他的结论", conversation.SYSTEM)
        self.assertIn("只有【我的备注】【我的批注】是用户自己敲的字", conversation.SYSTEM)
        self.assertIn("没留下看法", conversation.SYSTEM)

    def test_blockquote_is_bounded_in_body(self):
        """回帖引用的提问，不能变成「作者说的话」。

        ★ 2026-11-03 用户第二次提这件事（第一次是 2026-11-02）：
          「回复内容的时候，避免从引用里提炼候选……引文内容较多，我更关心的是
          作者的回复而非引文。」上一轮只修了 wiki 提炼那条链路，对话这条
          一直在用 `blk["text"]` —— 而 text 已被 `to_plain()` 洗过，
          `<blockquote>` 早没了，模型看到的是博主和提问者混在一起的一串句子。
        ★ 喂真实形状（text = to_plain(html)）：把 blockquote 塞进 text 会让
          「切 text」的错实现也能过，那是假绿。
        """
        html = ("<p>回复@小明：嗯嗯，明天卖点 put。</p>"
                "<blockquote>@小明：谷歌出财报了，搜索业务不太行了。</blockquote>")
        blk = {"title": "测试文章", "html": html, "text": to_plain(html)}
        out = conversation._material(blk, [], [])
        self.assertIn("【引文】", out)
        self.assertIn("【/引文】", out)
        # 边界必须正好套在被引者那句上：博主的回复在边界外
        self.assertRegex(out, r"【引文】[^\n]*搜索业务不太行了[^\n]*【/引文】")
        before = out.split("【引文】")[0]
        self.assertIn("明天卖点 put", before, "博主自己的话必须落在【引文】外面")

    def test_plain_body_gets_no_quote_marker(self):
        """反证：没有引用块就一个字都不许加 —— 全包成引文等于告诉模型
        「作者什么都没说」，比不标更糟。"""
        out = conversation._material(BLK, [], [])
        self.assertNotIn("【引文】", out)

    def test_system_prompt_explains_quotes(self):
        """边界标了，还得说它是什么 —— 光有标记，模型只能猜。"""
        self.assertIn("【引文】", conversation.SYSTEM)
        self.assertIn("不是本文作者的说法", conversation.SYSTEM)
        # 用户的原话：引文长、作者只回一句时，关心的是作者的回应
        self.assertIn("以作者那句为主", conversation.SYSTEM)


class AskTest(TestCase):
    def test_success_stores_both_sides(self):
        with patch_blk(), patch_call() as call:
            res = conversation.ask("entry", "12", "这篇讲了什么？")
        self.assertTrue(res["ok"])
        self.assertEqual(res["reply"], "这是回答。")
        roles = list(ChatMessage.objects.order_by("id").values_list("role", flat=True))
        self.assertEqual(roles, ["user", "assistant"])
        kw = call.call_args.kwargs
        self.assertEqual(kw["pool"], cr.POOL_WIKI)
        self.assertEqual(call.call_args.args[0], "wiki_chat")

    def test_messages_carry_material_and_question(self):
        with patch_blk(), patch_call() as call:
            conversation.ask("entry", "12", "核心结论？")
        msgs = call.call_args.kwargs["messages"]
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("第一段正文", msgs[0]["content"])     # 素材在 system（稳定前缀）
        self.assertEqual(msgs[-1], {"role": "user", "content": "核心结论？"})

    def test_budget_stop_keeps_question_and_reports_reason(self):
        # ★ 超限时：用户的问题必须留下（不能让人白打一遍），原因要能直接显示
        with patch_blk(), patch_call(("", "知识库池今日预算（¥1）已用尽")):
            res = conversation.ask("entry", "12", "还想问一句")
        self.assertFalse(res["ok"])
        self.assertIn("预算", res["err"])
        self.assertEqual(ChatMessage.objects.filter(role="user").count(), 1)
        self.assertEqual(ChatMessage.objects.filter(role="assistant").count(), 0)

    def test_empty_question_rejected(self):
        with patch_call() as call:
            res = conversation.ask("entry", "12", "   ")
        self.assertFalse(res["ok"])
        call.assert_not_called()
        self.assertEqual(ChatMessage.objects.count(), 0)

    def test_invalid_source_rejected(self):
        res = conversation.ask("entry", "abc", "问")
        self.assertFalse(res["ok"])
        self.assertIn("正整数", res["err"])

    def test_missing_body_refuses_to_answer(self):
        # 没有材料就别答：那种「对话」只是模型自由发挥
        with mock.patch("core.conversation.sourceblock.load", return_value=None), \
                patch_call() as call:
            res = conversation.ask("entry", "12", "问")
        self.assertFalse(res["ok"])
        self.assertIn("读不到", res["err"])
        call.assert_not_called()

    def test_history_is_passed_on_second_turn(self):
        with patch_blk(), patch_call(("第一答", "")):
            conversation.ask("entry", "12", "第一问")
        with patch_blk(), patch_call(("第二答", "")) as call:
            conversation.ask("entry", "12", "第二问")
        msgs = call.call_args.kwargs["messages"]
        self.assertIn({"role": "assistant", "content": "第一答"}, msgs)
        self.assertEqual(msgs[-1]["content"], "第二问")
        # 本次问题不能在历史里再出现一次（重复喂两遍 = 白花钱且模型困惑）
        self.assertEqual(sum(1 for m in msgs if m["content"] == "第二问"), 1)

    @override_settings(WIKI_CHAT_HISTORY=2)
    def test_history_window(self):
        with patch_blk(), patch_call(("答1", "")):
            conversation.ask("entry", "12", "问1")
        with patch_blk(), patch_call(("答2", "")):
            conversation.ask("entry", "12", "问2")
        with patch_blk(), patch_call(("答3", "")) as call:
            conversation.ask("entry", "12", "问3")
        contents = [m["content"] for m in call.call_args.kwargs["messages"][1:]]
        # limit=2 是「最近 2 条**消息**」：问2 + 答2，再拼本次问题
        self.assertEqual(contents, ["问2", "答2", "问3"])

    def test_clear_removes_conversation(self):
        with patch_blk(), patch_call():
            conversation.ask("entry", "12", "问")
        self.assertTrue(conversation.clear("entry", "12"))
        self.assertEqual(Conversation.objects.count(), 0)
        self.assertEqual(ChatMessage.objects.count(), 0)


class ChatViewTest(TestCase):
    def test_get_renders_component(self):
        res = self.client.get("/wiki/chat?source_type=entry&source_id=12")
        self.assertEqual(res.status_code, 200)
        html = res.content.decode()
        self.assertIn('id="wiki-chat-entry-12"', html)
        # 标题「问AI」在浮窗顶栏（_article_tools.html），不在这个片段里 ——
        # 片段再写一个就是双标题。这里只该有说明行与发送表单。
        self.assertIn("只依据正文、我的划线与批注", html)
        self.assertIn('name="body"', html)
        # 空状态给的是可点的快捷问题（浮窗很小，先给「总结这篇」最省事）
        self.assertIn("data-wc-ask", html)

    def test_send_renders_reply(self):
        with patch_blk(), patch_call(("回答内容", "")):
            res = self.client.post("/wiki/chat/send",
                                   {"source_type": "entry", "source_id": "12",
                                    "body": "问一句"})
        html = res.content.decode()
        self.assertIn("回答内容", html)
        self.assertIn("问一句", html)

    def test_error_shown_in_component(self):
        with patch_blk(), patch_call(("", "知识库池今日预算（¥1）已用尽")):
            res = self.client.post("/wiki/chat/send",
                                   {"source_type": "entry", "source_id": "12",
                                    "body": "问一句"})
        self.assertIn("wc-err", res.content.decode())
        self.assertIn("预算", res.content.decode())

    def test_reply_html_is_escaped(self):
        # 模型输出（或我的提问）里的 <script> 只能显示成文本
        with patch_blk(), patch_call(("<script>alert(1)</script>", "")):
            res = self.client.post("/wiki/chat/send",
                                   {"source_type": "entry", "source_id": "12",
                                    "body": "<img src=x onerror=alert(1)>"})
        html = res.content.decode()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("<img src=x", html)


def _token(html: str) -> str:
    m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html)
    return m.group(1) if m else ""


class CsrfTest(TestCase):
    """★ 这一组的存在理由：Django 测试客户端**默认不校验 CSRF**
    （`enforce_csrf_checks=False`），所以「页面上的 form 忘了 {% csrf_token %}」
    能带着全绿测试上线，真实点击却 403 —— 2026-10-31 用户点「发送」就撞上了
    `Forbidden (CSRF token missing.): /wiki/chat/send`。

    所以这里必须**显式开校验**，并保留一条「不带 token 要被拦」的反证：
    否则某天有人把 enforce 关了，守卫会变成摆设。
    """

    def setUp(self):
        self.c = Client(enforce_csrf_checks=True)

    def test_send_without_token_is_rejected(self):
        res = self.c.post("/wiki/chat/send",
                          {"source_type": "entry", "source_id": "12", "body": "问"})
        self.assertEqual(res.status_code, 403)   # 反证：校验真的在拦

    def test_fragment_carries_token_so_send_passes(self):
        # 真实链路：懒加载片段 → 片段里的 token + 同一响应的 cookie → 提交
        frag = self.c.get("/wiki/chat?source_type=entry&source_id=12")
        token = _token(frag.content.decode())
        self.assertTrue(token, "对话片段里没有 csrf token，页面上提交必然 403")
        with patch_blk(), patch_call(("好的", "")):
            res = self.c.post("/wiki/chat/send",
                              {"source_type": "entry", "source_id": "12",
                               "body": "问一句", "csrfmiddlewaretoken": token})
        self.assertEqual(res.status_code, 200)
        self.assertIn("好的", res.content.decode())

    def test_clear_form_carries_token(self):
        # 清空按钮原先是个裸 <button hx-post>：没有 form 就没有 token，且 hx-vals
        # 走请求头（中文 source_id 会直接发不出去）。现在必须是带 token 的 form。
        conv = Conversation.objects.create(source_type="entry", source_id="12")
        conv.messages.create(role="user", body="问过一句")
        frag = self.c.get("/wiki/chat?source_type=entry&source_id=12")
        html = frag.content.decode()
        self.assertIn('hx-post="/wiki/chat/clear"', html)
        token = _token(html)
        self.assertTrue(token)
        res = self.c.post("/wiki/chat/clear",
                          {"source_type": "entry", "source_id": "12",
                           "csrfmiddlewaretoken": token})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(ChatMessage.objects.count(), 0)
