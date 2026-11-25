"""第五步 5b 切片验收：审批对话（③ panel，让只读浮层变「可驱动」）。

铁律口径：
  - **wiki_loop.run 全程打桩**：桩打被调用点 `core.wiki_loop.run`（不是
    `core.citeguard._call`）；花钱出口 `core.wiki_loop._call` /
    `core.citeguard._call` 在主用例里 `assert_not_called`。
    ★「桩真被用上」反证：断言 run 收到的 question == 用户输入；另有一条用例
      「不桩 run、只桩 _call」证明链路确实走到了 loop 出口 —— 没花钱不是压根
      没走到（删掉 run 的桩就会走到 _call）。
  - **run 自己落库**：桩的 side_effect 模拟落库 user/assistant，视图只取历史
    渲染 —— 断言 conv.messages.count() == 2（不是 4），验证视图没重复落。
  - **刷新判据收一处**：只在 ok 且 actions 里有成功 set 时拼 ① ② 的
    hx-swap-oob 片段；纯 read / err 不刷（test_pure_read_does_not_refresh）。
  - **真产物断言**：set 成功后读 `.drafts/` 文件数 + frontmatter，不硬编码库状态。
  - WIKI_DIR override 到临时目录，ORM 走 Django 测试库，不动真实库。
"""
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from apps.wiki.views import _track_slug_change
from core import citeguard
from core import wiki as wstore
from core import wiki_agent as wa
from core import wiki_loop as wl
from core.tests.test_approval_overlay import ApprovalOverlayBase, DOMAIN

USER_Q = "把第 3、5 条候选挪去新页『端侧 AI 成本』"


def _run_res(actions=None, err="", reply="", ok=True):
    """构造 wiki_loop.run 的返回（视图只认这 4 个键）。"""
    return {"ok": ok, "err": err, "reply": reply, "actions": actions or []}


def _set_action(ok=True):
    return {"tool": "set", "args": {"target": "candidates"}, "ok": ok,
            "result": {}, "affected": ["p1", "p2"], "repolished": []}


def _read_action():
    return {"tool": "read", "args": {"scope": "drafts"}, "ok": True,
            "result": {}, "repolished": []}


class ApprovalChatBase(ApprovalOverlayBase):
    """复用 5a 的临时 WIKI_DIR 与造数 helper（判据收一处，不重复写）。"""

    def drop_user(self, question=USER_Q):
        """模拟 run 内部落的一条 user 消息。"""
        wa.get_or_create_approval_conversation().messages.create(
            role="user", body=question)

    def drop_assistant(self, body="已看完当前分组，没有改动。"):
        wa.get_or_create_approval_conversation().messages.create(
            role="assistant", body=body)

    def stub_run(self, *, actions=None, err="", reply="", ok=True,
                 assistant="已看完当前分组，没有改动。", extra=None):
        """返回 patch 用的 side_effect：模拟 run 落库 + 返回 res。

        ★ 这是「run 自己落库」的替身；视图若重复落，conv.messages.count()
          就会是 4 而不是 2，测试立刻红。
        """
        def fake(question, selected_slug=None):
            if not ok and err and question == "":
                # 空 body：run 早退，不落库、不调模型
                return _run_res(err=err, ok=False)
            self.drop_user(question)
            self.drop_assistant(assistant)
            if extra:
                extra()
            return _run_res(actions=actions, err=err, reply=reply, ok=ok)
        return fake


