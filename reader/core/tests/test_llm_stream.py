"""流式 LLM 出口（llm.chat_stream / costreport.chat_recorded_stream /
citeguard._call_stream）的行为判据。2026-11-19。

★★ 核心铁律在流式下最容易破的两处，这里钉死：
  1. usage 靠 `stream_options.include_usage` 从**末帧**拿 —— 拿不到就记账记 0，
     成本账失真（非流式是一次性 JSON，流式是逐帧，这里是最容易漏的差异）。
  2. 预算闸必须在调模型**前**判，闸下不花钱不记账 —— 流式不能成为绕过预算的后门。
· tool_calls 的 arguments 是分片给的，必须按 index 拼回去，形状与非流式一致。
· tool_calls/正文分帧到达时不能丢，末帧（choices=[]）只有 usage 也要吞掉。

打桩在 urlopen（真实 HTTP 边界），不碰 llm.chat / chat_recorded / citeguard._call
这三个被几十处钉死的函数。
"""
import http.client
import json
from unittest import mock

from django.test import SimpleTestCase, TestCase

from core import citeguard, costreport as cr, llm
from core.models import AppSetting, LlmUsage


class _FakeResponse:
    """把一串 dict 伪造成 SSE 字节行（data: {...}\\n\\n），末帧可带 usage。"""

    def __init__(self, frames):
        self._lines = []
        for f in frames:
            self._lines.append(("data: " + json.dumps(f, ensure_ascii=False)).encode())
        self._lines.append(b"data: [DONE]")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._lines)


def _frames(text_parts, usage=None, tool_parts=None, finish="stop"):
    out = []
    for p in text_parts:
        out.append({"choices": [{"delta": {"content": p}}]})
    for i, chunks in enumerate(tool_parts or []):
        for j, frag in enumerate(chunks):
            piece = {"index": i}
            if j == 0:
                piece["id"] = "call_%d" % i
                piece["type"] = "function"
                piece["function"] = {"name": frag[0], "arguments": frag[1]}
            else:
                piece["function"] = {"arguments": frag[1]}
            out.append({"choices": [{"delta": {"tool_calls": [piece]}}]})
    last = {"choices": [{"delta": {}, "finish_reason": finish}]}
    if usage is not None:
        last["usage"] = usage
    out.append(last)
    return out


# 字段名对齐 DeepSeek 真实返回（与非流式 chat() 读的同一组键）。
USAGE = {"prompt_tokens": 100, "completion_tokens": 20,
         "prompt_cache_hit_tokens": 40, "prompt_cache_miss_tokens": 60}


class LlmStreamTest(SimpleTestCase):
    def test_text_delta_and_usage_last_frame(self):
        """文字逐片回调，usage 从末帧拿到，四元组形状与非流式一致。"""
        got = []
        frames = _frames(["你好", "，世界"], usage=USAGE)
        with mock.patch.object(llm, "provider_cfg", return_value={
                "provider": "deepseek", "base": "http://x", "model": "m", "key": "k"}), \
                mock.patch.object(llm.urllib.request, "urlopen",
                                  return_value=_FakeResponse(frames)) as opener:
            text, usage, err, tries = llm.chat_stream(
                [{"role": "user", "content": "q"}], on_delta=got.append)
        self.assertEqual(text, "你好，世界")
        self.assertEqual(got, ["你好", "，世界"])
        self.assertEqual(err, "")
        self.assertEqual(tries, 1)
        self.assertEqual(usage["prompt_tokens"], 100)
        self.assertEqual(usage["completion_tokens"], 20)
        self.assertEqual(usage["cached_tokens"], 40)
        self.assertEqual(usage["finish_reason"], "stop")
        # ★ 铁律：payload 必须开 include_usage 且 stream=True
        payload = json.loads(opener.call_args[0][0].data)
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["stream_options"], {"include_usage": True})

    def test_tool_arguments_concatenated_by_index(self):
        """tool_calls 的 arguments 分片必须按 index 拼回完整 JSON。"""
        frames = _frames([], usage=USAGE,
                         tool_parts=[[("merge_into", '{"a":'), (None, "1}")]])
        with mock.patch.object(llm, "provider_cfg", return_value={
                "provider": "deepseek", "base": "http://x", "model": "m", "key": "k"}), \
                mock.patch.object(llm.urllib.request, "urlopen",
                                  return_value=_FakeResponse(frames)):
            text, usage, err, _ = llm.chat_stream([])
        self.assertEqual(err, "")
        tcs = usage["tool_calls"]
        self.assertEqual(len(tcs), 1)
        self.assertEqual(tcs[0]["function"]["name"], "merge_into")
        self.assertEqual(tcs[0]["function"]["arguments"], '{"a":1}')
        self.assertEqual(tcs[0]["id"], "call_0")

    def test_no_key_fails_before_http(self):
        with mock.patch.object(llm, "provider_cfg", return_value={
                "provider": "deepseek", "base": "http://x", "model": "m", "key": ""}), \
                mock.patch.object(llm.urllib.request, "urlopen") as opener:
            _, _, err, _ = llm.chat_stream([])
        self.assertIn("API_KEY", err)
        opener.assert_not_called()


