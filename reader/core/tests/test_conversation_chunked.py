"""「问 AI」长文分片（2026-11-15）：正文不再截断，均衡切片 → 逐片取材 → 汇总作答。

用户报的问题原话：「文章的问ai有『正文超 6000 字被截断』的问题，这就导致长文
总结不可用了呀」。原来 `ask()` 只有一条路：`_material()` 把正文 `clip_quotes(6000)`
后发一次调用 —— 超过 6000 字的后半篇，模型**从来没见过**。

这一组判据守的是「长文确实被读完」，不是「代码长得像分片」：

  · `split_body()` 是正文的**精确划分**（拼回去 == 原文）：为了「整齐」丢几个字，
    就变成另一种更难发现的截断。
  · 每片都要有**自己的引文边界**：硬切点落在【引文】中间时，中间那片一个标记
    都没有，模型会把被引者的话当成作者自己说的（2026-11-02 那类归属事故）。
  · 短文路径**零额外调用**：92% 的提问不该因为修这个 bug 变贵。
  · 某片失败要「继续但声明缺口」：三处都说（笔记 / 汇总 prompt / 回答开头），
    才算没假装读完。
  · 预算/调用上限触顶后**不再空转**：后面每片都不可能成功，继续调用只是多花钱。
"""
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from core import conversation as conv
from core import reporttext
from core.textutil import to_plain


def make_blk(n_para=20, para_len=100, title="长文"):
    """造一篇「有段落边界」的长文（真实 RSS 的样子：`<p>` 分段）。"""
    paras = ["第%d段：" % i + "材" * para_len for i in range(1, n_para + 1)]
    html = "".join("<p>%s</p>" % p for p in paras)
    return {"title": title, "html": html, "text": to_plain(html)}


def fake_call(script):
    """按调用顺序回放 (text, err)；用完就重复最后一项。

    返回 (fake, calls)：`calls` 里存每次调用的 messages + kwargs，供断言
    「哪一次是分片、哪一次是汇总、带了什么预算」。
    """
    calls = []

    def fake(kind, system, user, stats, **kw):
        calls.append({"kind": kind, "messages": kw["messages"],
                      "max_tokens": kw["max_tokens"], "tag": kw.get("tag", ""),
                      "max_calls": kw.get("max_calls")})
        text, err = script[len(calls) - 1] if len(calls) - 1 < len(script) \
            else script[-1]
        if err == "__BUDGET__":
            # 真 `_call` 在预算撞顶时会把 stats 点亮，后续调用据此短路。
            stats["budget_stop"] = True
            return "", "知识库池今日预算（¥1）已用尽"
        return text, err

    return fake, calls


#: `ask()` 对片大小有 `max(500, ...)` 下限，用例必须按同一个数算片数，
#: 否则「我算 6 片、它真跑 5 片」，断言全错位（本轮真踩过）。
CHUNK_CHARS = 500


def chunk_calls(calls):
    return [c for c in calls if "分片取材" in c["tag"]]


def reduce_calls(calls):
    return [c for c in calls if "汇总作答" in c["tag"]]


def extracted_chunk_text(call):
    """从分片材料里把那片正文原样取回来（验证「精确划分」用）。"""
    sysmsg = call["messages"][0]["content"]
    tail = sysmsg.split("## 正文（全文第", 1)[1]
    return tail.split("\n", 1)[1]