class ApprovalChatViewTest(ApprovalChatBase):
    """§三 验收：③ panel 形态 / 端点 / run 落库 / err / 空 body / set 刷新 / 清空。"""

    def test_shell_has_chat_panel_and_oob_targets(self):
        c = self.mk_cand(1, claim="甲判断")
        self.mk_draft("p1", [c.id], title="测试页")
        resp = self.client.get("/wiki/approval")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        # ③ 对话 panel 常显（改动 3）
        self.assertIn('id="approval-chat"', html)
        self.assertIn("审批对话", html)
        self.assertIn("说你要怎么改", html)
        self.assertIn('hx-post="/wiki/approval/chat/send"', html)
        self.assertIn('hx-indicator="#approval-chat-busy"', html)
        self.assertIn('hx-target="#approval-chat"', html)
        # ① ② 根节点仍在（① 已抽成独立模板供 OOB 刷新）
        self.assertIn('id="approval-list"', html)
        self.assertIn('id="approval-workarea"', html)
        self.assertIn('class="approval-draft', html)
        self.assertNotIn("<html", html, "浮层片段不能包整页 html")
        # ★ 可见文字里没有模板注释/源码残留（DOM 结构对 ≠ 页面没脏东西）
        for junk in ["cursor:pointer", "不带 href", "hx-get 不依赖",
                     "{%", "#}", "endcomment"]:
            self.assertNotIn(junk, html, "模板注释泄漏：%s" % junk)

    def test_chat_get_renders_fragment_and_creates_empty_conversation(self):
        resp = self.client.get("/wiki/approval/chat")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertTrue(html.lstrip().startswith('<div class="approval-chat"'),
                        html[:120])
        self.assertIn('id="approval-chat"', html)
        self.assertNotIn("<html", html)
        self.assertIn('hx-post="/wiki/approval/chat/send"', html)
        # ★ 坑 6：GET 就建一条空对话，无害，但 .messages 必须为空
        conv = wa.get_or_create_approval_conversation()
        self.assertIsNotNone(conv)
        self.assertEqual(conv.messages.count(), 0)

    def test_approval_messages_have_no_mark_anchors(self):
        """2026-11-22：审批对话暂时不做划线/批注 —— 不显示，也不能新建。

        ★ 只 CSS 隐藏不够：app.js 的 marksCtx 会顺着 `data-mark-type` 找锚点，
          锚点在就能选中文字建痕迹、存了又看不见（静默失败）。所以这里断 DOM。
        """
        self.drop_user("把第 3 条挪去新页")
        self.drop_assistant("已调整。")
        html = self.client.get("/wiki/approval/chat").content.decode()
        self.assertIn("把第 3 条挪去新页", html, "消息本身仍要看得见")
        for junk in ('data-mark-type="chat"', 'data-mark-body',
                     'id="marks-chat-'):
            self.assertNotIn(junk, html, "审批消息不该挂痕迹锚点：%s" % junk)

    def test_send_calls_run_with_user_body_and_no_llm(self):
        """★ 花钱桩主用例：run 被调、传的是 body、片段含新消息、不重复落、不花钱。"""
        with patch.object(wl, "run",
                          side_effect=self.stub_run(reply="已看完当前分组，没有改动。")) as mock_run, \
                patch.object(wl, "_call") as mock_call, \
                patch.object(citeguard, "_call") as mock_cite:
            resp = self.client.post("/wiki/approval/chat/send", {"body": USER_Q})
        html = resp.content.decode()
        # ★「桩真被用上」反证：run 收到的 question 就是用户敲的那句
        mock_run.assert_called_once_with(USER_Q, selected_slug=None)
        self.assertEqual(mock_run.call_args.args[0], USER_Q)
        # 花钱出口被 run 桩整条挡住
        mock_call.assert_not_called()
        mock_cite.assert_not_called()
        # 片段含新的 user 消息 + assistant 回复（run 落库 → 视图取历史渲染）
        self.assertIn(USER_Q, html)
        self.assertIn("已看完当前分组，没有改动。", html)
        self.assertIn('class="wc-msg wc-user"', html)
        self.assertIn('class="wc-msg wc-assistant"', html)
        # ★ 坑 1：run 落 2 条，视图不重复落（不是 4）
        self.assertEqual(
            wa.get_or_create_approval_conversation().messages.count(), 2)

    def test_run_stub_is_really_used(self):
        """★ 反证 2：不桩 run、只桩 _call → 链路确实走到了 loop 出口。

        证明「有 run 桩时没花钱」不是压根没走到 loop：删掉 run 的桩，请求
        就会往下走到 _call（这里 _call 仍是桩，所以照样不花钱）。
        """
        with patch.object(wl, "_call",
                          return_value=("已看完当前分组，没有改动。", [], "")) as mock_call, \
                patch.object(citeguard, "_call") as mock_cite:
            resp = self.client.post("/wiki/approval/chat/send", {"body": USER_Q})
        html = resp.content.decode()
        self.assertTrue(mock_call.called, "视图没走到 run / _call，桩白打了")
        mock_cite.assert_not_called()
        # 真实 run 落库 user + assistant，视图不重复落
        self.assertIn(USER_Q, html)
        self.assertIn("已看完当前分组，没有改动。", html)
        self.assertEqual(
            wa.get_or_create_approval_conversation().messages.count(), 2)

    def test_err_written_in_chat_area_not_toast(self):
        with patch.object(wl, "run", return_value=_run_res(
                err="wiki 池预算用尽，请明天再试", ok=False)):
            resp = self.client.post("/wiki/approval/chat/send", {"body": "继续"})
        html = resp.content.decode()
        self.assertIn("wiki 池预算用尽，请明天再试", html)
        self.assertIn('class="wc-err"', html)
        self.assertIsNone(resp.get("HX-Trigger"), "err 写对话区，不走 toast")
        self.assertNotIn("hx-swap-oob", html, "err 时 .drafts/ 未必变，不刷面板")

    def test_empty_body_returns_reason_and_required(self):
        with patch.object(wl, "run", side_effect=self.stub_run(
                err="问题不能为空", ok=False)) as mock_run, \
                patch.object(wl, "_call") as mock_call:
            resp = self.client.post("/wiki/approval/chat/send", {"body": ""})
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("问题不能为空", html)
        self.assertIn('name="body" rows="2" required', html)
        mock_run.assert_called_once_with("", selected_slug=None)
        mock_call.assert_not_called()

    def test_set_success_refreshes_panels_with_real_products(self):
        """★ set 成功后刷新 ① ②：真产物断言（读 .drafts/ 文件数 + frontmatter）。"""
        c1 = self.mk_cand(1, claim="甲判断")
        c2 = self.mk_cand(2, claim="乙判断")
        self.mk_draft("p1", [c1.id, c2.id], title="原页")
        before = sorted((wstore.root() / ".drafts").glob("*.md"))
        self.assertEqual(len(before), 1)
        moved = c2.id

        def do_set():
            ok, why = wa.set("candidates", {
                "move": [moved], "from": "p1", "to": "p2",
                "title": "新页", "domain": DOMAIN})
            assert ok, why      # 守卫：wa.set 拒了就把原因摆出来，不静默往下走

        with patch.object(wl, "run", side_effect=self.stub_run(
                actions=[_set_action()], reply="已拆出新页。",
                assistant="【本轮动作】\n- set(candidates) → 成功\n\n已拆出新页。",
                extra=do_set)):
            resp = self.client.post("/wiki/approval/chat/send",
                                    {"body": "把第 2 条挪去新页"})
        html = resp.content.decode()

        # ---- 真产物：文件系统里多了一页、frontmatter 分组真的变了 ----
        after = sorted((wstore.root() / ".drafts").glob("*.md"))
        self.assertEqual(len(after), len(before) + 1)
        drafts = {d["slug"]: d for d in wstore.list_drafts()}
        self.assertIn("p2", drafts)
        self.assertNotIn(moved, drafts["p1"]["candidates"])
        self.assertIn(moved, drafts["p2"]["candidates"])

        # ---- 判据触发：响应拼 ① + ② 两个 hx-swap-oob 片段 ----
        self.assertIn("hx-swap-oob", html)
        self.assertIn('id="approval-list"', html)
        self.assertIn('id="approval-workarea"', html)
        # ① OOB 片段反映的是文件系统真产物（列表项数 == 草稿页数）
        #   ★ 精确匹配带闭合引号：`class="approval-draft` 前缀会把 row 也数进去
        self.assertEqual(html.count('class="approval-draft"'), len(drafts))
        self.assertIn("新页", html)
        # 「本轮动作」随 run 落库的 assistant 消息显示（不单独渲染第二份）
        self.assertIn("本轮动作", html)
        self.assertEqual(html.count("本轮动作"), 1)

    def test_title_rename_keeps_selection_on_new_slug(self):
        """★ 改名后选中必须跟到新 slug：列表高亮 + chat 隐藏字段都用新 slug，
        不能掉回第一份草稿（2026-11-09 实测「第二次对话改错草稿」的根因）。"""
        c = self.mk_cand(1)
        self.mk_draft("p", [c.id], title="原页")

        def do_rename():
            ok, why = wa.set("title", {"slug": "p", "title": "投资心法"})
            assert ok, why      # 真改名：p → tou-zi-xin-fa（拼音）

        action = {"tool": "set", "args": {"target": "title"}, "ok": True,
                  "result": {"action": "title", "slug": "tou-zi-xin-fa",
                             "old_slug": "p"},
                  "affected": [], "repolished": []}
        with patch.object(wl, "run", side_effect=self.stub_run(
                actions=[action], reply="已改标题。",
                assistant="【本轮动作】\n- set(title) → 成功\n\n已改标题。",
                extra=do_rename)) as mock_run:
            resp = self.client.post("/wiki/approval/chat/send",
                                    {"body": "标题改成投资心法", "selected_slug": "p"})
        html = resp.content.decode()
        # POST 里的 selected_slug 原样传给 run（前端链条没断）
        mock_run.assert_called_once_with("标题改成投资心法", selected_slug="p")
        # 响应的 chat 面板 + 列表 oob 都指向新 slug，而不是第一份草稿
        self.assertIn('name="selected_slug" value="tou-zi-xin-fa"', html)
        self.assertIn("selected_slug=tou-zi-xin-fa", html)

    def test_clear_empties_history(self):
        """★ 反证 3：清空后 messages.count() == 0（新建的空对话不影响 .messages）。"""
        self.drop_user("第一句")
        self.drop_assistant("第一次回复")
        self.assertEqual(
            wa.get_or_create_approval_conversation().messages.count(), 2)
        resp = self.client.post("/wiki/approval/chat/clear")
        html = resp.content.decode()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            wa.get_or_create_approval_conversation().messages.count(), 0)
        self.assertIn("说你要怎么改", html)       # 空状态回来了
        self.assertNotIn('hx-post="/wiki/approval/chat/clear"', html)

    def test_clear_form_inside_chat_panel_when_history(self):
        """④ 降级：不做独立列表页，「③ 内含清空 + 完整滚动历史」。"""
        self.drop_user("历史里的一句话")
        self.drop_assistant("历史里的回复")
        html = self.client.get("/wiki/approval").content.decode()
        self.assertIn('id="approval-chat"', html)
        self.assertIn('hx-post="/wiki/approval/chat/clear"', html)
        self.assertIn("hx-confirm", html)
        self.assertIn("历史里的一句话", html)
        self.assertIn("历史里的回复", html)
        self.assertNotIn('id="approval-chat-list"', html, "本步不做独立 ④ 列表页")


