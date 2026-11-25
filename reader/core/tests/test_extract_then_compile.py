"""第十一步：`extract_then_compile()` 一条龙的编排判据（2026-11-08）。

盯的是「提炼隐身」之后**没人再看得见中间过程**这件事：一条龙把 N 个来源的
提炼和一次编译串在同一次点击里，任何一处静默（少提炼一个来源、守卫丢了候选
不报、提炼阶段没有状态）都不会有人发现。所以每条判据都要求返回值**如实报数**。

打桩纪律：
  - `extract` / `_compile_locked` 都打桩 → 零模型调用、零花钱；
  - 但「桩真被用上」必须有反证：`test_drafts_do_not_pause` 里的断言要配
    `test_stub_really_used_without_drafts`，否则也可能是链路压根没通（假绿）。
  - patch 的是**被调用点的命名空间**（`core.ingest.extract`），不是定义处别名。
"""
import shutil
import tempfile
from unittest.mock import patch

from django.test import TestCase, override_settings

from core import ingest
from core import wiki as wstore
from core.models import Annotation, IngestCandidate

SID1, SID2 = "7101", "7102"

OK_COMPILE = {"ok": True, "err": "", "drafts": ["page-one"],
              "groups": [], "dropped": [], "skipped": 0}


def _draft_text():
    return ("---\ntitle: 待审批页\ndomain: invest\nsources: [entry:7101]\n"
            "updated: 2026-11-08\nconfidence: medium\ntags: []\n"
            "candidates: [999999]\n---\n\n正文\n")


class OneShotBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-step11-")
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

    def mk_sources(self, n=2):
        """造 n 个「有我的字」的来源 → inventory() 里 state=new。"""
        for sid in (SID1, SID2)[:n]:
            Annotation.objects.create(source_type="entry", source_id=sid,
                                      body="我认为这条值得沉淀 %s" % sid)
        return [r for r in ingest.inventory()
                if r["state"] in ("new", "changed")]

    def add_draft(self, slug="d1"):
        p = wstore.root() / ".drafts" / f"{slug}.md"
        p.write_text(_draft_text(), encoding="utf-8")
        return p

    @staticmethod
    def ok_extract(created=1, dropped=None):
        return {"ok": True, "err": "", "created": created,
                "dropped": dropped or [], "batch": "b"}