class StreamBudgetAndRecordTest(TestCase):
    def test_gate_blocks_without_call_or_record(self):
        """闸下：不调模型、不落账（流式不能成为绕预算的后门）。"""
        AppSetting.objects.create(key="wiki_budget_cny", value="0.01")
        LlmUsage.objects.create(kind="wiki_chat", provider="deepseek", model="m",
                                prompt_tokens=1, completion_tokens=1,
                                cost_cny=1)  # 已远超 0.01
        with mock.patch.object(llm, "chat_stream") as stream:
            res = cr.chat_recorded_stream(
                "wiki_chat", [], pool=cr.POOL_WIKI, on_delta=lambda _: None)
        self.assertTrue(res.budget_stop)
        stream.assert_not_called()
        self.assertEqual(LlmUsage.objects.filter(kind="wiki_chat").count(), 1)

    def test_success_records_real_usage(self):
        """流式成功后按末帧 usage 落一行账，含真实 token 数。"""
        with mock.patch.object(llm, "chat_stream", return_value=(
                "回答", USAGE, "", 1)) as stream:
            res = cr.chat_recorded_stream(
                "wiki_chat", [], pool=None, on_delta=lambda _: None)
        self.assertEqual(res.text, "回答")
        stream.assert_called_once()
        row = LlmUsage.objects.get()
        self.assertEqual(row.prompt_tokens, 100)
        self.assertEqual(row.completion_tokens, 20)
        self.assertTrue(row.ok)


class CiteguardStreamTest(SimpleTestCase):
    def test_call_stream_no_retry_and_stats(self):
        """_call_stream：一轮到底不翻倍重试，统计口径与非流式一致。"""
        stats = citeguard.fresh_stats()
        with mock.patch.object(cr, "chat_recorded_stream", return_value=cr.ChatResult(
                "正文", USAGE, "", False, None)) as rec:
            text, err = citeguard._call_stream(
                "wiki_chat", "", "", stats, on_delta=lambda _: None,
                pool=cr.POOL_WIKI, tag="wiki 对话")
        self.assertEqual(text, "正文")
        self.assertEqual(err, "")
        self.assertEqual(stats["calls"], 1)
        self.assertEqual(stats["tokens"], 120)
        self.assertEqual(rec.call_count, 1)  # ★ 不重试

    def test_call_stream_carries_tool_calls_to_stats(self):
        """★ 流式工具轮：模型想调的工具必须写回 stats。

        2026-11-22 实测事故：审批对话说「D138合并到D137」，LLM 回了 tool_calls，
        但 `_call_stream` 只带 text 回来 ⇒ `wiki_loop.run` 当成「模型没有返回内容」，
        工具一次都没执行，用户只看到「没起作用」。非流式 `_call` 一直有写，流式漏了。
        """
        tc = {"id": "c1", "type": "function",
              "function": {"name": "set", "arguments": '{"target":"merge"}'}}
        stats = citeguard.fresh_stats()
        with mock.patch.object(cr, "chat_recorded_stream", return_value=cr.ChatResult(
                "", USAGE, "", False, None, (tc,))):
            text, err = citeguard._call_stream(
                "wiki_chat", "", "", stats, on_delta=lambda _: None,
                tools=[{"type": "function"}])
        self.assertEqual(err, "")
        self.assertEqual(stats["tool_calls"], [tc],
                         "工具被吞 = 流式审批对话里任何合并/拆页都不会发生")

    def test_call_stream_gate_short_circuit(self):
        stats = citeguard.fresh_stats()
        stats["budget_stop"] = True
        with mock.patch.object(cr, "chat_recorded_stream") as rec:
            text, err = citeguard._call_stream(
                "wiki_chat", "", "", stats, on_delta=lambda _: None)
        self.assertEqual(text, "")
        self.assertIn("预算", err)
        rec.assert_not_called()


class LlmTransportTest(SimpleTestCase):
    """HTTP chunked 响应被上游提前断开时抛的 IncompleteRead（2026-11-23 实测）。

    它不是 `urllib.error.URLError` 的子类，而是 `http.client.HTTPException`：
    从 llm.chat / chat_stream 的 except 列表漏出去 ⇒ 客户端重试不执行、
    `costreport.chat_recorded` 的记账行不写、scheduler._safe 把整轮 triage 打断。
    """

    def test_chat_retries_incomplete_read_and_returns_error_tuple(self):
        err = http.client.IncompleteRead(b"")
        with mock.patch("core.llm.time.sleep"), \
                mock.patch("core.llm.urllib.request.urlopen",
                           side_effect=[err, err, err]) as op:
            text, usage, error, tries = llm.chat(
                [{"role": "user", "content": "hi"}], retries=3)
        self.assertEqual(op.call_count, 3, "漏捕获时第一次就抛，根本不会重试")
        self.assertEqual(text, "")
        self.assertEqual(usage, {})
        self.assertIn("IncompleteRead", error)
        self.assertEqual(tries, 3)

    def test_chat_recovers_after_incomplete_read(self):
        """传输断一次、第二次成功：应返回正常四元组，tries=2 让账本记 1 次重试。"""
        err = http.client.IncompleteRead(b"")
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": "好了"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2}}).encode()
        with mock.patch("core.llm.time.sleep"), \
                mock.patch("core.llm.urllib.request.urlopen",
                           side_effect=[err, ok]):
            text, usage, error, tries = llm.chat(
                [{"role": "user", "content": "hi"}], retries=3)
        self.assertEqual((text, error, tries), ("好了", "", 2))

    def test_chat_stream_returns_error_tuple_on_incomplete_read(self):
        err = http.client.IncompleteRead(b"")
        with mock.patch("core.llm.time.sleep"), \
                mock.patch("core.llm.urllib.request.urlopen", side_effect=err):
            text, usage, error, tries = llm.chat_stream(
                [{"role": "user", "content": "hi"}], on_delta=lambda _: None)
        self.assertEqual(text, "")
        self.assertIn("IncompleteRead", error)
        self.assertEqual(usage, {})
