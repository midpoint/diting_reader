"""入库分诊 + 提取（阶段 2.2，2026-10-31）。

这一组盯的是**三层语义有没有被混掉**（这是整套产品的立场，也是我 2026-10-31 被
当面纠正过的地方）：

  正文 = 作者的；划线 = 我在意（不是我的结论）；**我敲的字 = 我的判断**。

边界上最容易错、错了还不报错的四件事：

1. **预设话术被当成"我敲的字"**：快捷问题按钮和「点开即总结」也是 role=user 的
   user 消息，但那是**我点出来的字，不是我想的字**。不排除的话，「总结这篇文章」
   会被当成我的判断 —— 整条沉淀链从第一个字就错了，而且页面上看不出来。
2. **模型的 quote 改写了我的原话**：看着完全像我说的话，其实是它写的。
   这是「把 AI 的观点伪装成我的判断」，比不提取坏得多。只能靠程序比对，
   prompt 挡不住 —— 所以 `verify_quotes` 有单独一组。
3. **划线原文被当成我的原话**：「我划的这句」和「我说的这句」在库里长得很像
   （都是文本），混起来等于让作者的观点冒充我的判断。
4. **0 条候选被当成失败**：模型说「这些话里没有可沉淀的判断」是合法结论。
   当成失败会让人反复点（每次扣钱），当成 0 条又不写台账会让人误以为没跑过。

★ 视图层单独有 CSRF 组：Django 测试客户端默认 `enforce_csrf_checks=False`，
  新增会 POST 的表单必须用 `Client(enforce_csrf_checks=True)` 验一遍 ——
  2026-10-31 `/wiki/chat/send` 就是这么漏过去的（单测全绿、页面上点一下 403）。
"""
import json
import shutil
import tempfile
from unittest.mock import patch

from django.conf import settings
from django.test import Client, TestCase, override_settings

from core import conversation as conv_mod
from core import citeguard
from core import ingest
from core.models import Annotation, Conversation, Highlight, IngestCandidate


def my_conv(source_id="daily-2026-10-31", questions=(), presets=True):
    """造一个会话：可选的预设话术（自动总结）+ 我自己敲的提问。"""
    conv, _ = Conversation.objects.get_or_create(source_type="report",
                                                 source_id=source_id)
    if presets:
        conv.messages.create(role="user", body=conv_mod.AUTOSEND_TEXT)
    for q in questions:
        conv.messages.create(role="user", body=q)
    return conv


BLOCK = {"title": "测试文章", "text": "这是正文，属于作者，不是我的判断。" * 5,
         "html": "<p>x</p>"}


class PresetTest(TestCase):
    """★ 这一组是整套判据的地基：预设话术必须被排除。"""

    def test_every_preset_is_detected(self):
        for t in conv_mod.PRESET_TEXTS:
            self.assertTrue(ingest.is_preset(t), t)

    def test_preset_with_extra_whitespace_is_detected(self):
        t = "  用中文总结这篇文章：先给 3-5 条要点，再指出作者的核心论断与主要证据。 "
        self.assertTrue(ingest.is_preset(t))

    def test_my_own_words_are_not_preset(self):
        self.assertFalse(ingest.is_preset("我理解这就是个 ToB 需求筛选的结构化模型。"))

    def test_empty_is_preset(self):
        # 空消息不该变成候选来源；归一化成空串的一律当作「没有我的话」
        self.assertTrue(ingest.is_preset("   "))
        self.assertTrue(ingest.is_preset("。。。"))


class MyWordsTest(TestCase):
    def test_preset_questions_are_excluded_but_mine_kept(self):
        my_conv(questions=["这七格在 ToC 场景是不是没用了？"])
        w = ingest.my_words("report", "daily-2026-10-31")
        texts = [i["text"] for i in w["items"]]
        self.assertIn("这七格在 ToC 场景是不是没用了？", texts)
        # 自动总结那句不在
        self.assertNotIn(conv_mod.AUTOSEND_TEXT, texts)
        self.assertEqual(w["chars"], len("这七格在 ToC 场景是不是没用了？"))

    def test_highlight_note_counts_but_quote_does_not(self):
        Highlight.objects.create(source_type="report", source_id="daily-2026-10-31",
                                 quoted_text="这是作者的原话，很长很长的一句。",
                                 note="这句是重点")
        w = ingest.my_words("report", "daily-2026-10-31")
        self.assertEqual([i["text"] for i in w["items"]], ["这句是重点"])

    def test_highlight_without_note_adds_nothing(self):
        Highlight.objects.create(source_type="report", source_id="daily-2026-10-31",
                                 quoted_text="没有备注的划线", note="")
        self.assertEqual(ingest.my_words("report", "daily-2026-10-31")["chars"], 0)

    def test_annotation_counts(self):
        Annotation.objects.create(source_type="report", source_id="daily-2026-10-31",
                                  body="我的批注：这条结论依赖前提，前提变了就不成立。")
        self.assertEqual(len(ingest.my_words("report", "daily-2026-10-31")["items"]), 1)

    # ---- 深链锚点（2026-11-02）：item 必须带来源 id，编译阶段才能拼精确深链
    def test_question_item_carries_msg_id(self):
        conv = my_conv(questions=["这七格在 ToC 场景是不是没用了？"])
        w = ingest.my_words("report", "daily-2026-10-31")
        q = [i for i in w["items"] if i["kind"] == "question"][0]
        msg = conv.messages.filter(role="user", body="这七格在 ToC 场景是不是没用了？").first()
        self.assertEqual(q["msg_id"], msg.id)

    def test_note_item_carries_mark_id(self):
        h = Highlight.objects.create(source_type="report", source_id="daily-2026-10-31",
                                     quoted_text="作者原话", note="这句是重点")
        w = ingest.my_words("report", "daily-2026-10-31")
        n = [i for i in w["items"] if i["kind"] == "note"][0]
        self.assertEqual(n["mark_id"], h.id)

    def test_annotation_item_carries_mark_id(self):
        a = Annotation.objects.create(source_type="report", source_id="daily-2026-10-31",
                                      body="我的批注")
        w = ingest.my_words("report", "daily-2026-10-31")
        an = [i for i in w["items"] if i["kind"] == "annotation"][0]
        self.assertEqual(an["mark_id"], a.id)


