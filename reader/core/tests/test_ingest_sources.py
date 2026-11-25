"""素材从哪来（2026-11-02 的三处修复 + force 提炼）。

用户的原话：「回复内容的时候，避免从引用里提炼候选……避免从引用中总结观点，
有时候引文内容较多，我更关心的是作者的回复而非引文。」

这一组盯四件事，每条都对应一个**已经发生过**的错误：

1. **引用块要标出来**（`_material` 给 `<blockquote>` 套【引文】边界）。
   不标的话，模型会把「被引用者的话」当成本文作者的观点 —— 带 `<blockquote>`
   的社交源就是归属事故的来源。规则层只做**结构标注**，
   「引用的是谁」交给 prompt 判（见 `textutil.split_blockquotes` 的 ★）。

2. **对话里的划线/批注也要进材料**。它们存在 `source_type='chat'` 下
   （source_id = 消息 id），和文章正文的划线是两个命名空间 —— 之前整类被漏掉，
   人在对话里划的重点永远进不了候选。

3. **对话划线归到文章名下**（`_resolve_source`）。否则收件箱里会出现一堆
   「chat:47」这种不可读、也点不开的来源。

4. **force 提炼**：人从阅读列表点「发送到 wiki」时，即使没有划线没有对话，
   也允许从正文提炼。两条底线不能破：
   - 预算闸照旧（`pool=wiki`）—— 「跳过信号闸」不等于「跳过花钱闸」；
   - quote 仍须**逐字**来自正文，非 force 时正文不能当 quote 来源。

★ 反证纪律（本项目付过学费）：断言全绿不等于测到了东西。这组里的用例都
  在「把实现拆掉」时变红过（2026-11-02 手工验证：`_material` 去掉
  split_blockquotes → 引用块用例红；`my_words` 保留 `at=None` → 排序用例
  TypeError 红）。新加用例请照做一遍。
"""
import json
import shutil
import tempfile
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from core import ingest
from core import reporttext
from core import sourceblock
from core.models import Annotation, Conversation, Highlight
from core.textutil import to_plain

BLOCK = {"title": "测试文章", "text": "这是正文，属于作者，不是我的判断。" * 5, "html": ""}


