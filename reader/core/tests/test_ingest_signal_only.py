"""wiki 只收高质量信号（2026-11-16 用户拍板）。

用户原话两条：

  ·「审批对话压根不该进待提炼清单。」
  ·「文章的发送到 wiki 的入口还是隐藏掉，文章内的噪声太大了，考虑 wiki
    只接收划线、批注等高质量内容，这样就不需要纠结 WIKI_INGEST_ARTICLE_CHARS
    截断了。」

这一组钉三件事，每件都对应一个**已经在真实库发生**的现象：

1. `inventory()` 跳过审批对话。真实库实测：`draftbatch:current`
   （37 条消息 / 14 条是用户的话）是**全库唯一**的 todo ⇒ 每次点编译都
   白跑一次 extract、报「1 处没提炼成：draftbatch:current」，永远消不掉
   （失败不写台账 ⇒ 永远 state=new），还占掉 EXTRACT_MAX_SOURCES 一个名额。

2. **只跳审批对话**，不是把所有对话都跳。反向判据（`entry` 对话仍进清单）
   必须有：只写「不在清单里」的断言，把整个扫描删掉也能绿 —— 空集合假绿。

3. `extract_then_compile()` 真的没对 draftbatch 调 extract。第 1 条修的是
   inventory；若某天 todo 改用别的来源清单，这条会立刻转红。
   同样配一个正向断言（真 entry 被调），否则「一次都没调」也能绿。
"""
import shutil
import tempfile
from unittest.mock import patch

from django.test import TestCase, override_settings

from core import ingest
from core import wiki as wstore
from core import wiki_agent as wa
from core.models import Conversation


class Base(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-signal-only-")
        cls.ov = override_settings(WIKI_DIR=cls.tmp)
        cls.ov.enable()
        wstore.ensure_dirs()

    @classmethod
    def tearDownClass(cls):
        cls.ov.disable()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        ingest.title_of.cache_clear()
        # ★ 取标题会问 Miniflux（`sourceblock.load`）：单测不许打外网。
        p = patch.object(ingest, "title_of", return_value="测试来源")
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def keys(rows):
        return {(r["source_type"], r["source_id"]) for r in rows}


class ApprovalNotInInventoryTest(Base):
    def test_approval_conversation_never_enters_inventory(self):
        conv = wa.get_or_create_approval_conversation()
        conv.messages.create(role="user", body="这条草稿的第三段是不是太绝对了？")

        rows = ingest.inventory(limit=None)
        self.assertNotIn(("draftbatch", "current"), self.keys(rows),
                         "审批对话又进了待提炼清单 —— 每轮编译都会报"
                         "「1 处没提炼成：draftbatch:current」")

    def test_entry_conversation_still_enters_inventory(self):
        """反向判据：只跳审批对话，不能把所有对话一起跳掉。"""
        conv = Conversation.objects.create(source_type="entry", source_id="4907")
        conv.messages.create(role="user", body="我同意作者这个判断。")

        rows = ingest.inventory(limit=None)
        self.assertIn(("entry", "4907"), self.keys(rows),
                      "把正常文章的对话也跳了 ⇒ 痕迹再也提炼不了")

    def test_approval_never_shows_up_as_todo(self):
        """审批对话不出现，且不能靠「todo 恰好是空的」蒙过去。

        ★ 必须同时造一个真来源：只造审批对话的话，todo 本来就该空 ——
          那样断言「不在里面」是空集合假绿，删掉修复也能过。
        """
        conv = wa.get_or_create_approval_conversation()
        conv.messages.create(role="user", body="这段话要不要并入上一页？")
        real = Conversation.objects.create(source_type="entry", source_id="4907")
        real.messages.create(role="user", body="我同意作者这个判断。")

        todo = [r for r in ingest.inventory(limit=None)
                if r["state"] in ("new", "changed")]
        self.assertTrue(todo, "用例本身没造出任何 todo，下一条断言是空的")
        self.assertNotIn(("draftbatch", "current"), self.keys(todo))


class ExtractTodoTest(Base):
    def _run(self):
        called = []

        def fake_extract(st, sid, force=False):
            called.append((st, sid))
            return {"ok": True, "created": 0, "dropped": []}

        with patch.object(ingest, "extract", side_effect=fake_extract), \
             patch.object(ingest, "_compile_locked",
                          return_value={"ok": True, "err": "", "drafts": [],
                                        "groups": [], "dropped": [], "skipped": 0,
                                        "msg": "没有可编译的候选"}):
            ingest.extract_then_compile()
        return called

    def test_todo_skips_approval_but_keeps_real_source(self):
        wa.get_or_create_approval_conversation().messages.create(
            role="user", body="这页要不要拆？")
        conv = Conversation.objects.create(source_type="entry", source_id="4907")
        conv.messages.create(role="user", body="我不同意作者的第二个结论。")

        called = self._run()
        self.assertIn(("entry", "4907"), called,
                      "正常来源没被提炼 ⇒ 修过头了，整条提炼链被跳过")
        self.assertNotIn(("draftbatch", "current"), called,
                         "审批对话仍被拿去提炼 ⇒ extract 必然失败并报给用户")

    def test_signal_only_source_uses_no_force(self):
        """只收高质量信号 ⇒ 一条龙里的提炼不能偷偷带 force。"""
        conv = Conversation.objects.create(source_type="entry", source_id="4907")
        conv.messages.create(role="user", body="这句话是关键。")

        forces = []

        def fake_extract(st, sid, force=False):
            forces.append((st, sid, force))
            return {"ok": True, "created": 0, "dropped": []}

        with patch.object(ingest, "extract", side_effect=fake_extract), \
             patch.object(ingest, "_compile_locked",
                          return_value={"ok": True, "err": "", "drafts": [],
                                        "groups": [], "dropped": [], "skipped": 0,
                                        "msg": "没有可编译的候选"}):
            ingest.extract_then_compile()

        self.assertTrue(forces, "没造出可提炼来源，判据是空的")
        self.assertEqual([f for _, _, f in forces], [False] * len(forces),
                         "一条龙带 force = 没划线也能从正文提候选，"
                         "正是用户 2026-11-16 要关掉的那条低质量路径")