class VerifyQuoteTest(TestCase):
    """引用守卫：只认逐字的「我的原话」或「我划的原文」。"""

    def setUp(self):
        self.words = {"items": [{"kind": "question", "text": "我认为这七格只适合 ToB，ToC 没有对应物。",
                                 "at": None}], "chars": 30, "err": ""}
        self.highlights = [{"quoted_text": "七格是 ToB 的需求筛选模型"}]

    def test_verbatim_kept(self):
        kept, dropped = ingest.verify_quotes(
            [{"claim": "七格只适合 ToB", "quote": "这七格只适合 ToB"}], self.words)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["quote_from"], "mine")
        self.assertEqual(dropped, [])

    def test_punctuation_and_space_difference_ok(self):
        kept, _ = ingest.verify_quotes(
            [{"claim": "x", "quote": "这七格只适合ToB ，ToC没有对应物"}], self.words)
        self.assertEqual(len(kept), 1, "标点/空格差异不该判死（模型抄时常改写标点）")

    def test_claim_copying_quote_is_dropped(self):
        """claim 与 quote 一字不差 = 没理解，等于把原文当候选。

        用户原话：「『嗯嗯，明天卖点某公司的 put』这种我希望出来的
        候选是『某作者计划明天卖点某公司的 put』，llm 应该会有自己的理解，
        为啥直接把原文当候选呢？」——候选的价值全在 claim 这一句，
        claim 只是原文的搬运，这条候选对三个月后的自己毫无用处。
        """
        kept, dropped = ingest.verify_quotes(
            [{"claim": "这七格只适合 ToB", "quote": "这七格只适合 ToB"}], self.words)
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 1)
        self.assertIn("逐字相同", dropped[0]["_why"])

    def test_claim_shortening_quote_is_kept(self):
        """反证：删掉「这」是合格的简化，不是照抄。

        ★ 守卫过严比漏一条更贵：人一旦发现「正常候选被吞」，就会开始怀疑整条
          链路，最后绕过它。所以只拦「完全相同」，不拦子串。
        """
        kept, dropped = ingest.verify_quotes(
            [{"claim": "七格只适合 ToB", "quote": "这七格只适合 ToB"}], self.words)
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, [])

    def test_claim_that_rewrites_quote_is_kept(self):
        """正常改写当然要留：用户要的就是这一种。"""
        kept, _ = ingest.verify_quotes(
            [{"claim": "读者认为七格模型只在 ToB 成立",
              "quote": "这七格只适合 ToB"}], self.words)
        self.assertEqual(len(kept), 1)

    def test_rewritten_dropped(self):
        # ★ 换了词 = 不是我的原话。这是「AI 帮我编了一句我的话」，必须丢。
        kept, dropped = ingest.verify_quotes(
            [{"claim": "x", "quote": "七格法在消费者市场基本失效"}], self.words)
        self.assertEqual(kept, [])
        self.assertIn("不是我的原话", dropped[0]["_why"])

    def test_highlight_quote_kept_and_marked(self):
        # ★ 划线摘录也是合法来源（我在意/认可的事实），但要标注 quote_from=highlight
        kept, dropped = ingest.verify_quotes(
            [{"claim": "七格是 ToB 的筛选模型", "quote": "七格是 ToB 的需求筛选模型"}],
            self.words, highlights=self.highlights)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["quote_from"], "highlight")
        self.assertEqual(dropped, [])

    def test_quote_from_article_dropped(self):
        # 作者正文（既不是我的原话、也不是我划出的原文）⇒ 丢
        kept, dropped = ingest.verify_quotes(
            [{"claim": "x", "quote": "这是正文，属于作者，不是我的判断。"}],
            self.words, highlights=self.highlights)
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 1)

    def test_missing_quote_dropped(self):
        kept, dropped = ingest.verify_quotes([{"claim": "x", "quote": ""}], self.words)
        self.assertEqual(kept, [])
        self.assertIn("缺 quote", dropped[0]["_why"])