class SplitBodyTest(SimpleTestCase):
    def test_short_body_is_one_chunk(self):
        self.assertEqual(conv.split_body("短文。", 6000, 6), ["短文。"])

    @override_settings()
    def test_partition_is_exact(self):
        blk = make_blk()
        body = reporttext.body_text(blk)
        chunks = conv.split_body(body, 400, 6)
        self.assertGreater(len(chunks), 1, "这篇本来就该分片，否则用例白跑")
        self.assertEqual("".join(chunks), body, "分片必须拼回原文，一个字都不能丢")

    def test_balanced_when_boundaries_exist(self):
        blk = make_blk()
        body = reporttext.body_text(blk)
        chunks = conv.split_body(body, 400, 6)
        sizes = [len(c) for c in chunks]
        target = len(body) / len(chunks)
        for s in sizes:
            self.assertLess(abs(s - target), target * 0.5,
                            "片大小离均值太远 = 没均衡：%s" % sizes)

    def test_cap_never_drops_tail(self):
        """★ 片数上限是「调用数闸」，不是「正文长度闸」：到上限就把每片加长。

        这条是这次 bug 的正面判据：截断的本质就是「取前 N 字，后面不要了」。
        """
        body = "字" * 20000          # 没有任何段落边界 ⇒ 只能硬切
        chunks = conv.split_body(body, 500, 3)
        self.assertEqual(len(chunks), 3)
        self.assertEqual("".join(chunks), body)
        self.assertEqual(len(chunks[-1]), 6666, "最后一片不能被截短")

    def test_unterminated_quote_is_padded_per_chunk(self):
        """硬切落在引文内部时，每片都要自带【引文】…【/引文】。

        ★ 只按本片内标记数配对会得到「0 开 0 闭 = 没问题」的假平衡 ——
          被引者的话就变成作者自己说的。所以判据看的是**每一片**都平衡。
        ★ 用「没有闭合标记的引文」构造：正常闭合的引文，其闭合处一定是候选
          切点，切点永远选在它上面，压根切不进引文中间（`split_body` 的候选集
          就是这么设计的）。只有 `body_text()` 吐出未闭合引文时才会真硬切进去。
        """
        body = "前言。" + "【引文】" + "引" * 900    # 故意没有【/引文】
        chunks = conv.split_body(body, 200, 3)
        self.assertGreater(len(chunks), 1)
        pos = 0
        mid_seen = False
        for chunk in chunks:
            starts_inside = conv._quote_depth(body[:pos]) > 0
            pos += len(chunk)
            out = conv._balance_quotes(chunk, starts_inside)
            self.assertEqual(out.count("【引文】"), out.count("【/引文】"),
                             "这片引文边界不平衡：%r" % out[:40])
            if starts_inside and "【引文】" not in chunk:
                mid_seen = True
                self.assertTrue(out.startswith("【引文】"))
                self.assertTrue(out.endswith("【/引文】"))
        self.assertTrue(mid_seen, "用例没切到引文内部，判据就是空的")

    def test_middle_piece_without_context_would_leak(self):
        """反证：不告诉它「本片从引文中间开始」，这段话看起来就是作者说的。"""
        middle = "引文正中间的一段话"
        self.assertNotIn("【引文】", conv._balance_quotes(middle))
        out = conv._balance_quotes(middle, starts_inside=True)
        self.assertTrue(out.startswith("【引文】"))
        self.assertTrue(out.endswith("【/引文】"))

    def test_quote_depth(self):
        self.assertEqual(conv._quote_depth("作者的话"), 0)
        self.assertEqual(conv._quote_depth("【引文】别人的话"), 1)
        self.assertEqual(conv._quote_depth("【引文】别人【/引文】"), 0)


