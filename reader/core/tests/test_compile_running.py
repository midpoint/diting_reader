"""「编译正在跑」的状态：切页回来按钮不能再亮（2026-11-03）。

用户报的毛病（原话）：点「编译成草稿」，立刻切到「开销」页再切回来，按钮又可用
了 —— 应该没完成就一直不可用，否则会触发多次编译。

根因不是按钮，是**页面凭什么知道**：
  - htmx 的 `hx-disabled-elt` 只在**当前那份 DOM** 里有效，一切页重新渲染就没了；
  - flock 只回答「现在能不能跑」，它不告诉页面「已经有一份在跑」——后端挡得住
    （回一句「还在跑」，钱不会多花），但那句话是**点了之后**才说的，人已经先误解
    了一次；而且那次响应会把「上次编译」那块换掉，草稿反而看不见。

所以这一组盯的是三件事，缺一条都会退化成更糟的毛病：
  1. 真在跑时，**任何一次页面渲染**拿到的状态都是 running（不靠前端记性）；
  2. 跑完 / 跑失败了，状态必须消失 —— 否则按钮永远灰着，比原来更糟；
  3. 状态文件在、编译锁却是空的（进程被 kill），必须**放行并说出来**：
     只认文件会永久锁死（还不报错），只认锁会让「上次没跑完」永远没人知道。

★ 反证纪律：`test_state_visible_at_the_moment_model_runs` 是在**模型被调用的那一
  刻**读状态，不是事后读 —— 事后读的话，「先跑完再写状态」这种错序也会假绿。
"""
import shutil
import tempfile
from unittest.mock import patch

from django.test import Client, TestCase, override_settings

from core import ingest
from core import wiki as wstore
from core.tests.test_ingest_compile import (CompileBase, group_json, mk_cand,
                                            page_json)


def one_page_reply(c):
    """一条 judge 候选 → 一页合法草稿的 S2 回复（复用 CompileHappyTest 的形状）。"""
    return page_json([{"cand_id": c.id, "section": "judge", "type": "judgment",
                       "text": "价值投资看的是十年后的现金流"}])