class Base(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-sources-")
        cls.ov = override_settings(WIKI_DIR=cls.tmp)
        cls.ov.enable()

    @classmethod
    def tearDownClass(cls):
        cls.ov.disable()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        ingest.title_of.cache_clear()


class BlockquoteMaterialTest(Base):
    """引用块必须在材料里显形，且不能把「非引用段」也标进去。

    ★★ 这里喂的必须是**真实形状**（2026-11-02 踩过）：
      `sourceblock.load()` 返回的 `text` 已经过 `to_plain()`，`<blockquote>`
      标签在那一步就被抹成空格了；只有 `html` 还留着标签。第一版用例把
      `<blockquote>` 直接塞进 `text`，于是「对 text 切」也能过 —— 假绿，
      真实数据上这条修复等于没做。下面 4 条里前 3 条都在 `text` 里**不留**
      标签，就是为了逼实现去读 `html`。
    """

    def _material(self, html, text=None):
        # text 默认模拟 to_plain 之后的真实产物：标签没了，只剩正文
        if text is None:
            text = to_plain(html)
        return ingest._material("report", "daily-2026-10-31",
                                {"items": [], "chars": 0},
                                {"title": "t", "text": text, "html": html})

    def test_blockquote_is_wrapped_as_quote(self):
        body = self._material(
            "<p>我的观点是七格只适合 ToB。</p>"
            "<blockquote>被引者说：七格是万能的。</blockquote>")
        self.assertIn("【引文】", body)
        self.assertIn("【/引文】", body)
        self.assertRegex(body, r"【引文】[^\n]*七格是万能的[^\n]*【/引文】")

    def test_text_alone_cannot_reveal_the_boundary(self):
        """反证（也是这次的真 bug）：只给 text、html 为空时，边界无从得知 ——
        实现必须能分清「有标签可切」和「标签已被洗掉」，不能假装切到了。"""
        html = "<p>我的观点是A。</p><blockquote>被引者说B</blockquote>"
        body = self._material(html, text=to_plain(html))
        # 走 html 分支：能标出来
        self.assertIn("【引文】", body)
        # 同一份内容、只给已洗白的 text：标不出来（旧实现的真实效果）
        body_text_only = ingest._material(
            "report", "daily-2026-10-31", {"items": [], "chars": 0},
            {"title": "t", "text": to_plain(html), "html": ""})
        self.assertNotIn("【引文】", body_text_only)
        self.assertIn("被引者说B", body_text_only)   # 内容仍在，只是没有边界

    def test_author_text_is_not_wrapped(self):
        """反证：作者自己的话不能被套上【引文】—— 套错等于把人家的判断
        降级成「引用」，模型反而更不敢用。"""
        body = self._material("<p>我的观点是七格只适合 ToB。</p>")
        self.assertIn("我的观点是七格只适合 ToB", body)
        self.assertNotIn("【引文】", body)

    def test_several_quotes_each_get_a_boundary(self):
        body = self._material(
            "<p>A段作者的话</p><blockquote>B被引</blockquote>"
            "<p>C段作者的话</p><blockquote>D被引</blockquote>")
        self.assertEqual(body.count("【引文】"), 2)
        self.assertEqual(body.count("【/引文】"), 2)

    def test_material_cut_never_leaves_an_open_quote(self):
        """截断切在引文里时必须闭合边界。

        ★ 实测（2026-11-03，600 条真实条目）：`WIKI_INGEST_ARTICLE_CHARS=4000`
          有 50 条被截，其中 entry 6905（全长 4029）正好切在第二段【引文】中间。
          模型于是收到一个开了不闭合的边界：知道「后面是别人说的」，
          但不知道在哪结束 —— 剩下的正文全被算进引文。
        ★ 断言用的是「开闭配平」而不是「末尾有【/引文】」：后者会被
          「本来就闭合、只是没超长」蒙过去。
        """
        html = ("<p>作者一句。</p><blockquote>" + "被引者的话" * 200 + "</blockquote>")
        with override_settings(WIKI_INGEST_ARTICLE_CHARS=300):
            body = self._material(html)
        self.assertEqual(body.count("【引文】"), body.count("【/引文】"), body[-60:])
        self.assertIn("被引者的话", body, "引文内容不该被整段丢掉")


class ClipQuotesTest(SimpleTestCase):
    """截断只在**真的超长**时发生，且不许把引文边界切成半截。"""

    def test_cut_inside_quote_gets_closed(self):
        body = "作者开头。" + "【引文】" + "被引" * 100 + "【/引文】"
        out = reporttext.clip_quotes(body, 30)
        self.assertEqual(out.count("【引文】"), out.count("【/引文】"))
        self.assertTrue(out.endswith("【/引文】"))

    def test_short_text_is_untouched(self):
        """反证：没超长就一个字都不许动 —— 补标记会凭空造出一个边界。"""
        body = "作者的话。【引文】被引【/引文】"
        self.assertEqual(reporttext.clip_quotes(body, 1000), body)

    def test_cut_at_clean_boundary_adds_nothing(self):
        """切点落在作者段里时不许补【/引文】：那会切出一段不存在的引文。"""
        out = reporttext.clip_quotes("作者的话。" * 20, 15)
        self.assertNotIn("【引文", out)
        self.assertNotIn("【/引文", out)

    def test_cut_through_the_marker_keeps_it_harmless(self):
        """切点正好落在标记字符中间（`【引`）时不许崩，也不许补半个边界。

        ★ 这条是「补闭合」的边界条件：补标记的前提是**能数到完整的开标记**。
          半个标记本身不构成边界，模型不会把它当引文起点 —— 无害，别去修它。
        """
        body = "作者。【引文】被引" * 5
        out = reporttext.clip_quotes(body, 5)
        self.assertNotIn("【引文】", out)
        self.assertNotIn("【/引文】", out)


class ForwardQuoteTest(Base):
    """`本人回复 //@某人：原帖` —— 行内转发，和 `<blockquote>` 是同一件事。

    ★ 示例来源：
      `回复@A: 嗯嗯，明天卖点Google的put。//@A:回复@B:谷歌出财报了……`
      本人只回了一句，后面一大段是别人写的。不切的话模型把「谷歌出财报了」
      算到本人头上，claim 就写成「财报后卖看跌期权建仓」——「财报后」来自引文。
    ★ 这条判据必须落在 `reporttext` 里而不是各自的 `_material` 里：日报/周报、
      对话、wiki 提炼都从那儿取正文，**改一处三处一起好**，才不会再出现
      「2026-11-02 只修了 wiki 那条、对话那条漏到 2026-11-03」。
    """

    FORWARD = ("回复@A: 嗯嗯，明天卖点Google的put。"
               "//@A:回复@B:谷歌出财报了，搜索业务确实不太行了。")

    def test_forward_splits_author_from_quoted(self):
        out = reporttext.mark_forward_quote(self.FORWARD)
        self.assertIn("【引文】", out)
        before = out.split("【引文】")[0]
        self.assertIn("明天卖点Google的put", before, "本人的话必须在引文外面")
        self.assertNotIn("搜索业务确实不太行了", before, "被转发的话不许留在本人这侧")
        self.assertRegex(out, r"【引文】[^\n]*搜索业务确实不太行了[^\n]*【/引文】")

    def test_no_forward_means_untouched(self):
        """反证：没有 `//@` 就一个字也别加。全包成引文等于告诉模型「作者没说话」。"""
        plain = "我觉得这个判断站不住，因为分母变了。"
        self.assertEqual(reporttext.mark_forward_quote(plain), plain)

    def test_material_marks_forward_quote(self):
        """端到端：真实形状（html 无 blockquote、text 含 //@）也要标出来。"""
        body = ingest._material(
            "entry", "5730", {"items": [], "chars": 0},
            {"title": "t", "text": self.FORWARD, "html": "<p>%s</p>" % self.FORWARD})
        self.assertIn("【引文】", body)
        self.assertIn("搜索业务确实不太行了", body.split("【引文】")[1])

    def test_forward_and_blockquote_do_not_nest(self):
        """两种引文同时出现时不许嵌套：`【引文】…【引文】…` 会让模型更难读。

        ★ 这条原来只断言「配平 + 不嵌套」——把实现拆掉它照样绿（两边都是 0 个
          也算配平），等于没测。补上「转发内容真的被包了、作者的话真的在外面」，
          它才有牙齿。
        """
        html = ("<p>回复@A: 明天卖点 put。//@A:谷歌出财报了。</p>"
                "<blockquote>@B:北京汽车怎么样</blockquote>")
        body = reporttext.annotate_quotes(html)
        self.assertRegex(body, r"【引文】[^\n]*谷歌出财报了[^\n]*【/引文】")
        self.assertIn("明天卖点 put", body.split("【引文】")[0])
        self.assertEqual(body.count("【引文】"), body.count("【/引文】"))
        self.assertNotIn("【引文】【引文】", body)


class ChatMarksCollectedTest(Base):
    """对话里的划线/批注（source_type='chat'）要合并进来。"""

    def _conv(self, sid="101"):
        conv = Conversation.objects.create(source_type="entry", source_id=sid)
        msg = conv.messages.create(role="user", body="这段产能数据怎么看？")
        return conv, msg

    def test_chat_highlight_is_collected(self):
        _conv, msg = self._conv()
        Highlight.objects.create(source_type="chat", source_id=str(msg.id),
                                 quoted_text="产能至 2029 年已售罄", note="")
        hs = ingest.chat_highlights("entry", "101")
        self.assertEqual([h["quoted_text"] for h in hs], ["产能至 2029 年已售罄"])
        self.assertEqual(str(hs[0]["msg_id"]), str(msg.id))

    def test_chat_annotation_is_collected(self):
        _conv, msg = self._conv()
        Annotation.objects.create(source_type="chat", source_id=str(msg.id),
                                  body="这里的口径和上一段不一致")
        ans = ingest._chat_annotations("entry", "101")
        self.assertEqual([a["body"] for a in ans], ["这里的口径和上一段不一致"])

    def test_other_articles_chat_marks_do_not_leak(self):
        """反证：别的文章的对话划线不能混进来（否则提炼会串味）。"""
        other = Conversation.objects.create(source_type="entry", source_id="999")
        m2 = other.messages.create(role="user", body="不相干的问题")
        Highlight.objects.create(source_type="chat", source_id=str(m2.id),
                                 quoted_text="别的文章里的话", note="")
        self.assertEqual(ingest.chat_highlights("entry", "101"), [])

    def test_chat_note_and_question_can_be_sorted_together(self):
        """★ 真踩过的崩：对话来源的 item 曾写成 `at=None`，而 my_words 末尾
        用 `sort(key=at)` 和真 datetime 混排 → TypeError，整条提炼链炸掉。
        单条时排不出来，所以必须**同时有**对话项和文章项才测得出来。"""
        _conv, msg = self._conv()
        Highlight.objects.create(source_type="chat", source_id=str(msg.id),
                                 quoted_text="产能至 2029 年已售罄", note="这句是硬约束")
        # 文章自己也有一个带时间的项：两者混排才会触发比较
        Conversation.objects.filter(source_type="entry", source_id="101").first() \
            .messages.create(role="user", body="我认为这是周期股，不该给成长估值。")

        words = ingest.my_words("entry", "101")     # 不该抛 TypeError
        texts = [i["text"] for i in words["items"]]
        self.assertIn("这句是硬约束", texts)
        self.assertTrue(all(i.get("at") is not None for i in words["items"]),
                        "还有 item 的 at 是 None —— 排序迟早炸")

    def test_chat_note_carries_msg_id_anchor(self):
        """对话划线的备注要带 msg_id：深链才能落到那条消息。"""
        _conv, msg = self._conv()
        Highlight.objects.create(source_type="chat", source_id=str(msg.id),
                                 quoted_text="产能至 2029 年已售罄", note="这句是硬约束")
        item = next(i for i in ingest.my_words("entry", "101")["items"]
                    if i["text"] == "这句是硬约束")
        self.assertEqual(str(item["msg_id"]), str(msg.id))


class ResolveSourceTest(Base):
    """inventory 里 chat 来源要归到文章名下。"""

    def test_chat_source_resolves_to_article(self):
        conv = Conversation.objects.create(source_type="entry", source_id="101")
        msg = conv.messages.create(role="user", body="问题")
        self.assertEqual(ingest._resolve_source("chat", str(msg.id)), ("entry", "101"))

    def test_unknown_chat_id_is_left_alone(self):
        """反证：指向不存在的消息时不能瞎猜，原样返回（宁可难看也不要配错）。"""
        self.assertEqual(ingest._resolve_source("chat", "999999"), ("chat", "999999"))

    def test_inventory_has_no_chat_rows(self):
        conv = Conversation.objects.create(source_type="entry", source_id="101")
        msg = conv.messages.create(role="user", body="问题")
        Highlight.objects.create(source_type="chat", source_id=str(msg.id),
                                 quoted_text="产能至 2029 年已售罄", note="")
        with patch("core.sourceblock.load", return_value=BLOCK):
            rows = ingest.inventory()
        self.assertFalse([r for r in rows if r["source_type"] == "chat"],
                         "收件箱里出现了 chat:xx 这种点不开的来源")
        self.assertTrue([r for r in rows
                         if r["source_type"] == "entry" and r["source_id"] == "101"])

    def test_highlight_without_note_still_counts(self):
        """★ 只划线、没写备注也要进收件箱：划线本身就是信号。"""
        Highlight.objects.create(source_type="report", source_id="daily-2026-10-31",
                                 quoted_text="七格是 ToB 的需求筛选模型", note="")
        with patch("core.sourceblock.load", return_value=BLOCK):
            rows = ingest.inventory()
        self.assertTrue([r for r in rows if r["source_id"] == "daily-2026-10-31"])


class ForceExtractTest(Base):
    """force 提炼：跳过信号闸，但不跳过花钱闸与引用守卫。"""

    def _empty_candidates(self):
        return ('{"candidates": []}', "")

    def test_without_force_no_signal_never_calls_model(self):
        """反证：没有 force 时信号闸照旧（这道闸是省钱的第一道）。"""
        with patch("core.citeguard._call") as call:
            res = ingest.extract("report", "daily-2026-10-31")
        self.assertFalse(res["ok"])
        self.assertIn("没有可沉淀的信号", res["err"])
        call.assert_not_called()

    def test_force_skips_signal_gate(self):
        with patch("core.citeguard._call", return_value=self._empty_candidates()) as call, \
                patch("core.sourceblock.load", return_value=BLOCK):
            res = ingest.extract("report", "daily-2026-10-31", force=True)
        self.assertTrue(res["ok"], res)
        call.assert_called_once()

    def test_force_still_spends_from_wiki_pool(self):
        """★ 「跳过信号闸」不等于「不受预算约束」：出账口仍是 wiki 池。
        这条盯的是「有人为了让 force 好使，把 pool 改掉或加个 bypass」。"""
        with patch("core.citeguard._call", return_value=self._empty_candidates()) as call, \
                patch("core.sourceblock.load", return_value=BLOCK):
            ingest.extract("report", "daily-2026-10-31", force=True)
        kw = call.call_args.kwargs
        self.assertEqual(kw["pool"], "wiki")
        self.assertEqual(call.call_args.args[0], "wiki_extract")

    def test_force_prompt_actually_relaxes_the_rule(self):
        """force 参数传到了提示词里，而不是只改了函数签名。"""
        strict = ingest._system_prompt()
        loose = ingest._system_prompt(force=True)
        self.assertIn("《正文》**永远不能作为 quote 的来源**", strict)
        self.assertNotIn("允许从正文提取", strict)
        self.assertIn("允许从正文提取", loose)
        self.assertNotIn("永远是", loose.replace("永远不能", "永远是"))

    def test_force_ledger_remembers_it(self):
        """force 标记要留在台账里：收件箱据此显示「强制」，也是重新提炼的判据。"""
        with patch("core.citeguard._call", return_value=self._empty_candidates()), \
                patch("core.sourceblock.load", return_value=BLOCK):
            ingest.extract("report", "daily-2026-10-31", force=True)
        self.assertTrue(ingest.read_ledger()["report:daily-2026-10-31"].get("force"))

    def test_force_flag_survives_plain_refetch(self):
        """人先 force 提炼过、后来又普通提炼一次，台账不该丢掉「这是强制提炼」。"""
        with patch("core.citeguard._call", return_value=self._empty_candidates()), \
                patch("core.sourceblock.load", return_value=BLOCK):
            ingest.extract("report", "daily-2026-10-31", force=True)
            ingest.extract("report", "daily-2026-10-31")      # 普通那次不该抹掉标记
        self.assertTrue(ingest.read_ledger()["report:daily-2026-10-31"].get("force"))

    def test_force_cannot_quote_from_inside_a_quote(self):
        """force 提炼允许从正文摘，但**引文里的字不算正文**。

        ★ 这是「从引文里提炼候选」的最后一道口子：守卫拿 `blk["text"]` 当 hay
          时，被引者的话同样能通过、还被标成「正文事实」。2026-11-03 那条
          「财报后卖看跌期权建仓」正是这么来的 —— 它的「财报后」出自
          `//@` 转发里被引者的「谷歌出财报了」，不是博主说的。
        """
        html = ("<p>回复@A: 嗯嗯，明天卖点 put。</p>"
                "<blockquote>@A:谷歌出财报了，搜索业务不太行了。</blockquote>")
        blk = {"title": "t", "html": html, "text": to_plain(html)}
        payload = ('{"candidates":[{"kind":"surprise","claim":"谷歌搜索业务在下滑",'
                   '"quote":"谷歌出财报了，搜索业务不太行了","domain":"invest",'
                   '"confidence":"medium"}]}', "")
        with patch("core.citeguard._call", return_value=payload), \
                patch("core.sourceblock.load", return_value=blk):
            res = ingest.extract("entry", "5730", force=True)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["created"], 0, "引文里的句子不能变成「正文事实」候选")
        self.assertEqual(len(res["dropped"]), 1)

    def test_force_can_still_quote_the_authors_own_words(self):
        """反证：作者自己那句话仍可摘 —— 别把口子焊死。"""
        html = ("<p>回复@A: 明天卖点 put，我会等财报。</p>"
                "<blockquote>@A:谷歌出财报了。</blockquote>")
        blk = {"title": "t", "html": html, "text": to_plain(html)}
        payload = ('{"candidates":[{"kind":"decision","claim":"读者计划在财报后卖出看跌期权",'
                   '"quote":"明天卖点 put，我会等财报","domain":"invest",'
                   '"confidence":"medium"}]}', "")
        with patch("core.citeguard._call", return_value=payload), \
                patch("core.sourceblock.load", return_value=blk):
            res = ingest.extract("entry", "5730", force=True)
        self.assertEqual(res["created"], 1, res.get("dropped"))

    def test_drop_reason_tells_misplaced_quote_from_fabrication(self):
        """落选理由要能分清「摘错层」和「编造」—— 两者的下一步动作完全不同。

        摘错层 = 模型读懂了这篇，只是把别人的话当成作者的（对用户来说：去划一条线
        它就合法了）；编造 = 模型在写小说（对用户来说：这条链路不可信）。
        混成一句「不是我的原话，也不是我划的原文」，人就分不清模型是笨还是坏。
        """
        html = ("<p>回复@A: 嗯嗯，明天卖点 put。</p>"
                "<blockquote>@A:谷歌出财报了，搜索业务不太行了。</blockquote>")
        blk = {"title": "t", "html": html, "text": to_plain(html)}
        payload = ('{"candidates":['
                   '{"kind":"surprise","claim":"谷歌搜索业务在下滑",'
                   '"quote":"谷歌出财报了，搜索业务不太行了",'
                   '"domain":"invest","confidence":"medium"},'
                   '{"kind":"surprise","claim":"一条凭空出现的事实",'
                   '"quote":"这句话根本不在文章里的任何地方",'
                   '"domain":"invest","confidence":"medium"}]}', "")
        with patch("core.citeguard._call", return_value=payload), \
                patch("core.sourceblock.load", return_value=blk):
            res = ingest.extract("entry", "5730", force=True)
        self.assertEqual(res["created"], 0, res.get("dropped"))
        whys = [d["_why"] for d in res["dropped"]]
        self.assertTrue(any("【引文】" in w for w in whys),
                        "摘错层的理由要说清「这句在引文里」：%s" % whys)
        self.assertTrue(any("不是我的原话" in w for w in whys),
                        "编造的理由要保留原句：%s" % whys)

    def test_force_material_still_shows_the_quote(self):
        """★★ 剔除的只是「归属资格」，不是上下文（2026-11-03 用户追问）。

        用户原话：「引文有交代背景的作用，还是有参考价值的，强行删除会丢失
        上下文信息。」—— 对。守卫的 haystack 里剔掉引文，是为了不让被引者的话
        拿到「作者的判断」这个标签；但**模型看到的材料必须是完整的**，
        引文原文还在、还带【引文】边界。否则模型连背景都读不到，
        只能瞎猜这篇在说什么。
        """
        html = ("<p>回复@A: 嗯嗯，明天卖点 put。</p>"
                "<blockquote>@A:谷歌出财报了，搜索业务不太行了。</blockquote>")
        blk = {"title": "t", "html": html, "text": to_plain(html)}
        seen = {}

        def fake(*a, **k):
            seen["prompt"] = (a[1] if len(a) > 1 else "") + "\n" + (a[2] if len(a) > 2 else "")
            return ('{"candidates":[]}', "")

        with patch("core.citeguard._call", side_effect=fake), \
                patch("core.sourceblock.load", return_value=blk):
            ingest.extract("entry", "5730", force=True)
        prompt = seen.get("prompt") or ""
        self.assertIn("谷歌出财报了", prompt, "引文原文必须还在材料里（背景不能丢）")
        self.assertIn("【引文】", prompt, "而且是带边界的 —— 不然又回到归属事故")
        # ★ 不切 `split("【引文】")[0]`：SYSTEM 自己也提到【引文】这个词，
        #   切出来会是提示词开头，断言等于没测（第一版就这么写错了，红的）。
        i_author = prompt.find("明天卖点 put")
        i_quote = prompt.find("谷歌出财报了")
        self.assertTrue(0 <= i_author < i_quote, "作者的话必须排在引文前面")
        self.assertIn("【引文】", prompt[i_author:i_quote],
                      "作者的话和被引内容之间必须有边界，否则模型分不清谁说的")


