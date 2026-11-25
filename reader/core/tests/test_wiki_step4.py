"""第四步切片验收（第十三步形态 A 口径）：三层工具调用基建 + 审批对话 agent loop
+ 结论（polish）增量重编 + `set_polish` 素材守卫。

形态 A 定案：正文 = 纯 polish 一段，不含 `## 候选清单` / `## 通顺话` / 三块 /
双层标题；frontmatter `candidates` 是分组唯一真值，精确出处由折叠溯源区读
`IngestCandidate` 字段负责。`render_two_layer` 已删，`BODY_HEAD` / `POLISH_HEAD`
降级为只读兼容（认清零前老页）。分组变更后的「结论待重生成」标记是 frontmatter
`stale: true`（不再插 `> ⚠️` 进正文），由 `set_polish` 成功后才剥。

铁律（照用户当面纠正过的口径）：
  - **LLM 调用全程打桩**，桩打在**被调用点的命名空间**（`core.wiki_loop._call`，
    不是 `core.citeguard._call`）。三层链路单测另桩 `llm.chat` / `chat_recorded`。
  - 桩必须配「真被用上」反证：
      * wiki_loop 侧 → **产物断言**（.drafts/ 文件正文真出现桩返回的结论 /
        frontmatter `stale: true` 真被写、真被剥）+ 桩调用参数/次数断言；
      * 记账侧 → **LlmUsage 真落一行**（不是「没花钱」因为没走到出口）。
  - `WIKI_DIR` 一律 override 到临时目录，`IngestCandidate` 落测试库；不动真实库。
  - 断言只看可见文字 / 真实产物，不看源码里有没有某个字符串。
  - 每条守卫至少一条「守卫自己会响」的对照测试（正向 + 放宽判据后行为改变）。
  - ⚠️K：`set_polish` 的素材守卫读素材里的 `#id` 与 frontmatter 比对，
    不从「要写回的正文」取（正文没有清单，空集合会 `[] == []` 假绿）。

与规格 §六 验收清单逐条对应，见各测试 docstring 的 【验收 x】。
"""
import io
import json
import shutil
import tempfile
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from core import citeguard
from core import costreport as cr
from core import llm
from core import wiki as wstore
from core import wiki_agent as wa
from core import wiki_loop as wl
from core.models import IngestCandidate, LlmUsage

DOMAIN = "invest"


def _tc(name, args, cid="call_1"):
    """一条 OpenAI 形状的 tool_call。"""
    return {"id": cid, "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps(args, ensure_ascii=False)}}


# ================================================================== 三层基建
class _Resp:
    """`urllib.request.urlopen` 的桩：支持 `with ... as r` + `json.load(r)`。"""

    def __init__(self, data):
        self._buf = io.BytesIO(json.dumps(data).encode("utf-8"))

    def read(self, *a, **kw):
        return self._buf.read(*a, **kw)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _openai_resp(*, content="", tool_calls=None, finish="stop", usage=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg, "finish_reason": finish}],
            "usage": ({"prompt_tokens": 10, "completion_tokens": 5}
                      if usage is None else usage)}


class LLMToolCallTest(SimpleTestCase):
    """`llm.chat`：tools 进 payload、tool_calls 出 norm、有 tool_calls 跳过空判。"""

    def _chat(self, data, tools):
        cfg = {"provider": "deepseek", "base": "http://x", "model": "m", "key": "k"}
        captured = {}

        def fake_open(req, timeout=None):
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return _Resp(data)

        with patch.object(llm, "provider_cfg", return_value=cfg), \
                patch.object(llm.urllib.request, "urlopen", side_effect=fake_open):
            out = llm.chat([{"role": "user", "content": "x"}],
                           tools=tools, retries=1)
        return out, captured

    def test_tools_really_in_payload(self):
        """【验收 1】传 tools 后 payload 真的带 tools（桩 urlopen 断言请求体）。"""
        out, cap = self._chat(_openai_resp(content="正文"), wl.TOOLS)
        self.assertEqual(out[2], "")
        self.assertIn("tools", cap["payload"])
        # ★ 别把工具名单写死：2026-11-13 新增 merge_into 时，写死的 3 项断言
        #   立刻假红。这里验的是「payload 真的原样透传了 wl.TOOLS」，
        #   并单独钉住新工具必须在给模型的 schema 里（空集合不假绿）。
        names = [t["function"]["name"] for t in cap["payload"]["tools"]]
        self.assertEqual(names, [t["function"]["name"] for t in wl.TOOLS])
        self.assertIn("merge_into", names, "并入已入库页的工具必须进 schema")

    def test_only_tool_calls_is_not_empty_error(self):
        """★【验收 2 / 反证 1 的正向】只回 tool_calls、content 空、usage 空，
        不走空响应错误分支。删掉 `and not tool_calls` 判据，这条必变红。"""
        tcs = [_tc("read", {"scope": "drafts"})]
        data = _openai_resp(content="", tool_calls=tcs, finish="tool_calls", usage={})
        out, _ = self._chat(data, wl.TOOLS)
        self.assertEqual(out[2], "", "有 tool_calls 不该被判成空响应错误")
        self.assertEqual(out[1].get("tool_calls"), tcs)

    def test_tool_calls_with_usage_is_ok_too(self):
        """真实工具调用（usage 有 token）同样成功，norm 带出 tool_calls。"""
        tcs = [_tc("set", {"target": "title", "payload": {}})]
        out, _ = self._chat(_openai_resp(content="", tool_calls=tcs,
                                          finish="tool_calls"), wl.TOOLS)
        self.assertEqual(out[2], "")
        self.assertEqual(out[1].get("tool_calls"), tcs)

    def test_true_empty_still_errors(self):
        """★ 守卫自己会响（对照）：既无 content 也无 usage 也无 tool_calls
        仍是空响应错误。证明「跳过空判」只跳 tool_calls，没有把真空也放过。"""
        data = {"choices": [{"message": {"role": "assistant", "content": ""},
                             "finish_reason": "stop"}], "usage": {}}
        out, _ = self._chat(data, None)
        self.assertIn("空响应", out[2])

    def test_no_tools_callers_unchanged(self):
        """【验收 3】不传 tools 的调用方：payload 不带 tools，行为不变。"""
        out, cap = self._chat(_openai_resp(content="正文"), None)
        self.assertEqual(out[0], "正文")
        self.assertEqual(out[2], "")
        self.assertNotIn("tools", cap["payload"])