class ChunkedAskTest(TestCase):
    def ask(self, script, blk=None, question="总结这篇", **over):
        """跑一次 ask()，返回 (res, calls)。"""
        blk = blk or make_blk()
        fake, calls = fake_call(script)
        with mock.patch("core.conversation.sourceblock.load", return_value=blk), \
                mock.patch("core.conversation.citeguard._call",
                           side_effect=fake), \
                override_settings(WIKI_CHAT_ARTICLE_CHARS=CHUNK_CHARS,
                                  WIKI_CHAT_MAX_CHUNKS=6, **over):
            res = conv.ask("entry", "12", question)
        return res, calls

    def n_chunks(self, blk=None):
        blk = blk or make_blk()
        return len(conv.split_body(reporttext.body_text(blk), CHUNK_CHARS, 6))

    def test_short_article_still_single_call(self):
        """短文（92% 的提问）一个 token 都不多花。"""
        short = {"title": "短文", "text": "只有一句话。"}
        fake, calls = fake_call([("短文回答", "")])
        with mock.patch("core.conversation.sourceblock.load", return_value=short), \
                mock.patch("core.conversation.citeguard._call",
                           side_effect=fake), \
                override_settings(WIKI_CHAT_ARTICLE_CHARS=6000):
            res = conv.ask("entry", "12", "总结这篇")
        self.assertTrue(res["ok"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(res["reply"], "短文回答", "短文不该有分片提示前缀")
        self.assertIn("## 正文", calls[0]["messages"][0]["content"])

    def test_long_article_calls_each_chunk_plus_one_reduce(self):
        n = self.n_chunks()
        script = [("笔记%d" % i, "") for i in range(1, n + 1)] + [("汇总回答", "")]
        res, calls = self.ask(script)
        self.assertTrue(res["ok"])
        self.assertEqual(len(chunk_calls(calls)), n)
        self.assertEqual(len(reduce_calls(calls)), 1)
        self.assertEqual(len(calls), n + 1)
        # 分片调用必须打上「第 i/n 片」，否则日志里看不出卡在哪
        tags = [c["tag"] for c in chunk_calls(calls)]
        self.assertEqual(tags[0], "wiki 对话 · 分片取材 1/%d" % n)
        self.assertEqual(tags[-1], "wiki 对话 · 分片取材 %d/%d" % (n, n))

    def test_whole_body_is_actually_read(self):
        """★ 核心判据：把所有分片里的正文拼起来 == 全文（不是「前 6000 字」）。"""
        blk = make_blk()
        body = reporttext.body_text(blk)
        n = self.n_chunks(blk)
        script = [("笔记", "")] * n + [("汇总", "")]
        _res, calls = self.ask(script, blk=blk)
        joined = "".join(extracted_chunk_text(c) for c in chunk_calls(calls))
        self.assertEqual(joined, body)
        self.assertIn(body[-20:], joined, "正文最后一段必须进过模型")

    def test_no_truncation_notice_anywhere(self):
        """老文案「以上为前 N 字」不许再出现在任何一条消息里。"""
        n = self.n_chunks()
        script = [("笔记", "")] * n + [("汇总", "")]
        _res, calls = self.ask(script)
        for c in calls:
            for m in c["messages"]:
                self.assertNotIn("以上为前", m["content"])
                self.assertNotIn("需要后面部分请让我展开", m["content"])

    def test_reply_note_declares_chunking(self):
        n = self.n_chunks()
        script = [("笔记", "")] * n + [("汇总回答", "")]
        res, _calls = self.ask(script)
        self.assertTrue(res["reply"].startswith("（全文 "))
        self.assertIn("已分 %d 片读完" % n, res["reply"])
        self.assertTrue(res["reply"].endswith("汇总回答"))

    def test_chunk_prompt_forbids_final_conclusion(self):
        """不写这条，每片都会各自给一份「总结」，汇总就成了拼 N 份互相矛盾的结论。"""
        n = self.n_chunks()
        script = [("笔记", "")] * n + [("汇总", "")]
        _res, calls = self.ask(script)
        first = chunk_calls(calls)[0]["messages"][0]["content"]
        self.assertIn("分片取材，不是最终作答", first)
        self.assertIn("不要下全文结论", first)
        self.assertIn("第 1/%d 片" % n, first)

    def test_chunk_notes_are_what_reduce_sees(self):
        n = self.n_chunks()
        script = [("第一片的笔记", "")] + [("其它片笔记", "")] * (n - 1) + [("汇总", "")]
        _res, calls = self.ask(script)
        red = reduce_calls(calls)[0]["messages"][0]["content"]
        self.assertIn("第一片的笔记", red)
        self.assertIn("全文取材笔记", red)
        self.assertIn("已按顺序均衡分成 %d 片" % n, red)

    def test_unterminated_quote_chunks_are_balanced_in_prompt(self):
        """★ 集成判据：`_ask_chunked` 必须把「本片起点在引文内」传给 `_balance_quotes`。

        只测 `_balance_quotes(starts_inside=True)` 是不够的 —— 那个参数完全可以
        在调用点被漏传，而单测照样绿。这里打断 `body_text`，让它吐出**没有闭合**
        的引文（真实 HTML 走不到，但 `body_text` 的边界算法出错时会），再看
        进模型的那几片是不是都自带边界。
        """
        body = "前言。" + "【引文】" + "引" * 2260      # 未闭合 ⇒ 只能硬切
        n = len(conv.split_body(body, CHUNK_CHARS, 6))
        script = [("笔记", "")] * n + [("汇总", "")]
        fake, calls = fake_call(script)
        with mock.patch("core.conversation.sourceblock.load",
                        return_value=make_blk()), \
                mock.patch("core.conversation.reporttext.body_text",
                           return_value=body), \
                mock.patch("core.conversation.citeguard._call",
                           side_effect=fake), \
                override_settings(WIKI_CHAT_ARTICLE_CHARS=CHUNK_CHARS,
                                  WIKI_CHAT_MAX_CHUNKS=6):
            res = conv.ask("entry", "12", "总结这篇")
        self.assertTrue(res["ok"])
        texts = [extracted_chunk_text(c) for c in chunk_calls(calls)]
        self.assertEqual(len(texts), n)
        for t in texts:
            self.assertEqual(t.count("【引文】"), t.count("【/引文】"),
                             "进模型的一片引文边界不平衡：%r" % t[:30])
        self.assertIn("【引文】", texts[1], "中间那片缺开边界 = 被引者的话变成作者说的")

    def test_failed_chunk_continues_and_declares_gap(self):
        """单片失败按用户拍板：继续，但三处都要写明缺口。"""
        n = self.n_chunks()
        script = [("笔记1", ""), ("", "超时"), ("笔记3", "")] + \
                 [("笔记", "")] * (n - 3) + [("汇总回答", "")]
        res, calls = self.ask(script)
        self.assertTrue(res["ok"], "一片失败不该让整次白等")
        self.assertIn("第 2 片没读到", res["reply"])
        red = reduce_calls(calls)[0]["messages"][0]["content"]
        self.assertIn("本片取材失败：超时", red)      # ① 笔记里
        self.assertIn("有 1 片取材失败", red)          # ② 汇总 prompt 里
        self.assertIn("不许假装读完了全文", red)

    def test_all_chunks_failed_gives_up(self):
        n = self.n_chunks()
        res, calls = self.ask([("", "超时")])
        self.assertFalse(res["ok"])
        self.assertIn("都失败", res["err"])
        self.assertEqual(len(reduce_calls(calls)), 0, "全失败还在汇总 = 白花钱")

    def test_budget_stop_does_not_keep_calling(self):
        """预算撞顶后不再一片片撞同一堵墙。"""
        n = self.n_chunks()
        script = [("笔记1", ""), ("", "__BUDGET__"), ("不该被调用", "")] + \
                 [("也不该", "")] * n
        res, calls = self.ask(script)
        self.assertTrue(res["ok"])
        # 2 次分片（第 2 片撞顶）+ 1 次汇总；第 3~n 片全部跳过
        self.assertEqual(len(chunk_calls(calls)), 2)
        self.assertEqual(len(calls), 3)
        self.assertIn("没有继续调用", reduce_calls(calls)[0]["messages"][0]["content"])

    def test_call_cap_stops_remaining_chunks(self):
        """err 文案是「达到单次调用上限」时同样要停手。"""
        n = self.n_chunks()
        script = [("笔记1", ""), ("", "达到单次调用上限")] + [("x", "")] * n
        _res, calls = self.ask(script)
        self.assertEqual(len(chunk_calls(calls)), 2)

    def test_highlights_and_annotations_only_in_reduce(self):
        """划线/批注只进汇总那一次 —— 每片都塞一遍等于把同一段话说 n 次。"""
        from core import marks as marks_mod
        from core.models import Annotation, Highlight
        Highlight.objects.create(source_type="entry", source_id="12",
                                 quoted_text="我划的这句", note="我的备注")
        Annotation.objects.create(source_type="entry", source_id="12",
                                  body="我的批注")
        n = self.n_chunks()
        script = [("笔记", "")] * n + [("汇总", "")]
        _res, calls = self.ask(script)
        for c in chunk_calls(calls):
            self.assertNotIn("我划的这句", c["messages"][0]["content"])
        red = reduce_calls(calls)[0]["messages"][0]["content"]
        self.assertIn("我划的这句", red)
        self.assertIn("我的备注", red)
        self.assertIn("我的批注", red)

    def test_chunk_and_reduce_share_one_budget_stats(self):
        """所有调用共用一份 stats：预算闸才可能在某片撞顶后立刻影响后续片。"""
        n = self.n_chunks()
        stats_seen = []

        def fake(kind, system, user, stats, **kw):
            stats_seen.append(id(stats))
            if "汇总作答" in kw.get("tag", ""):
                return "汇总", ""
            return "笔记", ""

        blk = make_blk()
        with mock.patch("core.conversation.sourceblock.load", return_value=blk), \
                mock.patch("core.conversation.citeguard._call",
                           side_effect=fake), \
                override_settings(WIKI_CHAT_ARTICLE_CHARS=CHUNK_CHARS,
                                  WIKI_CHAT_MAX_CHUNKS=6):
            conv.ask("entry", "12", "总结这篇")
        self.assertEqual(len(set(stats_seen)), 1, "分片与汇总必须是同一份 stats")

    def test_max_calls_allows_one_retry_per_call(self):
        """`_call` 默认 retry_on_empty ⇒ 每次调用最多花 2 次配额。

        给 `n+1` 会让「前面某片重试过一次」把后面几片误判成触顶（静默漏读）。
        """
        n = self.n_chunks()
        script = [("笔记", "")] * n + [("汇总", "")]
        _res, calls = self.ask(script)
        for c in calls:
            self.assertGreaterEqual(c["max_calls"], 2 * (n + 1))