class CompileStateCoreTest(CompileBase):
    """core 层：状态的三种局面。"""

    def test_idle_state_is_empty(self):
        st = ingest.compile_state()
        self.assertFalse(st["running"])
        self.assertEqual(st["aborted"], {})

    def test_state_visible_at_the_moment_model_runs(self):
        """★ 关键：模型被调用时，状态必须已经写着「在跑」并且知道有几条。

        写在模型之后 = 那 1–3 分钟里页面全是亮的（正是用户踩到的那段）。
        """
        a = mk_cand("价值投资看的是十年后的现金流", "价值投资看长期现金流")
        b = mk_cand("十年后无法精确预测，只能毛估估", "长期无法精确预测")
        seen, replies = {}, [
            group_json([a.id, b.id]),
            page_json([
                {"cand_id": a.id, "section": "judge", "type": "judgment",
                 "text": "价值投资看的是十年后的现金流"},
                {"cand_id": b.id, "section": "judge", "type": "judgment",
                 "text": "十年后无法精确预测，只能毛估估"},
            ]),
        ]

        def fake(*_a, **_kw):
            seen.setdefault("state", ingest.compile_state())
            return replies.pop(0), ""

        with patch("core.citeguard._call", side_effect=fake) as call:
            res = ingest.compile_candidates()
        self.assertTrue(res["ok"], res.get("err"))
        self.assertEqual(call.call_count, 2, "S1 分组 + S2 生成，桩没用上就是真花了钱")

        st = seen["state"]
        self.assertTrue(st["running"], "模型跑着的时候，页面必须看到「在跑」")
        self.assertTrue(st["started_at"], "要说清几点开始的，人才知道等了多久")
        self.assertEqual(st["n"], 2, "要说清在编几条，人才知道该等多久")

        after = ingest.compile_state()
        self.assertFalse(after["running"], "跑完了状态必须消失，否则按钮永远灰着")
        self.assertEqual(after["aborted"], {}, "正常收尾不许被记成「上次没跑完」")

    def test_state_cleared_even_when_run_fails(self):
        """失败路径也要清状态：漏清 = 一次模型失败就把按钮永久按死。"""
        mk_cand("价值投资看的是十年后的现金流", "价值投资看长期现金流")
        with patch("core.citeguard._call", return_value=("", "模拟模型失败")):
            res = ingest.compile_candidates()
        self.assertFalse(res["ok"])
        st = ingest.compile_state()
        self.assertFalse(st["running"])
        self.assertEqual(st["aborted"], {}, "失败是**跑完了**的一种，不是「没跑完」")

    def test_refused_click_names_which_run_is_going(self):
        """被挡下时那句话要带「几点开始、已跑多久、几条」——否则人分不清卡死。"""
        mk_cand("价值投资看的是十年后的现金流", "价值投资看长期现金流")
        fd = ingest._try_lock_compile()
        self.assertIsNotNone(fd)
        try:
            ingest._mark_running(n=7)
            calls = []
            with patch("core.citeguard._call",
                       side_effect=lambda *a, **k: calls.append(1) or ("", "")):
                res = ingest.compile_candidates()
            self.assertFalse(res["ok"])
            self.assertIn("还在跑", res["err"])
            self.assertIn("7 条候选", res["err"])
            self.assertIn("已 ", res["err"])
            self.assertEqual(len(calls), 0, "被挡下时一次模型都不许调")
        finally:
            ingest._clear_state()
            ingest._unlock_compile(fd)

    def test_leftover_state_with_no_lock_is_reported_not_blocking(self):
        """反证：进程被 kill（文件在、锁空了）→ 放行，但必须说出来。

        ★ 这条是全组最贵的：只认文件 = 编译**永久**点不动、还一声不响。
        """
        ingest._mark_running(n=9)
        st = ingest.compile_state()
        self.assertFalse(st["running"], "锁是空的就说明没人真在跑，不许把按钮按死")
        self.assertTrue(st["aborted"], "上次没跑完必须被看见；静默放行 = 人只会重试")
        self.assertTrue(st["aborted"].get("started_at"))

    def test_state_cleared_before_lock_released(self):
        """反证：**放锁那一刻**状态必须已经删掉。

        顺序反了会出现一瞬「锁空了、文件还在」—— 另一个线程正好那一刻渲染页面，
        就会把一次**正常跑完**的编译读成「上次没跑完」，白报一次警（而且这种
        偶发提示比没有提示更伤信任）。这一条只能盯放锁的瞬间，事后看是看不出来的。
        """
        c = mk_cand("价值投资看的是十年后的现金流", "价值投资看长期现金流")
        seen = []
        real_unlock = ingest._unlock_compile

        def spy(fd):
            seen.append(ingest._read_state())
            return real_unlock(fd)

        with patch("core.ingest._unlock_compile", side_effect=spy):
            with patch("core.citeguard._call", side_effect=iter(
                    [(group_json([c.id]), ""), (one_page_reply(c), "")])):
                res = ingest.compile_candidates()
        self.assertTrue(res["ok"], res.get("err"))
        self.assertEqual(len(seen), 1, "放锁只该发生一次（多出来的是探测）")
        self.assertEqual(seen[0], {}, "放锁时状态还在：别的线程会误报「上次没跑完」")

    def test_next_run_clears_the_aborted_note(self):
        """反证之反证：那句「上次没跑完」不能永远挂着（挂着就成了噪音，人不再读）。"""
        ingest._mark_running(n=9)
        c = mk_cand("价值投资看的是十年后的现金流", "价值投资看长期现金流")
        with patch("core.citeguard._call", side_effect=iter(
                [(group_json([c.id]), ""), (one_page_reply(c), "")])):
            res = ingest.compile_candidates()
        self.assertTrue(res["ok"], res.get("err"))
        self.assertEqual(ingest.compile_state()["aborted"], {})