class ToolCallChainTest(TestCase):
    """`chat_recorded` / `citeguard._call`：tools 透传 + tool_calls 别丢 + 记账反证。"""

    def test_chat_recorded_passes_tools_carries_calls_and_records(self):
        """【验收 1/2 链路】tools 透传到 llm.chat，ChatResult 带出 tool_calls。
        ★「真被用上」反证：真落一行 LlmUsage（不是压根没走到记账出口）。"""
        tcs = [_tc("read", {"scope": "drafts"})]
        norm = {"prompt_tokens": 8, "completion_tokens": 4,
                "finish_reason": "tool_calls", "tool_calls": tcs}
        with patch.object(llm, "chat", return_value=("", norm, "", 1)) as chat:
            res = cr.chat_recorded("wiki_chat", [{"role": "user", "content": "x"}],
                                   pool=cr.POOL_WIKI, tools=wl.TOOLS)
        self.assertEqual(chat.call_args.kwargs.get("tools"), wl.TOOLS)
        self.assertEqual(list(res.tool_calls), tcs, "ChatResult 别把 tool_calls 丢了")
        self.assertEqual(LlmUsage.objects.filter(kind="wiki_chat").count(), 1)

    def test_citeguard_carries_tool_calls_and_skips_double_retry(self):
        """★ 规格 §2.3 坑：有 tool_calls、content 空、finish_reason=length
        不算「推理吃光预算」，不翻倍重试，直接把 tool_calls 交给 loop。"""
        tcs = [_tc("set", {"target": "title", "payload": {}})]
        usage = {"finish_reason": "length", "prompt_tokens": 100,
                 "completion_tokens": 100, "tool_calls": tcs}
        res = cr.ChatResult("", usage, "", False, None, tuple(tcs))
        with patch.object(cr, "chat_recorded", return_value=res) as rec:
            stats = citeguard.fresh_stats()
            text, err = citeguard._call(
                "wiki_chat", "", "", stats,
                messages=[{"role": "user", "content": "x"}], tools=wl.TOOLS,
                max_tokens=100, retry_on_empty=True)
        self.assertEqual(rec.call_args.kwargs.get("tools"), wl.TOOLS)
        self.assertEqual(rec.call_count, 1, "有 tool_calls 不翻倍重试")
        self.assertEqual(stats["tool_calls"], tcs)
        self.assertEqual((text, err), ("", ""))

    def test_true_empty_still_doubles(self):
        """对照守卫：无 tool_calls 的真·空仍走翻倍重试（逻辑没被误删）。"""
        res = cr.ChatResult("", {"finish_reason": "length", "prompt_tokens": 100,
                                  "completion_tokens": 100}, "", False, None)
        with patch.object(cr, "chat_recorded", return_value=res) as rec:
            citeguard._call("wiki_chat", "", "", citeguard.fresh_stats(),
                            messages=[{"role": "user", "content": "x"}],
                            max_tokens=100, retry_on_empty=True)
        self.assertEqual(rec.call_count, 2)

    def test_polish_kind_is_inside_wiki_pool(self):
        """polish 复用 wiki_compile kind：真调记账出口，断言 kind 落在 POOL_WIKI。
        ★ 顺带证明「没花钱」不是压根没走到 —— LlmUsage 真落了一行。"""
        with patch.object(llm, "chat", return_value=("通顺话", {"prompt_tokens": 5}, "", 1)):
            res = cr.chat_recorded(wl.KIND_POLISH, [{"role": "user", "content": "x"}],
                                   pool=cr.POOL_WIKI)
        self.assertFalse(res.budget_stop)
        self.assertIn(wl.KIND_POLISH, cr.POOL_KINDS[cr.POOL_WIKI])
        self.assertEqual(LlmUsage.objects.filter(kind=wl.KIND_POLISH).count(), 1)


# ================================================================== wiki_loop
class WikiLoopBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-wiki-step4-")
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

    def mk_cand(self, n, status="pending"):
        return IngestCandidate.objects.create(
            batch="t", source_type="entry", source_id="123", kind="decision",
            claim=f"判断{n}", quote=f"我的原话{n}", domain=DOMAIN,
            confidence="medium", status=status)

    def mk_draft(self, slug, ids, body=None):
        text = ("---\n"
                f"title: {slug}\n"
                f"domain: {DOMAIN}\n"
                "sources: [entry:123]\n"
                "updated: 2026-11-05\n"
                "confidence: medium\n"
                "tags: []\n"
                f"candidates: [{', '.join(str(i) for i in ids)}]\n"
                "---\n\n"
                + (body if body is not None else "## 我的判断\n\n- 一条判断\n"))
        (wstore.root() / ".drafts" / f"{slug}.md").write_text(text, encoding="utf-8")
        return text

    def read_draft(self, slug):
        return (wstore.root() / ".drafts" / f"{slug}.md").read_text(encoding="utf-8")