class OneShotHappyTest(OneShotBase):
    def test_two_new_sources_then_compile(self):
        rows = self.mk_sources(2)
        self.assertEqual(len(rows), 2, "前置：得真有两个 new 来源")
        with patch("core.ingest.extract",
                   return_value=self.ok_extract()) as ext, \
                patch("core.ingest._compile_locked",
                      return_value=dict(OK_COMPILE)) as locked:
            res = ingest.extract_then_compile()
        self.assertTrue(res["ok"], res.get("err"))
        self.assertEqual(res["extracted"], 2)
        self.assertEqual(ext.call_count, 2)
        self.assertEqual({c.args[1] for c in ext.call_args_list}, {SID1, SID2})
        locked.assert_called_once_with()
        self.assertEqual(res["drafts"], ["page-one"])

    def test_no_new_source_still_compiles(self):
        """0 处新痕迹不是失败：已有候选照样要能编，不能把编译入口关掉。"""
        IngestCandidate.objects.create(
            batch="b", source_type="entry", source_id=SID1, kind="decision",
            claim="一条判断", quote="我的原话", status="pending")
        with patch("core.ingest.extract") as ext, \
                patch("core.ingest._compile_locked",
                      return_value=dict(OK_COMPILE)) as locked:
            res = ingest.extract_then_compile()
        self.assertTrue(res["ok"], res.get("err"))
        self.assertEqual(res["extracted"], 0)
        ext.assert_not_called()
        locked.assert_called_once_with()

    def test_created_zero_still_counts_as_processed(self):
        """★ 0 条候选是合法结论，不是失败；台账已更新，下次不该重复花钱。"""
        self.mk_sources(1)
        with patch("core.ingest.extract",
                   return_value=self.ok_extract(created=0)), \
                patch("core.ingest._compile_locked", return_value=dict(OK_COMPILE)):
            res = ingest.extract_then_compile()
        self.assertEqual(res["extracted"], 1)
        self.assertEqual(res["extract_skipped"], [])

    def test_extract_dropped_is_reported_not_silenced(self):
        """★ 提炼阶段守卫丢的候选要汇总，否则「提炼 2 处、编出 1 页」会骗人。"""
        self.mk_sources(2)
        dropped = [{"cand_id": 1, "why": "引用对不上"}] * 3
        with patch("core.ingest.extract",
                   return_value=self.ok_extract(dropped=dropped)), \
                patch("core.ingest._compile_locked", return_value=dict(OK_COMPILE)):
            res = ingest.extract_then_compile()
        self.assertEqual(res["extract_dropped"], 6)

    def test_single_source_failure_does_not_abort_round(self):
        """★ 一个来源字数不够是它自己的事，记 skipped 继续；整轮仍要编译。"""
        self.mk_sources(2)

        def fake(st, sid, force=False):
            if sid == SID1:
                return {"ok": False, "err": "这篇没有可沉淀的信号（字数不够）",
                        "dropped": []}
            return self.ok_extract()

        with patch("core.ingest.extract", side_effect=fake) as ext, \
                patch("core.ingest._compile_locked",
                      return_value=dict(OK_COMPILE)) as locked:
            res = ingest.extract_then_compile()
        self.assertEqual(ext.call_count, 2)
        self.assertEqual(res["extracted"], 1)
        self.assertEqual(len(res["extract_skipped"]), 1)
        self.assertEqual(res["extract_skipped"][0]["source_id"], SID1)
        self.assertIn("字数不够", res["extract_skipped"][0]["err"])
        locked.assert_called_once_with()

    def test_zero_candidates_after_extract_is_not_a_failure(self):
        """★ 提炼跑完却 0 条候选 = 合法结论，不能红着「编译未完成」（2026-11-09）。

        按钮对新痕迹开放后这条路可达：痕迹 → 提炼 → 模型说「没有可沉淀的判断」
        → 候选池空。`_compile_locked` 的空跑返回 no_candidates 标记，编排层
        必须转成 ok=True + 如实 msg，而不是把合法结论误报成失败。
        """
        self.mk_sources(1)
        with patch("core.ingest.extract",
                   return_value=self.ok_extract(created=0)), \
                patch("core.ingest._compile_locked",
                      return_value={"ok": False, "err": "没有未丢弃的候选可编译",
                                    "drafts": [], "groups": [], "dropped": [],
                                    "skipped": 0, "no_candidates": True}):
            res = ingest.extract_then_compile()
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["err"], "")
        self.assertIn("没有可沉淀的判断", res["msg"])
        self.assertEqual(res["extracted"], 1)


class OneShotPausedTest(OneShotBase):
    """★ 2026-11-09 用户澄清：审批未完成时拒绝新一轮提炼+编译。"""

    def test_drafts_pause_the_round(self):
        """有草稿待审批时，extract_then_compile 必须被暂停（2026-11-09 恢复）。"""
        self.add_draft()
        self.mk_sources(2)
        with patch("core.ingest.extract",
                   return_value=self.ok_extract()) as ext, \
                patch("core.ingest._compile_locked",
                      return_value=dict(OK_COMPILE)) as locked:
            res = ingest.extract_then_compile()
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("paused"))
        self.assertIn("审批进行中", res["err"])
        ext.assert_not_called()
        locked.assert_not_called()

    def test_stub_really_used_without_drafts(self):
        """★ 桩真被用上的反证：无草稿时同一条链路也真的跑到 extract。

        没有这条，上面 `call_count == 0` 也可能只是巧合 —— 那种「假绿」
        比不测更贵。
        """
        self.mk_sources(2)
        with patch("core.ingest.extract",
                   return_value=self.ok_extract()) as ext, \
                patch("core.ingest._compile_locked", return_value=dict(OK_COMPILE)):
            res = ingest.extract_then_compile()
        self.assertTrue(res["ok"], res.get("err"))
        self.assertEqual(ext.call_count, 2)


