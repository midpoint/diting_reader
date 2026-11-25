"""第十一步「双向硬锁」的方向二：提炼/编译进行中，审批与编辑必须被挡。

原方向一「审批中暂停提炼/编译」已于 2026-11-09 去掉（用户拍板：草稿分批
产生，审批期照样要编下一批；`approval_active()` 只剩展示用途）。本文件钉的
是**唯一保留的方向**：一条龙在写 `.drafts/` 时人若通过 / 退稿 / 编辑同一批
文件，「这一批」的边界就碎了。

判据与纪律：
  - 用**真锁 + 真状态文件**造 running，不 patch `compile_state()`：守卫读的就是
    这两个事实，patch 掉等于把被测对象换成一句假话（假绿）。
  - 拒绝必须「可见」：11 个端点响应形状各异（片段 / JSON / toast / 纯 HTML），
    统一断言人话出现在 body 或响应头里，且不许 500。
  - 「桩真被用上」的反证：空闲时同一个 POST 必须真的调到写函数，否则
    「running 时没被调用」也可能只是链路根本没通。
  - `aborted`（状态文件在、锁空 = 上次被 kill）必须放行：不能让一次死掉的
    编译把人永久锁在外面。
"""
import shutil
import tempfile
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from core import ingest
from core import wiki as wstore

GUARD_WORD = "不能审批或编辑"

#: 11 个会写 .drafts/ / wiki/ 的 POST 端点。URL 用 reverse，改名时这里会先红。
WRITE_ENDPOINTS = [
    ("wiki_approve", {"slug": "any-draft"}),
    ("wiki_discard", {"slug": "any-draft"}),
    ("wiki_draft_save", {"slug": "any-draft", "body": "正文"}),
    ("wiki_candidate", {"id": "1", "status": "discarded"}),
    ("wiki_approval_commit", {}),
    ("wiki_approval_chat_send", {"body": "把候选挪走"}),
    ("wiki_relink_apply", {"target": "x", "action": "demote"}),
    ("wiki_retire_apply", {"slug": "x", "cand_id": "1", "action": "discard"}),
    ("wiki_page_edit", {"page": "wiki/invest/x.md"}),
    ("wiki_page_delete", {"page": "wiki/invest/x.md"}),
    ("wiki_page_save", {"page": "wiki/invest/x.md", "body": "正文"}),
]


class MutexBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-mutex-")
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
        self.fd = None

    def tearDown(self):
        if self.fd is not None:
            ingest._unlock_compile(self.fd)
            self.fd = None
        ingest._clear_state()

    def running(self, phase="compile"):
        """真拿锁 + 真写状态文件，让 compile_state() 自己判出 running。

        幂等：同一个用例里循环验多个端点时，重复调不该第二次去拿同一把锁。
        """
        if self.fd is None:
            self.fd = ingest._try_lock_compile()
            assert self.fd is not None, "测试前置失败：编译锁拿不到"
        ingest._clear_state()
        ingest._mark_running(phase=phase, n=2, skipped=0, source="")
        self.assertTrue(ingest.compile_state()["running"])

    def visible(self, resp):
        """响应里能被人看到的那句话（body + 解码后的 HX-Trigger / JSON body）。

        ★ `_fragment` / `_toast_only` 把拒绝话放进 `HX-Trigger` 头，而 HTTP 头
          只能 ASCII ⇒ `json.dumps` 默认把中文转成 \\uXXXX；`JsonResponse` 的
          body 同理（Django 默认 ensure_ascii=True）。前端 JSON.parse 都会还原，
          产品没坏。测试若直接找中文会假红；必须按**浏览器真正看到**的口径
          先解码，这也顺带钉住「toast 内容必须单独断言」那条纪律。
        """
        text = resp.content.decode("utf-8", "replace")
        if "application/json" in resp.get("Content-Type", ""):
            try:
                import json
                text += " " + json.dumps(json.loads(text), ensure_ascii=False)
            except (ValueError, TypeError):
                pass
        raw = resp.get("HX-Trigger")
        if raw:
            try:
                import json
                text += " " + json.dumps(json.loads(raw), ensure_ascii=False)
            except (ValueError, TypeError):
                text += " " + str(raw)
        return text