class WikiLoopRunTest(WikiLoopBase):
    def test_run_stream_branch_executes_tools(self):
        """★ SSE 审批对话（on_delta）里模型调工具时，工具真执行、不留空回复。

        2026-11-22「D138合并到D137」没反应：下层 `_call_stream` 漏把 tool_calls
        带回来，`run()` 的流式分支把工具轮当「模型没有返回内容」。此测试钉住
        「流式分支照样执行工具 + 逐字增量照吐」，防止只修下层又把这里改回去。
        """
        c = self.mk_cand(1)
        self.mk_draft("p", [c.id])
        set_call = _tc("set", {"target": "title",
                               "payload": {"slug": "p", "title": "流式改的标题"}}, "c1")
        deltas = []
        turns = [0]

        def fake_stream(kind, messages, on_delta, *, tools=None, tag="", label=""):
            turns[0] += 1
            if turns[0] == 1:
                return "", [set_call], ""       # 工具轮：content 空、只有 tool_calls
            on_delta("已改好标题。")              # 最终答复轮：逐字吐
            return "已改好标题。", [], ""

        with patch.object(wl, "_call_stream", side_effect=fake_stream) as stream:
            res = wl.run("把 p 的标题改了", on_delta=deltas.append)
        self.assertEqual(stream.call_count, 2, "工具轮 + 最终答复轮")
        self.assertTrue(res["ok"], res["err"])
        self.assertEqual([a["tool"] for a in res["actions"]], ["set"])
        fm = wstore.parse_front(self.read_draft("liu-shi-gai-de-biao-ti"))[0]
        self.assertEqual(fm["title"], "流式改的标题", "工具必须真落盘")
        self.assertTrue(any("已改好" in d for d in deltas), "最终答复仍要逐字吐")

    def test_run_loops_tools_backfills_results_and_reports(self):
        """★【验收 4】run 真循环 read/set：桩 `_call`，断言 tools 参数 +
        循环轮次 + 工具结果回填。★ 产物断言（真被用上）：set 真改了 .drafts/。"""
        c = self.mk_cand(1)
        self.mk_draft("p", [c.id])
        read_call = _tc("read", {"scope": "drafts"}, "c1")
        set_call = _tc("set", {"target": "title",
                               "payload": {"slug": "p", "title": "新标题"}}, "c2")
        script = [("", [read_call], ""),
                  ("", [set_call], ""),
                  ("已把标题改为「新标题」。", [], "")]
        with patch.object(wl, "_call", side_effect=script) as call:
            res = wl.run("把 p 的标题改成新标题")
        self.assertEqual(call.call_count, 3, "两轮工具 + 一轮最终回复")
        for cargs in call.call_args_list:
            self.assertEqual(cargs.kwargs.get("tools"), wl.TOOLS,
                             "每轮 loop 都把 TOOLS 透传给出口")
        # 工具结果回填：第二轮 messages 里含 read 的结果（role=tool）
        second_msgs = call.call_args_list[1].args[1]
        self.assertTrue(any(m["role"] == "tool" and "drafts" in m["content"]
                            for m in second_msgs))
        # 产物：set 真的改了文件（不是「没花钱」因为压根没走到）
        # ★ title 改动同步改 slug（中文标题走拼音）：p → xin-biao-ti
        fm = wstore.parse_front(self.read_draft("xin-biao-ti"))[0]
        self.assertEqual(fm["title"], "新标题")
        self.assertTrue(res["ok"])
        self.assertEqual(res["reply"], "已把标题改为「新标题」。")
        self.assertEqual([a["tool"] for a in res["actions"]], ["read", "set"])
        # user + assistant 都落库
        conv = wa.get_or_create_approval_conversation()
        self.assertTrue(conv.messages.filter(role="user").exists())
        self.assertTrue(conv.messages.filter(role="assistant").exists())

    def test_whitelist_unknown_tool_is_ignored(self):
        """★【验收 5 / 反证 2 正向】非 read/set 的工具名被忽略并回「未知工具」。"""
        unknown = _tc("rm_rf", {"path": "/"}, "c1")
        script = [("", [unknown], ""), ("没有执行越界工具。", [], "")]
        with patch.object(wl, "_call", side_effect=script) as call:
            res = wl.run("删掉所有文件")
        a = res["actions"][0]
        self.assertFalse(a["ok"])
        self.assertIn("未知工具", a["result"]["err"])
        second_msgs = call.call_args_list[1].args[1]
        self.assertTrue(any(m["role"] == "tool" and "未知工具" in m["content"]
                            for m in second_msgs), "下一轮 LLM 能看到被拒结果")

    def test_guard_whitelist_itself_will_fire(self):
        """★ 守卫自己会响：放宽白名单，同一条未知工具不再被判「未知工具」。
        正向（白名单在⇒忽略）+ 反向（放宽⇒执行）都验才算数。"""
        self.mk_draft("p", [self.mk_cand(1).id])
        unknown = _tc("dangerous", {"target": "title",
                                    "payload": {"slug": "p", "title": "X"}}, "c1")
        with patch.object(wl, "_call", side_effect=[("", [unknown], ""), ("完成", [], "")]):
            r1 = wl.run("x")
        self.assertIn("未知工具", r1["actions"][0]["result"]["err"])
        with patch.object(wl, "ALLOWED_TOOLS", frozenset(("read", "set", "dangerous"))), \
                patch.object(wl, "_call", side_effect=[("", [unknown], ""), ("完成", [], "")]):
            r2 = wl.run("x")
        self.assertNotIn("未知工具", str(r2["actions"][0]["result"]),
                         "白名单一放宽，同名工具就不再被拦 —— 判据是活的")

    def test_set_guard_rejection_backfilled_verbatim(self):
        """★【验收 6】set 守卫拒写时，loop 如实把 err 回填，LLM 下一轮能看到。"""
        self.mk_draft("p", [self.mk_cand(1).id])
        bad = _tc("set", {"target": "title",
                          "payload": {"slug": "p", "title": "X",
                                      "sources": ["fake:999"]}}, "c1")
        script = [("", [bad], ""), ("守卫拒了，我换个写法。", [], "")]
        with patch.object(wl, "_call", side_effect=script) as call:
            res = wl.run("改标题")
        a = res["actions"][0]
        self.assertFalse(a["ok"])
        self.assertIn("程序维护", str(a["result"]), "守卫 1 的具体原因如实带回")
        second_msgs = call.call_args_list[1].args[1]
        self.assertTrue(any(m["role"] == "tool" and "程序维护" in m["content"]
                            for m in second_msgs))

    def test_polish_tool_recompiles_page(self):
        """★ polish 工具真重编一页结论：loop 里调 polish(slug) → `_polish_one`
        （素材/链接/数字守卫全过）→ set_polish 写回。桩脚本要留出 polish 自己的
        一次 `_call`（顺序：loop 工具轮 → polish 文本 → loop 最终回复）。"""
        c = self.mk_cand(1)
        self.mk_draft("p", [c.id], body="旧正文。")
        polish_call = _tc("polish", {"slug": "p"}, "c1")
        script = [("", [polish_call], ""),
                  ("重新生成后的结论。", [], ""),
                  ("已重新生成 p 的结论。", [], "")]
        with patch.object(wl, "_call", side_effect=script) as call:
            res = wl.run("重新polish当前页")
        self.assertEqual(call.call_count, 3, "loop 两轮 + polish 一次")
        a = res["actions"][0]
        self.assertTrue(a["ok"], a["result"])
        self.assertEqual(a["tool"], "polish")
        self.assertEqual(a["repolished"], ["p"])
        self.assertIn("重新生成后的结论。", self.read_draft("p"),
                      "结论正文真被替换（不是没花钱因为没走到）")
        self.assertNotIn("旧正文", self.read_draft("p"))
        self.assertTrue(res["ok"])

    def test_polish_tool_failure_reason_visible(self):
        """★ polish 传错 slug 时：失败原因如实回填进 tool 消息，且人读汇总里
        带着原因（不再只有「被拒」两个字）。"""
        polish_call = _tc("polish", {"slug": "no-such-page"}, "c1")
        script = [("", [polish_call], ""), ("这页不存在。", [], "")]
        with patch.object(wl, "_call", side_effect=script) as call:
            res = wl.run("重新polish当前页")
        a = res["actions"][0]
        self.assertFalse(a["ok"])
        self.assertIn("草稿不存在或读不到", str(a["result"]))
        second_msgs = call.call_args_list[1].args[1]
        self.assertTrue(any(m["role"] == "tool" and "草稿不存在或读不到" in m["content"]
                            for m in second_msgs), "下一轮 LLM 能看到失败原因")
        body = wa.get_or_create_approval_conversation().messages \
            .filter(role="assistant").last().body
        self.assertIn("草稿不存在或读不到", body, "人读的汇总也要带着失败原因")

    def test_max_turns_stops_and_reports_actions(self):
        """★【验收 7 / 反证 3 正向】桩 `_call` 永远返回 tool_calls：
        撞 MAX_TURNS 时如实汇总已执行动作，不静默、不无限循环。"""
        read_call = _tc("read", {"scope": "drafts"}, "c1")
        with patch.object(wl, "_call", return_value=("", [read_call], "")) as call:
            res = wl.run("一直调")
        self.assertEqual(call.call_count, wl.MAX_TURNS)
        self.assertFalse(res["ok"])
        self.assertIn("轮次上限", res["err"])
        self.assertTrue(res["actions"], "已执行动作要如实返回")
        self.assertIn("本轮已执行的动作", res["reply"])

    def test_guard_max_turns_itself_will_fire(self):
        """★ 守卫自己会响：把上限调大，同一根死循环桩真的会多跑几轮。"""
        read_call = _tc("read", {"scope": "drafts"}, "c1")
        with patch.object(wl, "MAX_TURNS", wl.MAX_TURNS + 3), \
                patch.object(wl, "_call", return_value=("", [read_call], "")) as call:
            wl.run("一直调")
        self.assertEqual(call.call_count, wl.MAX_TURNS + 3,
                         "上限一变，循环次数跟着变 —— 判据是活的")

    def test_budget_stop_reported_and_user_message_persists(self):
        """【验收 7】预算/模型失败：原因回显；question 先落库（失败也留）。"""
        with patch.object(wl, "_call", return_value=("", [], "日预算已用尽")):
            res = wl.run("这句话要留下来")
        self.assertFalse(res["ok"])
        self.assertIn("日预算已用尽", res["err"])
        conv = wa.get_or_create_approval_conversation()
        self.assertTrue(conv.messages.filter(
            role="user", body="这句话要留下来").exists())

    def test_agent_loop_log_one_with_question_context(self):
        """★ 动作日志：整轮只落一条带 question 上下文的 agent-loop，
        不逐 set 重复落两条让人对不上（wiki_agent 内部已落 agent-set）。"""
        self.mk_draft("p", [self.mk_cand(1).id])
        set_call = _tc("set", {"target": "title",
                               "payload": {"slug": "p", "title": "新"}}, "c1")
        with patch.object(wl, "_call", side_effect=[("", [set_call], ""), ("完成", [], "")]):
            wl.run("把标题改成新")
        log_text = (wstore.root() / "log.md").read_text("utf-8")
        self.assertEqual(log_text.count("agent-loop |"), 1)
        self.assertIn("问：把标题改成新", log_text)