class OneShotLockTest(OneShotBase):
    def test_busy_lock_returns_before_any_extract(self):
        """★ 编译锁拿不到 = 整轮级失败，当场返回，不逐个 extract。"""
        self.mk_sources(2)
        held = ingest._try_lock_compile()
        self.assertIsNotNone(held, "前置：本测试要先占住锁")
        try:
            with patch("core.ingest.extract") as ext, \
                    patch("core.ingest._compile_locked") as locked:
                res = ingest.extract_then_compile()
            self.assertFalse(res["ok"])
            self.assertIn("还在跑", res["err"])
            ext.assert_not_called()
            locked.assert_not_called()
        finally:
            ingest._unlock_compile(held)

    def test_same_round_runs_after_lock_released(self):
        """★ 上一条的反证：放锁后同一条链路真的能跑通。"""
        self.mk_sources(2)
        with patch("core.ingest.extract",
                   return_value=self.ok_extract()) as ext, \
                patch("core.ingest._compile_locked", return_value=dict(OK_COMPILE)):
            res = ingest.extract_then_compile()
        self.assertTrue(res["ok"], res.get("err"))
        self.assertEqual(ext.call_count, 2)

    def test_goes_directly_to_compile_locked_not_compile_candidates(self):
        """★ 锁结构选 A 的变异反证：若改回调 `compile_candidates`，本条必须红。

        `compile_candidates` 内部还会再拿一次同一把非阻塞锁 —— 一条龙已经持锁，
        再走它就会「自己挡自己」，整轮白跑。
        """
        def forbidden(*_a, **_kw):
            raise AssertionError("一条龙不许经过 compile_candidates（会自己挡自己）")

        with patch("core.ingest.compile_candidates", side_effect=forbidden), \
                patch("core.ingest._compile_locked", return_value=dict(OK_COMPILE)) as locked:
            res = ingest.extract_then_compile()
        self.assertTrue(res["ok"], res.get("err"))
        locked.assert_called_once_with()

    def test_two_entries_share_the_same_busy_wording(self):
        """★ 同口径两处：两条入口被闸挡下时说的是同一句话（判据收一处）。"""
        held = ingest._try_lock_compile()
        try:
            a = ingest.compile_candidates()
            b = ingest.extract_then_compile()
        finally:
            ingest._unlock_compile(held)
        self.assertFalse(a["ok"])
        self.assertFalse(b["ok"])
        self.assertEqual(a["err"], b["err"])


class OneShotStateTest(OneShotBase):
    def test_state_is_running_throughout_extract_phase(self):
        """★ 技术设计补漏：提炼阶段也必须有「在跑」状态。

        `compile_state()` 先读状态文件、文件不在就返回空闲（**不探锁**）。
        提炼要好几分钟，只在 `_compile_locked` 里写状态的话，这一整段页面都读成
        空闲 —— 硬锁方向二（编译中挡审批/编辑）就形同虚设。
        """
        self.mk_sources(1)
        seen = {}

        def fake_extract(*_a, **_kw):
            seen["extract"] = ingest.compile_state()
            return self.ok_extract()

        def fake_compile(*_a, **_kw):
            seen["compile"] = ingest.compile_state()
            return dict(OK_COMPILE)

        with patch("core.ingest.extract", side_effect=fake_extract), \
                patch("core.ingest._compile_locked", side_effect=fake_compile):
            res = ingest.extract_then_compile()
        self.assertTrue(res["ok"], res.get("err"))
        self.assertTrue(seen["extract"]["running"], seen["extract"])
        self.assertEqual(seen["extract"]["phase"], "extract")
        self.assertTrue(seen["compile"]["running"], seen["compile"])
        self.assertEqual(seen["compile"]["phase"], "compile")
        # ★ 开始时间不能被编译阶段重写（_mark_running 的 setdefault 语义）
        self.assertEqual(seen["compile"]["started_at"],
                         seen["extract"]["started_at"])

    def test_state_cleared_and_lock_released_after_round(self):
        """跑完必须收尾：状态消失 + 锁可再拿（否则页面永远灰着 / 永久锁死）。"""
        with patch("core.ingest._compile_locked", return_value=dict(OK_COMPILE)):
            ingest.extract_then_compile()
        st = ingest.compile_state()
        self.assertFalse(st["running"])
        # ★ 只验 running 不够：状态文件没清时「文件在 + 锁空」会走 aborted 分支，
        #   running 同样是 False ——「每次编译完都假报上次没跑完」就能悄悄蒙混过关。
        #   这条是变异反证 V6 抓出来的松断言。
        self.assertEqual(st["aborted"], {})
        fd = ingest._try_lock_compile()
        self.assertIsNotNone(fd)
        ingest._unlock_compile(fd)

    def test_state_cleared_even_when_extract_raises(self):
        """异常也不能留着状态文件 / 锁（finally 必须真的兜住）。"""
        self.mk_sources(1)
        with patch("core.ingest.extract", side_effect=RuntimeError("模拟炸了")):
            with self.assertRaises(RuntimeError):
                ingest.extract_then_compile()
        st = ingest.compile_state()
        self.assertFalse(st["running"])
        self.assertEqual(st["aborted"], {})
        fd = ingest._try_lock_compile()
        self.assertIsNotNone(fd)
        ingest._unlock_compile(fd)