class CompileBusyPageTest(CompileBase):
    """视图层：页面渲染时就该是灰的，且有一块自己会刷新的状态条。"""

    def test_page_shows_disabled_button_while_running(self):
        mk_cand("价值投资看的是十年后的现金流", "价值投资看长期现金流")
        fd = ingest._try_lock_compile()
        try:
            ingest._mark_running(n=1)
            html = Client().get("/wiki").content.decode()
        finally:
            ingest._clear_state()
            ingest._unlock_compile(fd)
        self.assertIn("编译中…", html)
        # ★ 在跑时页面上不该存在**可提交的**编译表单：只要表单在，就有人能点。
        self.assertNotIn('hx-post="/wiki/compile"', html,
                         "在跑时还渲染了可提交的表单 —— 切页回来就能再点一次")
        self.assertIn("/wiki/compile/state", html, "缺了轮询块：跑完页面不会自己变回来")

    def test_page_enables_button_after_run(self):
        """反证：空闲时必须回到可点。写死 disabled 的页面也能过上面那条。"""
        mk_cand("价值投资看的是十年后的现金流", "价值投资看长期现金流")
        html = Client().get("/wiki").content.decode()
        i = html.find('hx-post="/wiki/compile"')
        self.assertGreater(i, -1, "空闲时没有可提交的编译表单：按钮被永久按死了")
        block = html[i:html.find("</form>", i)]
        # ★ 只看 <button> 自己那个标签：整段里还有 `hx-disabled-elt`（htmx 的属性名
        #   里就带 disabled），拿整段判会被它骗过 —— 这是本项目反复踩的「断言太宽」。
        b0 = block.find("<button")
        btn_tag = block[b0:block.find(">", b0)]
        self.assertIn("编译成草稿", block[b0:block.find("</button>", b0)],
                      "没找到那个提交按钮，断言等于没做")
        self.assertNotIn("disabled", btn_tag, "空闲时按钮仍带 disabled：编译永远点不动")
        self.assertNotIn('class="compile-running"', html, "空闲时不该有轮询块")

    def test_stale_tab_click_is_refused_and_fragment_is_already_busy(self):
        """老页面（编译开始前就渲染好的）上点一下：拒绝，且回给它的那一段已经是灰的。

        ★ 这个场景前端挡不住 —— 那个标签页的 DOM 里表单还在。所以「点完得到的
          那一小段」必须自己翻成 busy 态，否则人看到一句「还在跑」、按钮依旧亮着，
          再点一次又是一句同样的话。
        """
        mk_cand("价值投资看的是十年后的现金流", "价值投资看长期现金流")
        fd = ingest._try_lock_compile()
        try:
            ingest._mark_running(n=3)
            c = Client()
            resp = c.get("/wiki")
            token = resp.cookies["csrftoken"].value
            calls = []
            with patch("core.citeguard._call",
                       side_effect=lambda *_a, **_k: calls.append(1) or ("", "")):
                resp = c.post("/wiki/compile", {"csrfmiddlewaretoken": token})
            html = resp.content.decode()
            self.assertIn("还在跑", html, "被挡下却没说原因：人会以为点了没反应")
            self.assertNotIn('hx-post="/wiki/compile"', html,
                             "被拒之后回的那一段仍是可点的表单 —— 还能再点一次")
            self.assertIn("编译中…", html)
            self.assertEqual(len(calls), 0, "被挡下时一次模型都不许调")
        finally:
            ingest._clear_state()
            ingest._unlock_compile(fd)

    def test_watch_endpoint_polls_while_running(self):
        fd = ingest._try_lock_compile()
        try:
            ingest._mark_running(n=1)
            resp = Client().get("/wiki/compile/state")
        finally:
            ingest._clear_state()
            ingest._unlock_compile(fd)
        body = resp.content.decode()
        self.assertNotIn("HX-Refresh", resp, "还在跑就重载页面 = 把人从阅读位置踢走")
        self.assertIn('hx-trigger="every 5s"', body, "响应片段必须自己带轮询属性")
        self.assertIn("/wiki/compile/state", body)

    def test_watch_endpoint_reloads_page_when_done(self):
        """跑完这一块换不回来（草稿在页面另一处），只能整页重载。"""
        resp = Client().get("/wiki/compile/state")
        self.assertEqual(resp["HX-Refresh"], "true")
        self.assertEqual(resp.content, b"")