class ArticleQuoteGuardTest(Base):
    """正文句子能不能当 quote：只在 force 时允许。"""

    QUOTE = "这是正文，属于作者，不是我的判断。"
    # ★ kind 必须在 INGEST_KINDS 里（surprise/reuse/decision/pitfall/transfer）：
    #   写个 "fact" 会先被 clean_candidates 静默丢掉，测的就不是引用守卫了
    #   —— 2026-11-02 第一次写这条用例就踩了（dropped=0 而 created=0，两处都不对）。
    REPLY = json.dumps({"candidates": [
        {"kind": "reuse", "claim": "正文里的一条事实", "quote": QUOTE,
         "domain": "ai", "confidence": "medium"},
    ]}, ensure_ascii=False)

    def test_article_quote_rejected_without_force(self):
        # 有别的信号（一句提问）先把信号闸过掉，测的才是引用守卫
        conv = Conversation.objects.create(source_type="report",
                                           source_id="daily-2026-10-31")
        conv.messages.create(role="user", body="我认为这条结论有个没说的前提。")
        with patch("core.citeguard._call", return_value=(self.REPLY, "")), \
                patch("core.sourceblock.load", return_value=BLOCK):
            res = ingest.extract("report", "daily-2026-10-31")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["created"], 0, "正文句子被当成了合法 quote")
        self.assertEqual(len(res["dropped"]), 1)

    def test_article_quote_accepted_when_forced(self):
        with patch("core.citeguard._call", return_value=(self.REPLY, "")), \
                patch("core.sourceblock.load", return_value=BLOCK):
            res = ingest.extract("report", "daily-2026-10-31", force=True)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["created"], 1)
        from core.models import IngestCandidate
        self.assertEqual(IngestCandidate.objects.get().quote_from, "article")