class PolishIncrementalTest(WikiLoopBase):
    """set 后增量重编结论（验收 8/9 + 反证 4）：只重编受影响页，
    产物是纯 polish 一段、无双层 / 三块标题、stale 被剥。"""

    def _fake_call_factory(self, set_call):
        """loop 调用（tools=TOOLS）返回脚本；polish 调用（tools=None）返回通顺话。"""
        state = {"calls": [], "loop_step": 0}

        def fake_call(kind, messages, *, tools=None, tag="", label=""):
            state["calls"].append((kind, tools, tag, label))
            if tools is None:
                return ("这页讲的是通顺话，关联 [[other]]。", [], "")
            state["loop_step"] += 1
            if state["loop_step"] == 1:
                return ("", [set_call], "")
            return ("已拆页并重写通顺话。", [], "")

        return fake_call, state

    def test_polish_only_recompiles_affected_pages(self):
        """★【验收 9 / 反证 4 正向】只重编本次 set 影响的页（旧页 + 新建页），不是整库。
        ★ 产物断言（真被用上）：影响页正文 = 桩返回的纯 polish 一段，不含双层 /
          三块标题 / 候选清单；未受影响的 other 页没有这段结论。
        ★ 一致性：frontmatter `candidates` 是分组唯一真值，已移走的候选不在 old 页；
          精确出处由 `trace_rows` 负责；stale 重编成功后被剥。"""
        a = self.mk_cand(1)
        b = self.mk_cand(2)
        c3 = self.mk_cand(3)
        self.mk_draft("old", [a.id, b.id])
        self.mk_draft("other", [c3.id])
        set_call = _tc("set", {"target": "candidates",
                               "payload": {"move": [b.id], "from": "old", "to": "new"}}, "c1")
        fake_call, state = self._fake_call_factory(set_call)
        with patch.object(wl, "_call", side_effect=fake_call):
            res = wl.run("把候选2拆去新页")
        # polish 只被调用于受影响的 old + new（交互文档 §5.3 闭环：两页都重生成）
        polish_labels = [c[3] for c in state["calls"] if c[1] is None]
        self.assertEqual(polish_labels, ["old", "new"], "只重编受影响页，不是整库")
        # 真产物：old 页正文 = 桩返回的纯 polish 一段（桩真被用上）
        old_text = self.read_draft("old")
        old_fm, old_body = wstore.parse_front(old_text)
        self.assertIn("这页讲的是通顺话", old_body, "桩结论真被写进正文")
        for head in (wstore.BODY_HEAD, wstore.POLISH_HEAD, "## 我的判断"):
            self.assertNotIn(head, old_body, "形态 A 正文是纯 polish，无结构标题")
        self.assertNotIn(f"`#{a.id}`", old_body, "候选明细清单不落盘")
        # 分组唯一真值：frontmatter candidates == [a.id]
        declared = wstore._cand_ids(old_fm)
        self.assertEqual(declared, [a.id], "set 已把候选2移走")
        self.assertNotIn(f"`#{b.id}`", old_text, "已移走的候选绝不能还列在这一页")
        # 精确出处改由折叠溯源区 trace_rows 按 frontmatter 负责
        trace = wa.trace_rows(declared)
        self.assertEqual(trace["total"], 1, "溯源区按 frontmatter 精确取出处")
        self.assertEqual(trace["unknown"], [])
        rows = [r for g in trace["groups"] for r in g["rows"]]
        self.assertEqual(len(rows), 1)
        self.assertIn("link", rows[0])
        self.assertIn("quote", rows[0])
        # stale 在 frontmatter，重编成功后被剥；正文不再插 > ⚠️ 提示
        self.assertNotIn("stale: true", old_text, "正文真的一致了，stale 才消失")
        self.assertNotIn(wstore.STALE_MARK, old_text, "正文不许插 > ⚠️ 提示")
        self.assertFalse(wa.read("draft", "old")["body_stale"])
        # 新建页也拿到了纯 polish 结论
        new_body = wstore.parse_front(self.read_draft("new"))[1]
        self.assertIn("这页讲的是通顺话", new_body)
        self.assertNotIn(wstore.POLISH_HEAD, new_body)
        # 未受影响的页没被动过
        other_text = self.read_draft("other")
        self.assertNotIn("这页讲的是通顺话", other_text, "未受影响页没有被重编")
        set_action = [x for x in res["actions"] if x["tool"] == "set"][0]
        self.assertEqual(set_action["repolished"], ["old", "new"])

    def test_guard_affected_only_itself_will_fire(self):
        """★ 守卫自己会响：放宽 `_affected_slugs` 判据返回所有页，
        未受影响的 other 也会被重编 —— 证明「只重编受影响页」是活判据。"""
        a = self.mk_cand(1)
        b = self.mk_cand(2)
        c3 = self.mk_cand(3)
        self.mk_draft("old", [a.id, b.id])
        self.mk_draft("other", [c3.id])
        set_call = _tc("set", {"target": "candidates",
                               "payload": {"move": [b.id], "from": "old", "to": "new"}}, "c1")
        fake_call, state = self._fake_call_factory(set_call)
        with patch.object(wl, "_affected_slugs", return_value=["old", "other"]), \
                patch.object(wl, "_call", side_effect=fake_call):
            wl.run("把候选2拆去新页")
        polish_labels = [c[3] for c in state["calls"] if c[1] is None]
        self.assertIn("other", polish_labels,
                      "判据一放宽，未受影响页也被重编 —— 正向那条断言就会变红")

    def test_polish_feedback_retry_fixes_dead_slug(self):
        """★ 带反馈的重试：第一次引用不存在的页被拒 → 原因喂回 → 第二次照抄 slug 写回。

        反证：若第二次调用不 fed 第一次的错误文案，桩只会返回同样的死链，正文
        不会被写回、stale 不会被剥 —— 这条测试就红。失败原因同时落进 frontmatter，
        下次点按钮也会带上（同一条 feedback 通道跨请求）。
        """
        c = self.mk_cand(1)
        self.mk_draft("p", [c.id], body="")
        self.mk_draft("target-page", [self.mk_cand(2).id])
        seen = []

        def fake_call(kind, messages, *, tools=None, tag="", label=""):
            seen.append(list(messages))  # ★ 拷一份：产品代码会在同一 list 上追加反馈
            if len(seen) == 1:
                return ("结论见 [[zhong-mei-mo-xing-cha-ju]]。", [], "")
            return ("结论见 [[target-page]]。", [], "")

        with patch.object(wl, "_call", side_effect=fake_call):
            ok, why = wl._polish_one("p")
        self.assertTrue(ok, why)
        self.assertEqual(len(seen), 2, "校验失败必须真重试一次")
        first_feedback = "".join(m["content"] for m in seen[0])
        self.assertNotIn("zhong-mei-mo-xing-cha-ju", first_feedback,
                         "第一次调用没有反馈可喂，清单里只有正确 slug")
        second_feedback = "".join(m["content"] for m in seen[1])
        self.assertIn("zhong-mei-mo-xing-cha-ju", second_feedback,
                      "被拒的死 slug 必须回喂给模型，否则是盲重试")
        self.assertIn("结论见 [[target-page]]", self.read_draft("p"))
        fm, _ = wstore.parse_front(self.read_draft("p"))
        self.assertNotIn(wstore.STALE_FM, fm, "成功必须剥 stale")
        self.assertNotIn(wstore.POLISH_ERR_FM, fm, "成功必须剥上次的失败原因")

    def test_polish_failure_reason_persists_for_next_click(self):
        """两次都失败：原因落 frontmatter（跨请求/重启），下次点按钮自动带上。"""
        c = self.mk_cand(1)
        self.mk_draft("p", [c.id], body="旧结论保留。")
        seen = []

        def fake_call(kind, messages, *, tools=None, tag="", label=""):
            seen.append(list(messages))
            return ("结论见 [[no-such-page]]。", [], "")

        with patch.object(wl, "_call", side_effect=fake_call):
            ok, why = wl._polish_one("p")
        self.assertFalse(ok)
        self.assertIn("no-such-page", why)
        fm, body = wstore.parse_front(self.read_draft("p"))
        self.assertIn("no-such-page", fm.get(wstore.POLISH_ERR_FM, ""),
                      "失败原因必须落盘，不能只写日志")
        self.assertEqual(body.strip(), "旧结论保留。", "失败不许改正文")

        seen.clear()
        with patch.object(wl, "_call", side_effect=fake_call):
            ok2, _ = wl._polish_one("p")
        self.assertFalse(ok2)
        self.assertEqual(len(seen), 1, "上次已有反馈 → 不再重试（防重复花钱空转）")
        self.assertIn("no-such-page", "".join(m["content"] for m in seen[0]),
                      "下次点按钮必须把落盘的原因喂回去")

    def test_polish_generation_error_not_retried(self):
        """生成错误（网络/预算/空正文）不重试：citeguard 已对 truncated 翻过倍。"""
        c = self.mk_cand(1)
        self.mk_draft("p", [c.id], body="")
        with patch.object(wl, "_call", return_value=("", [], "预算池今日预算已用尽")) as call:
            ok, why = wl._polish_one("p")
        self.assertFalse(ok)
        self.assertIn("预算", why)
        self.assertEqual(call.call_count, 1, "预算闸是确定性的，重试只是白花钱")

    def test_polish_dead_old_links_not_fed_to_model(self):
        """★ 正文里的死引用（改名/归档残留）不进「已有的引用」清单：
        喂了模型会照保留 → _check_links 拒写 → 重新 polish 空转
        （2026-11-11 实测）。活引用仍要喂，重编才守得住页间关联。"""
        c = self.mk_cand(1)
        self.mk_draft("p", [c.id], body="旧结论，见 [[gone-page]] 与 [[other]]。")
        self.mk_draft("other", [self.mk_cand(2).id])
        captured = {}

        def fake_call(kind, messages, *, tools=None, tag="", label=""):
            captured["messages"] = messages
            return ("新结论，见 [[other]]。", [], "")

        with patch.object(wl, "_call", side_effect=fake_call):
            ok, why = wl._polish_one("p")
        self.assertTrue(ok, why)
        prompt = captured["messages"][1]["content"]
        self.assertNotIn("[[gone-page]]", prompt,
                         "死引用不能出现在「如仍相关则保留」清单里")
        self.assertIn("[[other]]", prompt, "活引用仍要喂给模型")
        self.assertIn("新结论", self.read_draft("p"), "重编真的写回了")