class TestGuardHelper(MutexBase):
    def test_idle_allows(self):
        """空闲：返回空串（放行），不能凭空挡人做事。"""
        self.assertEqual(views_guard(), "")

    def test_extract_phase_is_named(self):
        """提炼阶段也要挡，且说清是「提炼」不是「编译」（提炼同样几分钟）。"""
        self.running(phase="extract")
        msg = views_guard()
        self.assertIn(GUARD_WORD, msg)
        self.assertIn("正在提炼新痕迹", msg)

    def test_compile_phase_is_named(self):
        self.running(phase="compile")
        msg = views_guard()
        self.assertIn(GUARD_WORD, msg)
        self.assertIn("正在编译成页", msg)

    def test_aborted_does_not_lock_forever(self):
        """状态文件在但锁空 = 上次被 kill：放行，且 compile_state 报 aborted。"""
        ingest._mark_running(phase="compile", n=2, skipped=0, source="")
        st = ingest.compile_state()
        self.assertFalse(st["running"])
        self.assertTrue(st["aborted"])
        self.assertEqual(views_guard(), "")


class TestWriteEndpointsBlocked(MutexBase):
    def test_all_11_endpoints_refuse_visibly(self):
        """11 个端点每个都要：不 500、返回同一句人话、且不是静默成功。"""
        self.running()
        for name, data in WRITE_ENDPOINTS:
            with self.subTest(endpoint=name):
                resp = self.client.post(reverse(name), data)
                self.assertEqual(resp.status_code, 200, name)
                self.assertIn(GUARD_WORD, self.visible(resp), name)

    def test_guard_word_absent_when_idle(self):
        """反证：这句话不是模板里恒有的字，只在 running 时出现。

        ★ 状态码不能一律断言 200：`wiki_page_save` 正常路径就是 302 跳回详情页。
          这里只关心「拒绝话有没有凭空出现」，顺手确认没 500 就够了。
        """
        for name, data in WRITE_ENDPOINTS:
            with self.subTest(endpoint=name):
                resp = self.client.post(reverse(name), data)
                self.assertLess(resp.status_code, 500, name)
                self.assertNotIn(GUARD_WORD, self.visible(resp), name)

    def test_stub_really_used_when_idle_and_blocked_when_running(self):
        """★ 桩真被用上的反证：空闲时写函数必须被调；running 时一次都不许调。

        只写「running 时 assert_not_called」是假绿 —— 链路压根没通也绿。
        """
        with patch("core.wiki.approve", return_value=(False, {"err": "桩"})) as fn:
            self.client.post(reverse("wiki_approve"), {"slug": "any-draft"})
            self.assertEqual(fn.call_count, 1, "空闲时桩没被用上，测试链路是断的")
        with patch("core.wiki.approve", return_value=(False, {"err": "桩"})) as fn:
            self.running()
            self.client.post(reverse("wiki_approve"), {"slug": "any-draft"})
            fn.assert_not_called()

    def test_commit_and_draft_save_also_never_reach_core(self):
        """另外两个最贵的写口（整批入库 / 改草稿正文）同样不许被穿透。"""
        for url_name, dotted, data in (
            ("wiki_approval_commit", "core.wiki.commit", {}),
            ("wiki_draft_save", "core.wiki.save_draft",
             {"slug": "any-draft", "body": "正文"}),
        ):
            with self.subTest(endpoint=url_name):
                with patch(dotted) as fn:
                    self.running()
                    self.client.post(reverse(url_name), data)
                    fn.assert_not_called()


class TestMutexStaticGuard(MutexBase):
    def test_static_guard_scan_is_clean(self):
        """新增 POST 端点会自动被静态守卫扫到，而不是靠人记住清单。"""
        import runpy
        import sys
        from pathlib import Path
        p = Path(__file__).resolve().parents[2] / "scripts" / "verify_wiki_mutex_guard.py"
        # 守卫以脚本方式运行（`main()` 里 rc=1 会抛 SystemExit）。
        old = sys.argv
        sys.argv = [str(p)]
        try:
            # 守卫末尾 `sys.exit(main())`：0 = 合格，非 0 = 扫出漏守卫的写视图。
            with self.assertRaises(SystemExit) as cm:
                runpy.run_path(str(p), run_name="__main__")
            self.assertEqual(cm.exception.code, 0)
        finally:
            sys.argv = old


def views_guard():
    from apps.wiki import views
    return views._compile_running_guard()