class TrackSlugChangeTest(SimpleTestCase):
    """★ 选中链路的枢纽：set 结果在 `act["result"]`（`_exec_tool` 落键），
    读成 `act["payload"]` 会永远匹配不上 —— 改名后选中跳回第一份草稿，
    下一句话就改错对象（2026-11-09 实测）。"""

    def test_rename_of_selected_returns_new_slug(self):
        actions = [{"tool": "read", "args": {"scope": "drafts"}, "ok": True,
                    "result": {}},
                   {"tool": "set", "args": {"target": "title"}, "ok": True,
                    "result": {"action": "title", "slug": "tou-zi-xin-fa",
                               "old_slug": "ai-pao-mo-xiang-guan"}}]
        self.assertEqual(_track_slug_change(actions, "ai-pao-mo-xiang-guan"),
                         "tou-zi-xin-fa")

    def test_rename_of_other_draft_keeps_selection(self):
        actions = [{"tool": "set", "args": {"target": "title"}, "ok": True,
                    "result": {"action": "title", "slug": "other-new",
                               "old_slug": "other"}}]
        self.assertEqual(_track_slug_change(actions, "p"), "p")

    def test_failed_set_result_is_string_and_ignored(self):
        actions = [{"tool": "set", "args": {"target": "title"}, "ok": False,
                    "result": "草稿不存在：x"}]
        self.assertEqual(_track_slug_change(actions, "p"), "p")