class VerifyQuoteAnchorTest(TestCase):
    """深链锚点：quote 命中哪条，就把哪条的 id 挂到候选上（不靠编译时文本反查）。"""

    def test_mine_from_question_gets_msg_id(self):
        words = {"items": [{"kind": "question", "text": "这七格只适合 ToB。",
                            "at": None, "msg_id": 42}], "chars": 10, "err": ""}
        kept, _ = ingest.verify_quotes(
            [{"claim": "x", "quote": "这七格只适合 ToB"}], words)
        self.assertEqual(kept[0]["quote_from"], "mine")
        self.assertEqual(kept[0]["msg_id"], 42)
        self.assertIsNone(kept[0].get("mark_id"))

    def test_mine_from_note_gets_mark_id(self):
        words = {"items": [{"kind": "note", "text": "这句是重点。",
                            "at": None, "mark_id": 7}], "chars": 10, "err": ""}
        kept, _ = ingest.verify_quotes(
            [{"claim": "x", "quote": "这句是重点"}], words)
        self.assertEqual(kept[0]["mark_id"], 7)
        self.assertIsNone(kept[0].get("msg_id"))

    def test_highlight_gets_mark_id(self):
        words = {"items": [], "chars": 0, "err": ""}
        highlights = [{"id": 9, "quoted_text": "七格是 ToB 的筛选模型"}]
        kept, _ = ingest.verify_quotes(
            [{"claim": "x", "quote": "七格是 ToB 的筛选模型"}], words,
            highlights=highlights)
        self.assertEqual(kept[0]["quote_from"], "highlight")
        self.assertEqual(kept[0]["mark_id"], 9)

    def test_item_without_id_keeps_anchor_none(self):
        # 旧 item 没带 id（老数据/老测试），锚点应为 None —— 深链退回来源层
        words = {"items": [{"kind": "question", "text": "这七格只适合 ToB。",
                            "at": None}], "chars": 10, "err": ""}
        kept, _ = ingest.verify_quotes(
            [{"claim": "x", "quote": "这七格只适合 ToB"}], words)
        self.assertIsNone(kept[0].get("msg_id"))
        self.assertIsNone(kept[0].get("mark_id"))

    def test_shortest_hit_wins_when_substring_matches_many(self):
        # 同话说过两次（一长一短），quote 是短的那句的子串 → 锚到最短那条
        words = {"items": [
            {"kind": "question", "text": "这七格只适合 ToB，ToC 没有对应物。",
             "at": None, "msg_id": 100},
            {"kind": "question", "text": "这七格只适合 ToB。", "at": None, "msg_id": 101},
        ], "chars": 0, "err": ""}
        kept, _ = ingest.verify_quotes(
            [{"claim": "x", "quote": "这七格只适合 ToB"}], words)
        self.assertEqual(kept[0]["msg_id"], 101)


class CleanParseTest(TestCase):
    def test_illegal_kind_and_empty_claim_dropped(self):
        out = ingest.clean_candidates([
            {"kind": "bogus", "claim": "a", "quote": "q"},
            {"kind": "decision", "claim": "", "quote": "q"},
            {"kind": "decision", "claim": "ok", "quote": "q"},
        ], 6)
        self.assertEqual([c["claim"] for c in out], ["ok"])

    def test_bad_domain_and_confidence_blanked_not_guessed(self):
        out = ingest.clean_candidates(
            [{"kind": "pitfall", "claim": "c", "quote": "q",
              "domain": "crypto", "confidence": "sure"}], 6)
        self.assertEqual((out[0]["domain"], out[0]["confidence"]), ("", ""))

    def test_limit_applied(self):
        cands = [{"kind": "reuse", "claim": f"c{i}", "quote": "q"} for i in range(10)]
        self.assertEqual(len(ingest.clean_candidates(cands, 3)), 3)

    def test_parse_fenced_and_chatty(self):
        text = '好的，结果如下：\n```json\n{"candidates":[{"kind":"decision","claim":"a","quote":"b"}]}\n```'
        self.assertEqual(len(ingest.parse_candidates(text)), 1)

    def test_parse_bare_array(self):
        self.assertEqual(len(ingest.parse_candidates('[{"kind":"decision","claim":"a"}]')), 1)

    def test_parse_garbage_returns_empty(self):
        self.assertEqual(ingest.parse_candidates("我觉得这篇文章很好。"), [])


class ExtractTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-ingest-")
        cls.ov = override_settings(WIKI_DIR=cls.tmp)
        cls.ov.enable()

    @classmethod
    def tearDownClass(cls):
        cls.ov.disable()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        ingest.title_of.cache_clear()

    def test_zero_words_never_calls_model(self):
        my_conv(questions=[])          # 只有自动总结那句 ⇒ 零个字是我的，也没划线
        with patch("core.citeguard._call") as call:
            res = ingest.extract("report", "daily-2026-10-31")
        self.assertFalse(res["ok"])
        self.assertIn("没有可沉淀的信号", res["err"])
        call.assert_not_called()       # ★ 确定性省钱闸：一个字都没有就别调模型

    def test_happy_path_creates_and_writes_ledger(self):
        my_conv(questions=["我认为这七格只适合 ToB，ToC 没有对应物。"])
        reply = json.dumps({"candidates": [
            {"kind": "decision", "claim": "七格只适合 ToB", "quote": "这七格只适合 ToB",
             "domain": "ai", "confidence": "medium"},
            {"kind": "transfer", "claim": "编的", "quote": "我从来没说过这句"},
        ]}, ensure_ascii=False)
        with patch("core.citeguard._call", return_value=(reply, "")) as call, \
                patch("core.sourceblock.load", return_value=BLOCK):
            res = ingest.extract("report", "daily-2026-10-31")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["created"], 1)
        self.assertEqual(len(res["dropped"]), 1)
        call.assert_called_once()
        # 记账 kind 与池必须是 wiki 的那一对（写错池 = 挤占日报预算且查不出来）
        kw = call.call_args.kwargs
        self.assertEqual(call.call_args.args[0], "wiki_extract")
        self.assertEqual(kw["pool"], "wiki")
        c = IngestCandidate.objects.get()
        self.assertEqual((c.kind, c.claim, c.status), ("decision", "七格只适合 ToB", "pending"))
        led = ingest.read_ledger()
        self.assertIn("report:daily-2026-10-31", led)
        self.assertEqual(led["report:daily-2026-10-31"]["n"], 1)

    def test_zero_candidates_still_writes_ledger(self):
        """★ 0 条也必须记账：否则收件箱永远显示「还没提炼」，人会反复点、反复扣钱。"""
        # 只是提问、没表达判断 —— 模型正确输出就是"没有候选"。
        # ★ 问题要够长（超过 WIKI_INGEST_MIN_CHARS），否则先被零字闸挡掉，
        #   测的就不是「0 条候选」而是「字数不够」了。
        my_conv(questions=["这篇里提到的七格法，第三格那个频次门槛具体是怎么算的？"])
        with patch("core.citeguard._call", return_value=('{"candidates": []}', "")), \
                patch("core.sourceblock.load", return_value=BLOCK):
            res = ingest.extract("report", "daily-2026-10-31")
        self.assertTrue(res["ok"])
        self.assertEqual(res["created"], 0)
        self.assertIn("report:daily-2026-10-31", ingest.read_ledger())

    def test_highlight_only_is_enough_signal(self):
        """★ 只有划线摘录、没有我敲的字，也算有信号（2026-11-01 用户纠正）：
        划出的原文代表我的关切，可能是认可的观点/重要事实，不该因「没到 15 字」
        而被零字闸挡掉。"""
        Highlight.objects.create(source_type="report", source_id="daily-2026-10-31",
                                 quoted_text="七格是 ToB 的需求筛选模型，把想要变成能检验",
                                 note="")
        with patch("core.citeguard._call", return_value=('{"candidates": []}', "")) as call, \
                patch("core.sourceblock.load", return_value=BLOCK):
            res = ingest.extract("report", "daily-2026-10-31")
        self.assertTrue(res["ok"], res)   # 不再被零字闸拒
        call.assert_called_once()

    def test_single_char_is_enough_signal(self):
        """★ 门槛再放宽：一个字的判断也认（2026-11-01 用户纠正）。
        「好」就代表认可 —— 重要的不是字数，是「有 vs 没有」信号。"""
        my_conv(questions=["好"])
        with patch("core.citeguard._call", return_value=('{"candidates": []}', "")) as call, \
                patch("core.sourceblock.load", return_value=BLOCK):
            res = ingest.extract("report", "daily-2026-10-31")
        self.assertTrue(res["ok"], res)   # 单字不再被零字闸拒
        call.assert_called_once()

    def test_highlight_quote_from_persisted(self):
        """划线来源的候选落库时 quote_from=highlight，编译时据此区分「我的判断」vs「我划的素材」。"""
        my_conv(questions=[])
        Highlight.objects.create(source_type="report", source_id="daily-2026-10-31",
                                 quoted_text="七格是 ToB 的需求筛选模型", note="")
        reply = json.dumps({"candidates": [
            {"kind": "reuse", "claim": "七格是 ToB 的筛选模型", "quote": "七格是 ToB 的需求筛选模型",
             "domain": "ai", "confidence": "medium"},
        ]}, ensure_ascii=False)
        with patch("core.citeguard._call", return_value=(reply, "")), \
                patch("core.sourceblock.load", return_value=BLOCK):
            res = ingest.extract("report", "daily-2026-10-31")
        self.assertTrue(res["ok"], res)
        c = IngestCandidate.objects.get()
        self.assertEqual(c.quote_from, "highlight")

    def test_unparsable_output_is_failure_not_zero(self):
        my_conv(questions=["我认为这条结论依赖一个没说的前提，所以我不采纳。"])
        with patch("core.citeguard._call", return_value=("这篇文章挺好的。", "")), \
                patch("core.sourceblock.load", return_value=BLOCK):
            res = ingest.extract("report", "daily-2026-10-31")
        self.assertFalse(res["ok"])
        self.assertIn("candidates", res["err"])

    def test_model_error_propagates(self):
        my_conv(questions=["我认为这条结论依赖一个没说的前提，所以我不采纳。"])
        with patch("core.citeguard._call", return_value=("", "wiki 池预算已用尽")), \
                patch("core.sourceblock.load", return_value=BLOCK):
            res = ingest.extract("report", "daily-2026-10-31")
        self.assertFalse(res["ok"])
        self.assertIn("预算", res["err"])

    def test_truncated_output_does_not_write_ledger(self):
        """★★ 输出被预算硬截断时**不许记台账**（2026-11-09 清零重来现场抓出）。

        截断的 JSON 是半截 → 候选必然少几条（真实现场 created=0）。台账一写，
        这篇就被标成「提炼过了」，下次不再提炼 ⇒ 少掉的那部分永久蒸发，
        而且一个字都不报。宁可下次再花一次钱，也不静默少提炼。

        真实数据（30 次提炼的 LlmUsage）：1 次 i=3214 / out=4096（正好撞顶）/
        27.7 s / ¥0.0399，created=0、状态却变 done。
        """
        my_conv(questions=["我认为这七格只适合 ToB，ToC 没有对应物。"])
        # ★ 前置：本类的 WIKI_DIR 是 class 级共享的，前面的用例已经写过同名台账。
        #   不先清掉，「没写台账」这条断言会被上一轮的残留文件骗成假红/假绿。
        ingest._write_ledger({})
        self.assertNotIn("report:daily-2026-10-31", ingest.read_ledger())
        # 真·半截 JSON：数组没闭合（模型被 max_tokens 切断就是这样）
        half = '{"candidates":[{"kind":"decision","claim":"七格只适合 ToB","quote":"七格'
        stats = citeguard.fresh_stats()
        stats["truncated"] = ["wiki 提取（预算 8192 tokens）"]
        with patch("core.citeguard.fresh_stats", return_value=stats), \
                patch("core.citeguard._call", return_value=(half, "")), \
                patch("core.sourceblock.load", return_value=BLOCK):
            res = ingest.extract("report", "daily-2026-10-31")
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["truncated"], "必须如实标出这次被截断")
        self.assertEqual(res["created"], 0)
        # ★ 核心断言：没写台账 ⇒ 收件箱仍然是「没提炼」，下次会重新提炼
        self.assertNotIn("report:daily-2026-10-31", ingest.read_ledger())

    def test_not_truncated_writes_ledger_as_counter_evidence(self):
        """反证上一条：同一段半截 JSON，只要没被标截断，台账就该照写。

        没有这条，「没写台账」也可能只是因为链路压根没走到写台账那一步（假绿）。
        """
        my_conv(questions=["我认为这七格只适合 ToB，ToC 没有对应物。"])
        half = '{"candidates":[{"kind":"decision","claim":"七格只适合 ToB","quote":"七格'
        ingest._write_ledger({})   # 同上：先清掉 class 级共享目录里的残留台账
        with patch("core.citeguard._call", return_value=(half, "")), \
                patch("core.sourceblock.load", return_value=BLOCK):
            res = ingest.extract("report", "daily-2026-10-31")
        self.assertTrue(res["ok"], res)
        self.assertFalse(res["truncated"])
        self.assertIn("report:daily-2026-10-31", ingest.read_ledger())

    def test_missing_block_is_rejected(self):
        my_conv(questions=["我认为这条结论依赖一个没说的前提，所以我不采纳。"])
        with patch("core.sourceblock.load", return_value=None):
            res = ingest.extract("report", "daily-2026-10-31")
        self.assertFalse(res["ok"])
        self.assertIn("正文", res["err"])


class InventoryTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-ingest-")
        cls.ov = override_settings(WIKI_DIR=cls.tmp)
        cls.ov.enable()

    @classmethod
    def tearDownClass(cls):
        cls.ov.disable()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        ingest.title_of.cache_clear()
        # ★ 2026-11-02：每条测试都从空台账开始，否则跨测试残留会导致
        #   test_preset_only_source_not_listed 失败（前一个测试写的台账还在）
        try:
            (ingest.wstore.root() / "meta" / "ingest.json").unlink()
        except OSError:
            pass

    def _inv(self):
        with patch("core.sourceblock.load", return_value=BLOCK):
            return ingest.inventory()

    def _mark_done(self):
        """把当前状态写进台账 = 假装刚提炼过（必须带**当前**算法版本，否则会退回字数比较）。

        ★ 用 `signal_sig()` 而不是 `my_words()` + `words_sig()`：台账要记的是
          「信号」的指纹（我的原话 + 我划出的原文）。测试这边自己拼一套，
          测的就不是线上那份口径了。
        """
        w = ingest.my_words("report", "daily-2026-10-31")
        sig, chars, _n = ingest.signal_sig("report", "daily-2026-10-31", w)
        ingest._write_ledger({"report:daily-2026-10-31":
                              {"at": "2026-10-31 21:00", "chars": chars,
                               "sig": sig, "sigv": ingest.SIG_VERSION,
                               "n": 0}})
        return w

    def test_new_then_done_then_changed(self):
        my_conv(questions=["我认为这七格只适合 ToB，ToC 没有对应物。"])
        row = self._inv()[0]
        self.assertEqual(row["state"], "new")

        self._mark_done()
        self.assertEqual(self._inv()[0]["state"], "done")

        conv = Conversation.objects.get(source_type="report")
        conv.messages.create(role="user", body="我又想到一点：这套方法的前提是组织有流程。")
        self.assertEqual(self._inv()[0]["state"], "changed")

    def test_same_length_rewrite_counts_as_changed(self):
        """★ 字数相同但内容变了必须是「又写了」。

        这是字数判定会**静默漏掉**的一种：改了措辞、字数没变 ⇒ 状态还是「已提炼」，
        新写的判断永远等不到提炼。指纹不会漏。
        """
        my_conv(questions=["我认为这七格只适合 ToB，ToC 没有对应物。"])
        w = self._mark_done()
        conv = Conversation.objects.get(source_type="report")
        old = conv.messages.filter(role="user").exclude(body=conv_mod.AUTOSEND_TEXT).first()
        # 换一个同字数的说法（「只适合」→「仅适合」，都是 3 字）
        old.body = old.body.replace("只适合", "仅适合")
        old.save(update_fields=["body"])
        self.assertEqual(ingest.my_words("report", "daily-2026-10-31")["chars"], w["chars"],
                         "字数必须没变，否则测的就不是这一条")
        self.assertEqual(self._inv()[0]["state"], "changed")

    def test_whitespace_only_change_stays_done(self):
        """★ 只是空白/标点差别不能算「又写了」——否则点一次白花一次钱。"""
        my_conv(questions=["我认为这七格只适合 ToB，ToC 没有对应物。"])
        self._mark_done()
        conv = Conversation.objects.get(source_type="report")
        old = conv.messages.filter(role="user").exclude(body=conv_mod.AUTOSEND_TEXT).first()
        old.body = "  " + old.body + "  "        # 字数变了 4 个，但归一化后一模一样
        old.save(update_fields=["body"])
        self.assertEqual(self._inv()[0]["state"], "done")

    def test_old_fingerprint_version_falls_back_not_phantom_changed(self):
        """★ 指纹算法升级后，旧 sig 必须失效但**不能**把所有来源都判成「又写了」。

        否则升级一次 = 每个来源都催着再花一次钱。判据：退回字数比较。
        """
        my_conv(questions=["我认为这七格只适合 ToB，ToC 没有对应物。"])
        w = ingest.my_words("report", "daily-2026-10-31")
        ingest._write_ledger({"report:daily-2026-10-31":
                              {"at": "2026-10-31 21:00", "chars": w["chars"],
                               "sig": "deadbeefdeadbeef", "sigv": 1, "n": 0}})
        self.assertEqual(self._inv()[0]["state"], "done")

    def test_preset_only_source_not_listed(self):
        my_conv(questions=[])          # 只有自动总结
        self.assertEqual(self._inv(), [])

    def test_title_falls_back_to_key(self):
        my_conv(questions=["我认为这七格只适合 ToB，ToC 没有对应物。"])
        with patch("core.sourceblock.load", return_value=None):
            self.assertEqual(ingest.inventory()[0]["title"], "report:daily-2026-10-31")