class SourceNameTest(Base):
    """材料抬头要写清「这条是谁发布的」（2026-11-03）。

    起因：一条社交源回帖提炼出「某作者在财报后表示，明天卖出一些看跌期权」，
    看着完全正确 —— 但材料里那个名字**一次都没出现**，模型是靠先验知识把网名
    认成真人的。认对了是运气；换成转载号 / 聚合号就是张冠李戴，而引用守卫只
    比对 quote 是否逐字，管不到主语。

    这里盯三件事：
      1. 有订阅源标题，就必须出现在材料里；
      2. 没有（日报/周报、老数据）时**不许留下「来源：」空标签** —— 空标签比没有
         更糟：模型要么以为来源不明，要么干脆自己编一个；
      3. 提炼、对话两条链路共用同一份实现，不能只修一条（这个项目已经为
         「同一判据抄两份」付过两次学费）。
    """

    BLK = {"title": "回复@某人", "text": "正文一句。", "html": "",
           "feed_title": "某作者 的社交动态"}

    def test_source_line_in_material(self):
        out = ingest._material("entry", "5730", {"items": [], "chars": 0}, dict(self.BLK))
        self.assertIn("来源：某作者 的社交动态", out)

    def test_source_line_sits_between_title_and_body(self):
        out = ingest._material("entry", "5730", {"items": [], "chars": 0}, dict(self.BLK))
        i_title, i_src, i_body = out.find("# 材料："), out.find("来源："), out.find("## 正文")
        self.assertTrue(0 <= i_title < i_src < i_body,
                        "来源行要挨着标题、排在正文之前，否则模型读正文时还没看到主体")

    def test_no_dangling_label_without_feed_title(self):
        for blk in ({"title": "t", "text": "x", "html": ""},
                    {"title": "t", "text": "x", "html": "", "feed_title": ""},
                    {"title": "t", "text": "x", "html": "", "feed_title": "   "}):
            with self.subTest(blk=blk):
                self.assertNotIn("来源：",
                                 ingest._material("entry", "1", {"items": [], "chars": 0}, blk))

    def test_load_carries_feed_title(self):
        class _Entry:
            title = "t"
            content = "<p>正文</p>"
            feed_title = " 某源 "

        class _DS:
            def get_entry(self, _id):
                return _Entry()

        with patch("core.datasource.get_datasource", return_value=_DS()):
            blk = sourceblock.load("entry", "1")
        self.assertEqual(blk["feed_title"], "某源", "取块时必须带出订阅源，且去掉首尾空白")