class ApprovalChatGuardProofTest(ApprovalChatBase):
    """三条「守卫自己会响」的反证 + enabled 开关。"""

    def test_pure_read_does_not_refresh(self):
        """★ 反证 1：纯 read 对话不刷面板。

        把 views._set_succeeded 放宽成「每次都刷」→ 这条 RED；还原 → OK。
        """
        with patch.object(wl, "run", side_effect=self.stub_run(
                actions=[_read_action()], reply="只看了看，没改动。",
                assistant="只看了看，没改动。")):
            resp = self.client.post("/wiki/approval/chat/send", {"body": "看看分组"})
        html = resp.content.decode()
        self.assertIn("只看了看，没改动。", html)
        self.assertNotIn("hx-swap-oob", html)
        self.assertNotIn('id="approval-list"', html)
        self.assertNotIn('id="approval-workarea"', html)

    def test_failed_set_does_not_refresh(self):
        """ok=False / set 被拒都不刷：判据是「成功 set」，不是「有过 set」。"""
        with patch.object(wl, "run", side_effect=self.stub_run(
                actions=[_set_action(ok=False)], err="拆页被守卫拒绝", ok=False,
                assistant="（拆页被守卫拒绝）")):
            resp = self.client.post("/wiki/approval/chat/send", {"body": "拆页"})
        self.assertNotIn("hx-swap-oob", resp.content.decode())

    @override_settings(WIKI_CHAT_ENABLED=False)
    def test_disabled_hides_send_form(self):
        """enabled 复用 WIKI_CHAT_ENABLED，不新加配置开关（坑 7）。"""
        html = self.client.get("/wiki/approval/chat").content.decode()
        self.assertIn("对话未开启", html)
        self.assertNotIn('hx-post="/wiki/approval/chat/send"', html)