class SignalStateTest(TestCase):
    """提炼完必须**真的**变成「已提炼」——台账与收件箱不能各算一套指纹。

    ★ 2026-11-03 实测（entry 6670）：提炼成功、候选也落了库，状态却还是
      「提炼后又写了」，再点一次再花一次钱。根因不是花不花钱，是**同一件事写了两遍**：
      台账按 `my_words()`（我的原话）算指纹，收件箱另外把「没写备注的划线原文」
      也算进信号 —— 两个集合永远差一条，指纹永远对不上。库里 12 条「又写了」
      全是这么来的，其中 4 条是 0 字纯划线（连一个字都没变也判「又写了」）。

    这一组盯的正是那个差集：
      ① 只有划线（没备注）也必须能变成 done；
      ② 提炼后又划一段 ⇒ changed（不能静默漏掉新信号）；
      ③ 给已有划线补备注 ⇒ changed；
      ④ 台账记的字数 == 收件箱显示的字数（否则旧版本退字数比较时同样永久 changed）；
      ⑤ 对话里的批注要归到文章下，不许凭空多出一行 `chat:xx`。
    """

    SID = "6670"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-sig-")
        cls.ov = override_settings(WIKI_DIR=cls.tmp)
        cls.ov.enable()

    @classmethod
    def tearDownClass(cls):
        cls.ov.disable()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        ingest.title_of.cache_clear()
        try:
            (ingest.wstore.root() / "meta" / "ingest.json").unlink()
        except OSError:
            pass
        conv = Conversation.objects.create(source_type="entry", source_id=self.SID)
        conv.messages.create(role="user",
                             body="这是不是说明有自带流量就不必做 SEO 了？")
        self.msg = conv.messages.create(role="assistant", body="可能。")
        self.hl = Highlight.objects.create(source_type="chat", source_id=str(self.msg.id),
                                           quoted_text="热度最高的一批几乎不做 SEO", note="")

    def _extract(self):
        with patch("core.citeguard._call", return_value=('{"candidates": []}', "")), \
                patch("core.sourceblock.load", return_value=BLOCK):
            return ingest.extract("entry", self.SID)

    def _row(self):
        with patch("core.sourceblock.load", return_value=BLOCK):
            rows = [r for r in ingest.inventory() if r["source_id"] == self.SID]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def test_extract_makes_it_done_even_with_unnoted_highlight(self):
        self.assertEqual(self._row()["state"], "new")
        self.assertTrue(self._extract()["ok"])
        self.assertEqual(self._row()["state"], "done",
                         "提炼成功了却还显示「提炼后又写了」—— 指纹算的不是同一批字")

    def test_new_highlight_after_extract_is_changed(self):
        """反证：真的又划了一段，就必须报「又写了」（不许为了好看一律算 done）。"""
        self._extract()
        self.assertEqual(self._row()["state"], "done")
        Highlight.objects.create(source_type="chat", source_id=str(self.msg.id),
                                 quoted_text="做 SEO 的是没有流量来源", note="")
        self.assertEqual(self._row()["state"], "changed")

    def test_note_added_after_extract_is_changed(self):
        self._extract()
        self.hl.note = "这句是我要的结论"
        self.hl.save(update_fields=["note"])
        self.assertEqual(self._row()["state"], "changed")

    def test_ledger_chars_match_inbox_chars(self):
        """字数口径也要同一份：旧指纹失效时会退字数比较，差一个数就永久 changed。"""
        self._extract()
        rec = ingest.read_ledger()["entry:%s" % self.SID]
        self.assertEqual(rec["chars"], self._row()["words_chars"])

    def test_sig_comes_from_signal_texts(self):
        """收件箱的指纹必须就是 signal_texts 的指纹（唯一那份定义）。"""
        texts = ingest.signal_texts("entry", self.SID)
        self.assertIn("热度最高的一批几乎不做 SEO", texts,
                      "没备注的划线原文也是信号，必须进指纹")
        self.assertNotIn("可能。", texts, "助手的回复不是我的痕迹，不能算信号")
        self.assertEqual(self._row()["sig"],
                         ingest.words_sig({"items": [{"text": t} for t in texts]}))

    def test_chat_annotation_resolves_to_article(self):
        """对话里的批注归到文章下：否则会多出一行点不开的 `chat:xx`，且字数两套口径。"""
        Annotation.objects.create(source_type="chat", source_id=str(self.msg.id),
                                  body="这里的口径和上一段不一致")
        with patch("core.sourceblock.load", return_value=BLOCK):
            rows = ingest.inventory()
        self.assertFalse([r for r in rows if r["source_type"] == "chat"],
                         "收件箱里出现了 chat:xx 这种点不开的来源")
        self.assertIn("这里的口径和上一段不一致", ingest.signal_texts("entry", self.SID))