class ExtractCapTest(OneShotBase):
    """★ 一次最多提炼 `EXTRACT_MAX_SOURCES` 个来源 —— 但差额必须报出来。

    2026-11-09 清零重来后暴露：这个上限此前**不存在**，是 `inventory()` 默认
    `limit=30` 被顺手当成的上限，超出的来源一个字都不报（真实库 33 个来源 →
    3 个静默不提炼）。上限本身合理（那是钱），静默才是不允许的。
    """

    def mk_many(self, n, base=7200):
        for i in range(n):
            Annotation.objects.create(source_type="entry", source_id=str(base + i),
                                      body="第 %d 条我认为值得沉淀" % i)
        return [r for r in ingest.inventory(limit=None)
                if r["state"] in ("new", "changed")]

    def test_cap_leaves_the_rest_and_reports_it(self):
        rows = self.mk_many(5)
        self.assertEqual(len(rows), 5, "前置：得真有 5 个 new 来源")
        with patch.object(ingest, "EXTRACT_MAX_SOURCES", 2), \
                patch("core.ingest.extract",
                      return_value=self.ok_extract()) as ext, \
                patch("core.ingest._compile_locked",
                      return_value=dict(OK_COMPILE)):
            res = ingest.extract_then_compile()
        # 反证「桩真被用上」：正好提炼 2 个，不是 0 个也不是 5 个
        self.assertEqual(ext.call_count, 2)
        self.assertEqual(res["extracted"], 2)
        self.assertEqual(res["extract_left"], 3, "剩 3 个必须如实报")

    def test_under_cap_no_leftover(self):
        """反证：没超上限时 `extract_left` 必须是 0，否则人白读一句提示。"""
        self.mk_many(3)
        with patch("core.ingest.extract", return_value=self.ok_extract()) as ext, \
                patch("core.ingest._compile_locked", return_value=dict(OK_COMPILE)):
            res = ingest.extract_then_compile()
        self.assertEqual(ext.call_count, 3)
        self.assertEqual(res["extract_left"], 0)


class ExtractCutTest(OneShotBase):
    """★ 提炼输出被预算截断的来源：单独计数，**不算提炼成功**（2026-11-09）。

    `extract()` 截断时不写台账（否则这篇被标成「提炼过了」，少掉的那部分永久
    蒸发），所以它下次还会被提炼 —— 这一轮既不能算进 `extracted`（人会以为
    处理完了），也不能混进 `extract_skipped`（那不是失败）。
    """

    def fake(self, cut_sid):
        def _f(st, sid, force=False):
            if sid == cut_sid:
                return {"ok": True, "err": "", "created": 2, "dropped": [],
                        "batch": "b", "truncated": True}
            return self.ok_extract()
        return _f

    def test_truncated_source_not_counted_as_extracted(self):
        self.mk_sources(2)
        with patch("core.ingest.extract", side_effect=self.fake(SID1)) as ext, \
                patch("core.ingest._compile_locked", return_value=dict(OK_COMPILE)):
            res = ingest.extract_then_compile()
        self.assertEqual(ext.call_count, 2, "前置：两处都要提炼过")
        self.assertEqual(res["extracted"], 1, "截断的那处不算提炼成功")
        self.assertEqual(res["extract_cut"], 1)
        self.assertEqual(res["extract_skipped"], [], "截断不是失败，不许混进 skipped")

    def test_no_truncation_zero(self):
        """反证：没截断时必须是 0，否则人天天读一句「正文太长」。"""
        self.mk_sources(2)
        with patch("core.ingest.extract", return_value=self.ok_extract()), \
                patch("core.ingest._compile_locked", return_value=dict(OK_COMPILE)):
            res = ingest.extract_then_compile()
        self.assertEqual(res["extracted"], 2)
        self.assertEqual(res["extract_cut"], 0)