class TwoLayerTest(WikiLoopBase):
    """形态 A 正文写回口径 + `set_polish` 素材守卫 + `_first_sentence` 不把标题当摘要。

    `render_two_layer` 已删；`BODY_HEAD` / `POLISH_HEAD` 只作只读兼容（认清零前老页）。
    """

    def test_new_page_text_has_body_head(self):
        """【验收 8 / 形态 A】新建页正文留空：frontmatter `candidates` 才是分组唯一真值，
        正文不写候选清单 / 三块 / 双层标题（结论由 polish 统一写）。"""
        c = self.mk_cand(1)
        text = wa._new_page_text([c], title="T", domain=DOMAIN)
        fm, body = wstore.parse_front(text)
        self.assertEqual(wstore._cand_ids(fm), [c.id],
                         "frontmatter candidates 是分组唯一真值")
        self.assertEqual(body.strip(), "", "形态 A 新建页正文留空，等 polish 写结论")
        for head in (wstore.BODY_HEAD, wstore.POLISH_HEAD, "## 我的判断"):
            self.assertNotIn(head, text, "正文不许再写任何结构标题")
        self.assertNotIn(f"`#{c.id}`", text, "候选明细清单不落盘")

    def test_render_two_layer_has_both_heads_and_is_idempotent(self):
        """【形态 A】`render_two_layer` 已删，写回口径换成 `render_polish_only`：
        只产出纯 polish 一段、无任何结构标题；重复重编是整段替换，不攒旧结论 /
        旧双层标题。另验 `split_two_layer` 降级为只读兼容，仍能认清零前老页。"""
        one = wstore.render_polish_only("结论 A")
        self.assertEqual(one, "结论 A\n")
        for head in (wstore.BODY_HEAD, wstore.POLISH_HEAD):
            self.assertNotIn(head, one, "形态 A 不写双层标题")
        two = wstore.render_polish_only("结论 B")
        self.assertEqual(two, "结论 B\n")
        self.assertEqual(two.count(wstore.POLISH_HEAD), 0)
        self.assertIn("结论 B", two)
        self.assertNotIn("结论 A", two, "重编是整段替换，不攒旧结论")
        self.assertEqual(wstore.render_polish_only(""), "",
                         "空文本 → 空正文（可见的「结论还没生成」状态）")
        # 只读兼容：认清零前老页（BODY_HEAD / POLISH_HEAD 仍能被拆）
        legacy = (f"{wstore.BODY_HEAD}\n- `#1` 老清单\n\n"
                  f"{wstore.POLISH_HEAD}\n老结论")
        head, polish = wstore.split_two_layer(legacy)
        self.assertIn("`#1`", head)
        self.assertEqual(polish, "老结论")

    def _authoritative(self, slug):
        """按 frontmatter candidates 重排出的**候选明细素材**（纯喂模型，不落盘），
        与 `wiki_loop._polish_one` / `set_polish` 守卫同一口径。"""
        info = wa.read("draft", slug)
        details = {c["cand_id"]: c for c in info["candidate_detail"]}
        return wstore.candidate_list_text(info["candidates"], details)

    def test_set_polish_writes_polish_and_removes_stale(self):
        """【验收 8 / 形态 A】分组变更后 stale 写在 frontmatter（不插正文）；
        `set_polish` 用与 frontmatter 一致的素材成功 → 正文只写纯 polish 一段、
        剥掉 stale。分组唯一真值是 frontmatter candidates，精确出处由
        `trace_rows` 负责，不落正文清单。"""
        a = self.mk_cand(1)
        b = self.mk_cand(2)
        self.mk_draft("p", [a.id, b.id])
        ok, _ = wa.set("candidates", {"move": [b.id], "from": "p", "to": "q"})
        self.assertTrue(ok)
        # stale 在 frontmatter，不插正文
        text1 = self.read_draft("p")
        _fm1, body1 = wstore.parse_front(text1)
        self.assertIn("stale: true", text1, "stale: true 写在 frontmatter")
        self.assertNotIn(wstore.STALE_MARK, body1, "正文不许再插 > ⚠️ 提示")
        self.assertTrue(wa.read("draft", "p")["body_stale"])
        # 用与 frontmatter 一致的素材重编
        ok2, why = wstore.set_polish("p", "这页讲的是结论", self._authoritative("p"))
        self.assertTrue(ok2, why)
        text2 = self.read_draft("p")
        fm2, body2 = wstore.parse_front(text2)
        self.assertEqual(body2.strip(), "这页讲的是结论",
                         "正文只有 polish 一段，无结构标题")
        for head in (wstore.BODY_HEAD, wstore.POLISH_HEAD, "## 我的判断"):
            self.assertNotIn(head, body2, "形态 A 不写双层 / 三块标题")
        self.assertNotIn(wstore.STALE_MARK, body2)
        self.assertNotIn("stale: true", text2, "重编成功才剥 stale")
        self.assertFalse(wa.read("draft", "p")["body_stale"])
        # 分组唯一真值：frontmatter candidates == [a.id]
        declared = wstore._cand_ids(fm2)
        self.assertEqual(declared, [a.id], "set 已把候选2移走")
        self.assertNotIn(f"`#{b.id}`", text2, "移走的候选不落进正文/清单")
        self.assertNotIn(f"`#{a.id}`", body2, "候选明细清单不落盘")
        # 精确出处改由折叠溯源区 trace_rows 负责
        trace = wa.trace_rows(declared)
        self.assertEqual(trace["total"], 1, "溯源区按 frontmatter 精确取出处")
        self.assertEqual(trace["unknown"], [])
        rows = [r for g in trace["groups"] for r in g["rows"]]
        self.assertEqual(len(rows), 1)
        self.assertIn("link", rows[0])
        self.assertIn("quote", rows[0])

    def test_set_polish_rejects_stale_body(self):
        """★【守卫反证 5 正向 / 形态 A】把旧正文（分组已变、不含候选明细素材）
        直接传进 `set_polish` 必须**拒写**：素材里的 `#id` 与 frontmatter 对不上
        （⚠️K 不从正文取，防空集合假绿）。拒写时 stale 仍在 frontmatter、
        `body_stale` 仍为真、结论一个字节都没落盘。"""
        a = self.mk_cand(1)
        b = self.mk_cand(2)
        self.mk_draft("p", [a.id, b.id])
        ok, _ = wa.set("candidates", {"move": [b.id], "from": "p", "to": "q"})
        self.assertTrue(ok)
        old_body = wa.read("draft", "p")["body"]
        ok2, why = wstore.set_polish("p", "这页讲的是结论", old_body)
        self.assertFalse(ok2, "旧正文不含候选明细素材，必须被 ⚠️K 守卫挡下")
        self.assertIn("对不上", why)
        # 一个字节都没动：stale 仍在 frontmatter、body_stale 仍真、结论没落盘
        text = self.read_draft("p")
        _fm, body = wstore.parse_front(text)
        self.assertIn("stale: true", text, "失败时 stale 仍在 frontmatter")
        self.assertTrue(wa.read("draft", "p")["body_stale"])
        self.assertNotIn("这页讲的是结论", body, "拒写时结论不许落盘")
        for head in (wstore.BODY_HEAD, wstore.POLISH_HEAD):
            self.assertNotIn(head, body, "拒写时不写双层标题")

    def test_guard_body_consistency_itself_will_fire(self):
        """★ 守卫自己会响（形态 A 换 ⚠️K 素材守卫）：`set_polish` 守的是
        「素材里的 `#id` == frontmatter candidates」。故意让素材多一条 `#id` →
        必须 (False, ...)；把 `_listed_ids` 放宽成「恒等于 frontmatter」→ 同一份
        不一致素材就不再被拒，证明拦截确实来自这道判据，不是别处碰巧。
        守卫读素材、不读要写回的正文（正文没有清单，不许 `[] == []` 假绿）。"""
        a = self.mk_cand(1)
        b = self.mk_cand(2)
        self.mk_draft("p", [a.id, b.id])
        ok, _ = wa.set("candidates", {"move": [b.id], "from": "p", "to": "q"})
        self.assertTrue(ok)
        info = wa.read("draft", "p")
        declared = wstore._cand_ids(info["fm"])
        details = {c["cand_id"]: c for c in info["candidate_detail"]}
        good = wstore.candidate_list_text(declared, details)
        # 正向：素材与 frontmatter 一致 → 放行（且素材里真有 #id 行，不是空集合）
        self.assertEqual(wstore._listed_ids(good), declared)
        ok_pos, why_pos = wstore.set_polish("p", "这段是结论", good)
        self.assertTrue(ok_pos, why_pos)
        # 变异反证：素材里多一条 #id（与 frontmatter 对不上）→ 必须拒写
        ghost = max(declared or [0]) + 100000
        bad = good + f"\n- `#{ghost}` （凭空多出来的候选）"
        self.assertNotEqual(wstore._listed_ids(bad), declared, "变异素材必须真的多一条 #id")
        ok_bad, why_bad = wstore.set_polish("p", "这段是结论", bad)
        self.assertFalse(ok_bad, "素材 #id 与 frontmatter 不一致必须被 ⚠️K 守卫挡下")
        self.assertIn("对不上", why_bad)
        # 放宽判据：让 `_listed_ids` 恒返回 frontmatter → 同一份 bad 不再被拒
        with patch.object(wstore, "_listed_ids", return_value=declared):
            ok_loose, why_loose = wstore.set_polish("p", "这段是结论", bad)
        self.assertTrue(ok_loose, why_loose)

    def test_first_sentence_skips_body_and_polish_heads(self):
        """【验收 8 / 形态 A】`_first_sentence` 跳过 `#` 开头的标题行，不把
        `## 候选清单` / `## 通顺话` / 三块标题当摘要（认清零前老页也要读对）。
        形态 A 新页是纯 polish 一段，首句就是结论本身。"""
        body = (f"{wstore.BODY_HEAD}\n\n"
                "- 一条判断 [文章《测试》](http://example.com)\n\n"
                f"{wstore.POLISH_HEAD}\n这页讲的是通顺话")
        fs = wstore._first_sentence(body)
        self.assertNotIn("候选清单", fs)
        self.assertNotIn("通顺话", fs)
        self.assertIn("一条判断", fs)
        pure = wstore.render_polish_only("这页讲的是结论")
        self.assertIn("这页讲的是结论", wstore._first_sentence(pure))