class SetStatusTest(TestCase):
    def test_illegal_status_rejected(self):
        c = IngestCandidate.objects.create(
            batch="b", source_type="report", source_id="daily-2026-10-31",
            kind="decision", claim="x", quote="q")
        ok, err = ingest.set_status(c.id, "accpeted")     # 拼错
        self.assertFalse(ok)
        self.assertIn("非法状态", err)
        c.refresh_from_db()
        self.assertEqual(c.status, "pending")

    def test_accept_and_discard(self):
        c = IngestCandidate.objects.create(
            batch="b", source_type="report", source_id="daily-2026-10-31",
            kind="decision", claim="x", quote="q")
        self.assertTrue(ingest.set_status(c.id, "accepted")[0])
        c.refresh_from_db()
        self.assertEqual(c.status, "accepted")


@override_settings(WIKI_DIR=tempfile.mkdtemp(prefix="diting-ingest-view-"))
class IngestViewTest(TestCase):
    """真调视图：上下文能不能渲染、POST 能不能落库、CSRF 挡不挡。"""

    def setUp(self):
        ingest.title_of.cache_clear()
        # ★ 每条测试都从「没提炼过」开始：台账是文件、会跨测试残留，
        #   不清就会得到「这条用例在我这台机器上通过、在你那台上失败」。
        try:
            (ingest.wstore.root() / "meta" / "ingest.json").unlink()
        except OSError:
            pass
        my_conv(questions=["我认为这七格只适合 ToB，ToC 没有对应物。"])

    def test_page_renders_panel(self):
        """第十一步：首页不再渲染提炼过程；状态行与编译入口必须始终在。"""
        r = self.client.get("/wiki")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('id="compile"', html)
        self.assertIn("编译成草稿", html)
        self.assertIn("候选查询", html)
        # 提炼的中间态对用户隐身（改到 /wiki/candidates 只读查询页）
        self.assertNotIn("提炼候选", html)
        self.assertNotIn("还没提炼", html)

    def test_ingest_post_creates_candidates(self):
        """提炼端点仍可用（代码层 / 一条龙内部），产物必须真落库、反馈如实报数。

        ★ 第十一步：候选面板从首页下线，原「候选出现在页面 HTML 里」的断言
          没有了落点；语义改由「DB 真有这一条 + 反馈条说出条数」共同保证。
        """
        reply = json.dumps({"candidates": [
            {"kind": "decision", "claim": "七格只适合 ToB", "quote": "这七格只适合 ToB",
             "domain": "ai", "confidence": "medium"}]}, ensure_ascii=False)
        with patch("core.citeguard._call", return_value=(reply, "")), \
                patch("core.sourceblock.load", return_value=BLOCK):
            r = self.client.post("/wiki/ingest",
                                 {"source_type": "report", "source_id": "daily-2026-10-31"})
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("提炼出 1 条候选", html)
        self.assertEqual(IngestCandidate.objects.count(), 1)
        self.assertEqual(IngestCandidate.objects.first().claim, "七格只适合 ToB")

    def test_after_post_the_row_says_done(self):
        """点完提炼，台账里这一来源必须当场变成 done（不再催着再花一次钱）。

        ★ 2026-11-03 用户的真实抱怨就是「点完还是提炼后又写了」。
        ★ 第十一步：收件箱表格从首页下线，原「回给 htmx 的那一行 HTML」判据
          失去落点。语义不删，改验 `inventory()` 的 state（数据层真值）+ 反馈条。
          多造一条没写备注的划线仍然必要：指纹口径那次的 bug 只在「信号里含
          划线原文」时暴露，少了它用例在坏代码上照样绿。
        """
        Highlight.objects.create(source_type="report", source_id="daily-2026-10-31",
                                 quoted_text="七格是 ToB 的需求筛选模型", note="")
        reply = json.dumps({"candidates": [
            {"kind": "decision", "claim": "七格只适合 ToB", "quote": "这七格只适合 ToB",
             "domain": "ai", "confidence": "medium"}]}, ensure_ascii=False)
        with patch("core.citeguard._call", return_value=(reply, "")), \
                patch("core.sourceblock.load", return_value=BLOCK):
            r = self.client.post("/wiki/ingest",
                                 {"source_type": "report", "source_id": "daily-2026-10-31"})
        row = next(x for x in ingest.inventory()
                   if x["source_id"] == "daily-2026-10-31")
        self.assertEqual(row["state"], "done")
        self.assertIn("提炼出 1 条候选", r.content.decode())

    def test_candidate_status_post(self):
        c = IngestCandidate.objects.create(
            batch="b", source_type="report", source_id="daily-2026-10-31",
            kind="decision", claim="x", quote="q")
        r = self.client.post("/wiki/candidate", {"id": c.id, "status": "discarded"})
        self.assertEqual(r.status_code, 200)
        c.refresh_from_db()
        self.assertEqual(c.status, "discarded")

    def test_get_not_allowed(self):
        # 提炼会花钱，绝不能是 GET（会被预取/预览链接触发）
        self.assertEqual(self.client.get("/wiki/ingest").status_code, 405)
        self.assertEqual(self.client.get("/wiki/candidate").status_code, 405)


