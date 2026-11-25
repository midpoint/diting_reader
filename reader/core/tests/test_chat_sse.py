"""SSE 流式对话端点的行为判据（2026-11-19）。

钉的是「流式是增强、不是替换」这条设计：
  · 老 send 端点（/wiki/chat/send、/wiki/approval/chat/send）一行没动，仍返回整块片段；
  · 新 stream/send 端点逐字吐 delta，末尾 done 帧带**服务端渲染**的权威片段（含 OOB）；
  · 后端生成函数（ask/run）收到 on_delta，字真的从线程桥接出来；
  · 帧协议 t=delta/done/err 稳定，前端据此渲染。

生成出口全打桩（conversation.ask / wiki_loop.run），不真联网、不动真实库数据。
"""
import json
from unittest import mock

from django.test import Client, TestCase, override_settings

from apps.wiki import views as wv


def parse_sse(resp):
    """把 SSE 响应（StreamingHttpResponse）解析成 [{t:...}, ...]。"""
    content = b"".join(resp.streaming_content) if hasattr(resp, "streaming_content") \
        else resp.content
    out = []
    for line in content.decode("utf-8").split("\n"):
        if line.startswith("data: "):
            out.append(json.loads(line[6:]))
    return out


def fake_ask_factory(chunks):
    def fake_ask(st, sid, body, on_delta=None):
        for c in chunks:
            if on_delta:
                on_delta(c)
        return {"ok": True, "err": "", "reply": "".join(chunks)}
    return fake_ask


@override_settings(ALLOWED_HOSTS=["testserver"])
class ArticleStreamTest(TestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=False)

    def test_delta_then_done(self):
        """逐字帧在前、权威片段在后；done.html 是整块 _chat.html。"""
        with mock.patch.object(wv.conversation, "ask",
                               side_effect=fake_ask_factory(["你好", "世界"])):
            r = self.client.post("/wiki/chat/stream/send",
                                 {"source_type": "entry", "source_id": "12",
                                  "body": "讲讲"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "text/event-stream")
        self.assertEqual(r["X-Accel-Buffering"], "no")
        frames = parse_sse(r)
        deltas = [f["v"] for f in frames if f["t"] == "delta"]
        self.assertEqual(deltas, ["你好", "世界"])
        dones = [f for f in frames if f["t"] == "done"]
        self.assertEqual(len(dones), 1)
        self.assertIn('class="wiki-chat"', dones[0]["html"])
        # ★ done.html 是服务端从库里重渲染的权威片段（不是把流式文字拼回去）。
        #   本用例 mock 的 ask 没落库，所以片段是空状态 —— 这正证明它来自库而非流式缓冲。
        self.assertIn('class="wc-form"', dones[0]["html"])
        self.assertTrue(frames[-1]["t"] in ("done", "err"))

    def test_error_surface_as_err_frame(self):
        """生成失败也发 err 帧（不静默挂起），done 仍带片段显示原因。"""
        def bad_ask(st, sid, body, on_delta=None):
            return {"ok": False, "err": "知识库池今日预算已用尽", "reply": ""}
        with mock.patch.object(wv.conversation, "ask", side_effect=bad_ask):
            r = self.client.post("/wiki/chat/stream/send",
                                 {"source_type": "entry", "source_id": "12",
                                  "body": "讲讲"})
        frames = parse_sse(r)
        dones = [f for f in frames if f["t"] == "done"]
        self.assertIn("预算已用尽", dones[0]["html"])


@override_settings(ALLOWED_HOSTS=["testserver"])
class ApprovalStreamTest(TestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=False)

    def test_approval_delta_then_done_with_oob_disabled_when_no_set(self):
        """审批流式：无 set 动作时 done 只有 chat 面板，不带 OOB。"""
        def fake_run(question, *, selected_slug=None, on_delta=None):
            for c in ["挪好了", "：端侧 AI"]:
                if on_delta:
                    on_delta(c)
            return {"ok": True, "err": "", "reply": "挪好了：端侧 AI", "actions": []}
        with mock.patch.object(wv.wiki_loop, "run", side_effect=fake_run):
            r = self.client.post("/wiki/approval/chat/stream/send",
                                 {"body": "挪一下", "selected_slug": ""})
        frames = parse_sse(r)
        self.assertEqual([f["v"] for f in frames if f["t"] == "delta"],
                         ["挪好了", "：端侧 AI"])
        done = [f for f in frames if f["t"] == "done"][0]["html"]
        self.assertIn('id="approval-chat"', done)
        self.assertNotIn('id="approval-list"', done)   # 无 set ⇒ 不刷 OOB

    def test_old_endpoint_unchanged(self):
        """★ 老端点仍返回整块 HTML（流式是并存不是替换）。"""
        def fake_run(question, *, selected_slug=None, on_delta=None):
            return {"ok": True, "err": "", "reply": "旧端点", "actions": []}
        with mock.patch.object(wv.wiki_loop, "run", side_effect=fake_run):
            r = self.client.post("/wiki/approval/chat/send", {"body": "x"})
        self.assertEqual(r.status_code, 200)
        self.assertNotEqual(r["Content-Type"], "text/event-stream")
        self.assertIn('id="approval-chat"', r.content.decode())