@override_settings(WIKI_DIR=tempfile.mkdtemp(prefix="diting-ingest-csrf-"))
class IngestCsrfTest(TestCase):
    """★ 必须用 enforce_csrf_checks=True：默认的测试客户端**不校验 CSRF**，
    于是「页面上点一下 403」这类 bug 单测永远抓不到（2026-10-31 已栽过一次）。

    ★★ 必须**覆盖 WIKI_DIR**：这个类会真的 POST 到 /wiki/ingest，而
      `extract()` 会写 `meta/ingest.json`。不覆盖 = 测试跑一次就把用户的
      **真实台账**改成测试库里的数据（2026-10-31 实际发生了：台账里出现了
      测试用例那 24 个字，页面上的真实来源被标成「提炼后又写了」）。
      凡是会写文件的视图，测试类一律要盖 WIKI_DIR —— 这是纪律，不是可选项。
    """

    def setUp(self):
        c = Client(enforce_csrf_checks=True)
        self.c = c
        # ★ 必须先有「我的字」，否则 /wiki 上一个表单都不渲染 ⇒ 拿不到 csrftoken
        #   cookie（这也说明：令牌是**表单渲出来时**才下发的，不是页面一开就有）。
        my_conv(questions=["我认为这七格只适合 ToB，ToC 没有对应物。"])

    def test_ingest_without_token_is_403(self):
        r = self.c.post("/wiki/ingest",
                        {"source_type": "report", "source_id": "daily-2026-10-31"})
        self.assertEqual(r.status_code, 403)

    def test_candidate_without_token_is_403(self):
        r = self.c.post("/wiki/candidate", {"id": 1, "status": "discarded"})
        self.assertEqual(r.status_code, 403)

    def test_with_token_passes_csrf(self):
        """带 token 的提炼 POST 必须放行。

        ★ 第十一步：首页删掉逐来源提炼表后，空闲首页可能一个 form 都不渲染，
          于是 `GET /wiki` 不再下发 csrftoken cookie ⇒ 靠 cookie 取 token 的老写法
          直接 KeyError。改成显式生成 token 并塞进 client，不再依赖页面结构。
        """
        from django.middleware.csrf import get_token
        r = self.c.get("/wiki")
        token = get_token(r.wsgi_request)
        self.c.cookies["csrftoken"] = token
        with patch("core.citeguard._call", return_value=('{"candidates": []}', "")), \
                patch("core.sourceblock.load", return_value=BLOCK):
            r = self.c.post("/wiki/ingest",
                            {"source_type": "report", "source_id": "daily-2026-10-31",
                             "csrfmiddlewaretoken": token})
        self.assertEqual(r.status_code, 200)


@override_settings(WIKI_DIR=tempfile.mkdtemp(prefix="diting-ingest-tpl-"))
class PresetTemplateTest(TestCase):
    """预设话术的**单一来源**守卫：模板渲染出来的按钮文案必须就是 Python 里那份。

    ★ 用真调视图（不是读源码）：源码里有 `data-wc-ask` 不代表渲染对；
      而且这里要的正是「模板 -> 页面」这条链的产物。
    """

    def test_chips_match_source_of_truth(self):
        # ★ 必须是**空会话**：有消息时模板走的是消息列表，快捷问题按钮只在
        #   空状态渲染。造一个带 autosend 消息的会话会让这条断言假失败
        #   （第一次写就踩了：模板里根本没有那个按钮）。
        my_conv(questions=[], presets=False)
        r = self.client.get("/wiki/chat?source_type=report&source_id=daily-2026-10-31")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        for _label, text in conv_mod.QUICK_ASKS:
            self.assertIn(f'data-wc-ask="{text}"', html)
        self.assertEqual(html.count("data-wc-ask="), len(conv_mod.QUICK_ASKS))

    def test_autosend_template_has_no_hardcoded_copy(self):
        from pathlib import Path
        src = Path(settings.BASE_DIR, "templates", "reader", "_entry_detail.html") \
            .read_text(encoding="utf-8")
        self.assertNotIn("先给 3-5 条要点", src,
                         "自动总结那句话不能在模板里写死：它是分诊的判据，必须取自 "
                         "core.conversation.AUTOSEND_TEXT")
