"""审批对话「把某几条候选并入已入库页」（2026-11-13）。

实景：用户说「#217 并入"AI泡沫"页」，当时 loop 只有 read/set/polish，模型把
`set(candidates, to="ai-pao-mo")` 当成并入 —— `_set_split` 只查 `.drafts/`
快照，于是新建了一份同名普通新页草稿。那份草稿没有 `merges` 声明，
`_validate_draft` 会判「已存在同名页」永远拒收：**一次「成功」的 set 造出一份
卡住的死草稿**，模型还以为办完了，接着乱试 `confidence` 撞轮次上限。

本轮实现 `merge_into` 工具 + `ingest.attach_candidates_to_page`，判据分四层：
  1. **产物可提交**：生成的 `.drafts/<to>.md` 与目标页同名、带 `merges`、
     候选只增不减、`_validate_draft` 放行，并且**真跑一次 approve** 覆盖目标页
     且备份旧版 —— 「自动卷入审批」必须有落地证据，不能只看日志说成功。
  2. **候选守恒**：指定候选从源页移出、目标页原有候选一条不少、源页剩余候选
     一条不丢；全部并入（源页会空）要拒并给出口。
  3. **守卫会响**：`set` 的两条新建路径撞已入库页 slug 必须拒、给可执行出口，
     普通新 slug 必须放行；目标是待审草稿必须拒。
  4. **工具与可见**：merge_into 进白名单/TOOLS、`from` 回落选中、动作汇总、
     撞上限明细只出一份、标题区显示去向。

★ 空集合不假绿：每条判据先造非空数据再断言具体内容。
★ 打桩打**被调用点**：`core.wiki_loop._polish_one`（ingest 里就调这个命名空间）。
"""
import json
import re
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from core import ingest
from core import wiki as wstore
from core import wiki_agent as wa
from core import wiki_loop as wl
from core.models import IngestCandidate

PAGE = """---
title: {title}
domain: {domain}
sources: [entry:1001]
updated: 2026-11-09
confidence: low
tags: [AI泡沫, 供需]
candidates: [{cands}]
---

{body}
"""

DRAFT = """---
title: {title}
domain: ai
sources: [report:daily-abc]
updated: 2026-11-11
confidence: medium
tags: [推理算力]
candidates: [{cands}]
{extra}---

{body}
"""


def plain(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


class AttachBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-attach-")
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
        self.c202 = self.mk_cand("反AI运动拖慢建设，反而可能避免AI泡沫")
        self.c216 = self.mk_cand("中科大给新生每天 1 亿 Token、每日重置")
        self.c217 = self.mk_cand("需求加速而资本减速，瓶颈转向谁为推理付费")
        self.mk_page("ai-pao-mo", "AI泡沫页的旧结论。", [self.c202.id],
                     title="AI泡沫", domain="ai")
        self.src = "inference-compute-and-payment"
        self.mk_draft(self.src, "推理算力的供给与付费：旧结论。",
                      [self.c216.id, self.c217.id])

    def mk_cand(self, claim):
        return IngestCandidate.objects.create(
            batch="t", source_type="report", source_id="daily-abc",
            kind="surprise", claim=claim, quote=claim, quote_from="mine",
            domain="ai", confidence="medium", status="pending",
            published_at=timezone.now())

    def mk_page(self, slug, body, cands, *, title=None, domain="ai"):
        d = wstore.root() / "wiki" / domain
        d.mkdir(parents=True, exist_ok=True)
        (d / (slug + ".md")).write_text(
            PAGE.format(title=title or slug, domain=domain, body=body,
                        cands=", ".join(str(i) for i in cands)),
            encoding="utf-8")

    def mk_draft(self, slug, body, cands, *, title=None, extra=""):
        box = wstore.root() / ".drafts"
        box.mkdir(parents=True, exist_ok=True)
        (box / (slug + ".md")).write_text(
            DRAFT.format(title=title or slug, body=body,
                         cands=", ".join(str(i) for i in cands), extra=extra),
            encoding="utf-8")

    def fm(self, slug):
        p = wstore.root() / ".drafts" / (slug + ".md")
        if not p.exists():
            return None
        fm, body = wstore.parse_front(p.read_text(encoding="utf-8"))
        return fm, body

    def fake_polish(self, slug):
        """模拟「结论重编成功」：走真实 set_polish 写正文并剥 stale。"""
        info = wa.read("draft", slug)
        material = wl._authoritative_body(info)
        return wstore.set_polish(slug, "重编后的结论：%s。" % slug, material)

    def attach(self, cands=None, to="ai-pao-mo", src=None, polish=None):
        if polish is None:
            polish = self.fake_polish
        with patch("core.wiki_loop._polish_one", side_effect=polish) as p:
            ok, payload = ingest.attach_candidates_to_page(
                src or self.src,
                [self.c217.id] if cands is None else cands, to)
        return ok, payload, p


# ------------------------------------------------------------------ 1. 产物可提交
class ProductTest(AttachBase):
    def test_generates_named_merge_draft_with_conserved_candidates(self):
        ok, payload, p = self.attach()
        self.assertTrue(ok, payload)
        self.assertEqual(payload["draft"], "ai-pao-mo")
        self.assertFalse(payload["existing"], "首次并入 = 新拉一份草稿")
        self.assertEqual(payload["moved"], [self.c217.id])
        self.assertEqual(payload["added"], [self.c217.id])
        self.assertEqual(payload["kept"], [self.c216.id])

        fm, body = self.fm("ai-pao-mo")
        self.assertEqual(str(fm.get("merges") or "").strip(), "ai-pao-mo")
        self.assertEqual(wstore._cand_ids(fm), [self.c202.id, self.c217.id],
                         "目标页原有候选必须在前、一条不能少")
        self.assertEqual(str(fm.get("title")).strip(), "AI泡沫")
        self.assertEqual(str(fm.get("domain")).strip(), "ai")
        self.assertTrue(body.strip(), "polish 桩真的写了正文")
        self.assertNotIn("stale", fm, "重编成功后 stale 要被剥掉")
        self.assertIsNotNone(wstore._draft_id(fm),
                             "首次拉入就发新号，不等展示入口补")

        src_fm, _ = self.fm(self.src)
        self.assertEqual(wstore._cand_ids(src_fm), [self.c216.id])
        self.assertEqual([c.args[0] for c in p.call_args_list],
                         [self.src, "ai-pao-mo"],
                         "先重编候选减少的源页，再重编新增的合并稿")

    def test_validate_and_real_approve_overwrites_page_with_backup(self):
        """★★ 用户要的「可以提交入库更新」：真跑一次 approve，不只看日志。"""
        ok, payload, _ = self.attach()
        self.assertTrue(ok, payload)
        ok, info = wstore._validate_draft("ai-pao-mo")
        self.assertTrue(ok, info)
        self.assertEqual(info["merges"], "ai-pao-mo")
        self.assertEqual(info["dst"], wstore.root() / "wiki/ai/ai-pao-mo.md")

        approved, out = wstore.approve("ai-pao-mo")
        self.assertTrue(approved, out)
        self.assertTrue(out.get("backup"), "旧版必须备份")
        self.assertTrue((wstore.root() / out["backup"]).exists())
        page = wstore.read_page("wiki/ai/ai-pao-mo.md")
        self.assertEqual(wstore._cand_ids(page["fm"]),
                         [self.c202.id, self.c217.id])
        self.assertNotIn("AI泡沫页的旧结论", page["body"])
        self.assertFalse((wstore.root() / ".drafts/ai-pao-mo.md").exists())

    def test_second_attach_appends_into_existing_merge_draft(self):
        """★ 同一目标已有并入稿 → 追加候选，不再造第二份同名稿（用户实测 bug）。"""
        ok, first, _ = self.attach()
        self.assertTrue(ok, first)
        new, spare = self.mk_cand("另一条要并入"), self.mk_cand("留着不并入")
        self.mk_draft("other", "另一份草稿。", [new.id, spare.id])
        ok, second, p = self.attach(cands=[new.id], src="other")
        self.assertTrue(ok, second)
        self.assertTrue(second["existing"])
        self.assertEqual(second["draft"], "ai-pao-mo")
        self.assertEqual(second["added"], [new.id])
        self.assertEqual(second["kept"], [spare.id])
        fm, _ = self.fm("ai-pao-mo")
        self.assertEqual(wstore._cand_ids(fm),
                         [self.c202.id, self.c217.id, new.id],
                         "第二批候选要追加，不能覆盖第一批")
        self.assertEqual([c.args[0] for c in p.call_args_list],
                         ["other", "ai-pao-mo"])

    def test_whole_draft_merge_reuses_draft_created_by_attach(self):
        """按钮路径（整份并入）也要认已有并入稿，别从已入库页再拉一份。"""
        ok, first, _ = self.attach()
        self.assertTrue(ok, first)
        extra = self.mk_cand("整页并入的候选")
        self.mk_draft("whole", "整页稿。", [extra.id])
        with patch("core.wiki_loop._polish_one", side_effect=self.fake_polish):
            ok, payload = ingest.merge_draft_into("whole", "ai-pao-mo")
        self.assertTrue(ok, payload)
        self.assertTrue(payload["existing"])
        self.assertEqual(payload["draft"], "ai-pao-mo")
        self.assertFalse((wstore.root() / ".drafts" / "whole.md").exists(),
                         "整份并入后源稿归档")
        fm, _ = self.fm("ai-pao-mo")
        self.assertEqual(wstore._cand_ids(fm),
                         [self.c202.id, self.c217.id, extra.id])

    def test_merge_reuses_legacy_source_named_merge_draft(self):
        """旧版「原地改判」留下的并入稿叫源稿名：认 merges 字段，别按文件名再拉一份。"""
        a = self.mk_cand("旧版并入稿的候选")
        self.mk_draft("legacy", "旧版并入稿。", [a.id], extra="merges: ai-pao-mo\n")
        b = self.mk_cand("本次并入的候选")
        self.mk_draft("src2", "源稿。", [b.id])
        with patch("core.wiki_loop._polish_one", return_value=(True, "")):
            ok, payload = ingest.merge_draft_into("src2", "ai-pao-mo")
        self.assertTrue(ok, payload)
        self.assertTrue(payload["existing"])
        self.assertEqual(payload["draft"], "legacy")
        self.assertFalse((wstore.root() / ".drafts" / "ai-pao-mo.md").exists(),
                         "已有并入稿时不许再拉一份同目标草稿")
        fm, _ = self.fm("legacy")
        self.assertEqual(str(fm.get("merges") or "").strip(), "ai-pao-mo")
        self.assertEqual(wstore._cand_ids(fm), [a.id, b.id])

    def test_noop_when_target_already_has_all_candidates(self):
        """目标页已含这些候选 → _compose_page 返回空，不能生成空壳草稿。"""
        self.mk_page("ai-pao-mo", "旧结论。",
                     [self.c202.id, self.c217.id], title="AI泡沫")
        ok, payload, _ = self.attach()
        self.assertFalse(ok)
        self.assertIn("没有新增", str(payload))
        self.assertIsNone(self.fm("ai-pao-mo"))
        self.assertEqual(wstore._cand_ids(self.fm(self.src)[0]),
                         [self.c216.id, self.c217.id], "拒绝时源页一个字节没动")

    def test_merge_draft_can_detach_candidate_to_a_different_page(self):
        """2026-11-22：整页并入稿里的一条候选改投别的页（#386 → RSI相关）是合法需求。

        底层本就会收缩源稿 + 两侧重编 + 自动卷入审批；此前被 draft_direction
        一刀切挡住，等于逼人把错分类的候选一起并进原目标页。
        """
        self.mk_page("rsi-xiang-guan", "RSI 页旧结论。", [], title="RSI相关")
        self.mk_draft(self.src, "推理算力旧结论。",
                      [self.c216.id, self.c217.id],
                      extra="merges: ai-pao-mo\n")
        ok, payload, p = self.attach(cands=[self.c217.id],
                                     to="rsi-xiang-guan")
        self.assertTrue(ok, payload)
        self.assertEqual(payload["moved"], [self.c217.id])
        self.assertEqual(payload["kept"], [self.c216.id])
        self.assertEqual(p.call_count, 2, "源稿与目标稿都要重编，不是只改候选行")

        src_fm, _ = self.fm(self.src)
        self.assertEqual(wstore._cand_ids(src_fm), [self.c216.id],
                         "源稿收缩：被拆走的候选不能同时留在两边")
        self.assertEqual(str(src_fm.get("merges") or "").strip(), "ai-pao-mo",
                         "源稿仍照原去向整页并入")

        dst_fm, dst_body = self.fm("rsi-xiang-guan")
        self.assertEqual(str(dst_fm.get("merges") or "").strip(),
                         "rsi-xiang-guan")
        self.assertEqual(wstore._cand_ids(dst_fm), [self.c217.id])
        self.assertTrue(dst_body.strip(), "目标稿必须有正文，不能空着等审批")

    def test_polish_failure_leaves_visible_stale_drafts(self):
        ok, payload, _ = self.attach(
            polish=lambda s: (False, "模型没返回内容"))
        self.assertTrue(ok, "写盘成功就是成功，但失败必须可见")
        self.assertEqual(len(payload["polish_failed"]), 2)
        for slug in (self.src, "ai-pao-mo"):
            fm, body = self.fm(slug)
            self.assertEqual(str(fm.get(wstore.STALE_FM)), "true")
        # ★ 源草稿只改 candidates + 打 stale，正文仍是旧结论 —— 这是设计：
        #   「警告消失 ≠ 内容一致」，stale 就是在提醒人这段正文还没跟着
        #   候选收缩重编。真正会被入库闸门挡住的是**新建的、正文为空**的合并稿。
        self.assertIn("旧结论", self.fm(self.src)[1])
        self.assertFalse(self.fm("ai-pao-mo")[1].strip())
        ok, info = wstore._validate_draft("ai-pao-mo")
        self.assertFalse(ok, "空正文不许入库")
        self.assertIn("结论还没生成", info)


# ------------------------------------------------------------------ 2. 拒绝路径（零写入）
class RejectTest(AttachBase):
    def _no_writes(self, fn, *args, **kw):
        before = {p.name: p.read_bytes() for p in
                  (wstore.root() / ".drafts").glob("*.md")}
        ok, payload = fn(*args, **kw)
        after = {p.name: p.read_bytes() for p in
                 (wstore.root() / ".drafts").glob("*.md")}
        self.assertFalse(ok, payload)
        self.assertEqual(before, after, "被拒时 .drafts/ 一个字节都不许动")
        return str(payload)

    def test_target_must_be_published_page(self):
        self.assertIn("待审草稿", self._no_writes(
            ingest.attach_candidates_to_page, self.src, [self.c217.id], self.src))
        self.assertIn("库里没有这一页", self._no_writes(
            ingest.attach_candidates_to_page, self.src, [self.c217.id], "没这页"))

    def test_unknown_or_duplicate_or_noninteger_candidates(self):
        for ids, needle in (([999999], "不在草稿"),
                            ([], "没有指定"),
                            (["abc"], "cand_ids 必须是")):
            self.assertIn(needle, self._no_writes(
                ingest.attach_candidates_to_page, self.src, ids, "ai-pao-mo"))

    def test_all_candidates_must_use_the_button_instead(self):
        msg = self._no_writes(ingest.attach_candidates_to_page, self.src,
                              [self.c216.id, self.c217.id], "ai-pao-mo")
        self.assertIn("改为并入", msg)

    def test_existing_target_draft_is_never_overwritten(self):
        self.mk_draft("ai-pao-mo", "人没审过的内容。", [self.c216.id])
        self.assertIn("先处理它", self._no_writes(
            ingest.attach_candidates_to_page, self.src, [self.c217.id], "ai-pao-mo"))

    def test_detach_to_its_own_merge_target_is_rejected(self):
        """拆给自己当前的并入目标 = `_merge_draft_for` 会把它加回去，必须拒。"""
        self.mk_draft("already-merges", "已改判。", [self.c217.id],
                      extra="merges: ai-pao-mo\n")
        msg = self._no_writes(ingest.attach_candidates_to_page,
                              "already-merges", [self.c217.id], "ai-pao-mo")
        self.assertIn("整页并入的目标就是", msg)
        self.assertIn("改为并入", msg, "拒了要给出路，别只说不行")

    def test_destructive_direction_is_still_rejected(self):
        """放行只限 merges；edits/retire/delete 等破坏性方向仍一刀切拒。"""
        for extra, key in (("edits: ai-pao-mo\n", "edits"),
                           ("retire: ai-pao-mo\n", "retire")):
            self.mk_draft("directed", "已改判。", [self.c217.id], extra=extra)
            msg = self._no_writes(ingest.attach_candidates_to_page,
                                  "directed", [self.c217.id], "ai-pao-mo")
            self.assertIn("已有明确去向（%s）" % key, msg)

    def test_resolve_target_accepts_slug_or_exact_title(self):
        self.assertEqual(ingest.resolve_merge_target("ai-pao-mo"), ("ai-pao-mo", ""))
        self.assertEqual(ingest.resolve_merge_target("AI泡沫"), ("ai-pao-mo", ""))
        self.mk_page("rsi-xiang-guan", "另一页。", [self.c216.id],
                     title="AI泡沫", domain="ai")
        slug, err = ingest.resolve_merge_target("AI泡沫")
        self.assertEqual(slug, "")
        self.assertIn("对应多页", err)
        self.assertEqual(ingest.resolve_merge_target("ai-pao-mo"),
                         ("ai-pao-mo", ""), "slug 精确命中优先于重名标题")


# ------------------------------------------------------------------ 3. 撞名守卫
class GuardTest(AttachBase):
    def test_split_to_published_slug_is_rejected_with_actionable_hint(self):
        ok, payload = wa.set("candidates", {
            "from": self.src, "to": "ai-pao-mo",
            "move": [self.c217.id], "domain": "ai"})
        self.assertFalse(ok)
        self.assertIn("是已入库页", payload)
        self.assertIn("merge_into", payload)
        self.assertIsNone(self.fm("ai-pao-mo"), "不许造出同名死草稿")

    def test_merge_to_published_slug_is_rejected(self):
        self.mk_draft("second", "另一份草稿。", [self.c216.id])
        ok, payload = wa.set("merge", {
            "pages": [self.src, "second"], "to": "ai-pao-mo", "domain": "ai"})
        self.assertFalse(ok)
        self.assertIn("是已入库页", payload)
        self.assertIsNone(self.fm("ai-pao-mo"))

    def test_ordinary_new_slug_still_allowed(self):
        with patch("core.wiki_loop._polish_one", side_effect=self.fake_polish):
            ok, payload = wa.set("candidates", {
                "from": self.src, "to": "quan-xin-de-ye",
                "move": [self.c217.id], "domain": "ai"})
        self.assertTrue(ok, payload)
        self.assertIsNotNone(self.fm("quan-xin-de-ye"), "守卫不许误伤正常拆页")

    def test_both_new_draft_paths_call_the_guard(self):
        """★★ 同类扫全集：守卫必须挂在 _set_split 与 _set_merge 两处。"""
        src = (Path(wa.__file__).read_text(encoding="utf-8"))
        self.assertEqual(src.count("if collision := _published_dst_error(dst, before):"),
                         2, "新建草稿的两条路径都要过守卫")


# ------------------------------------------------------------------ 4. 工具与可见
class LoopToolTest(AttachBase):
    def test_tool_is_in_schema_and_whitelist(self):
        self.assertEqual(sorted(wl.ALLOWED_TOOLS),
                         ["merge_into", "polish", "read", "set"])
        self.assertEqual([t["function"]["name"] for t in wl.TOOLS][-1], "merge_into")
        self.assertIn("merge_into", wl.SYSTEM)
        self.assertIn("merge_into", json.dumps(wl.TOOLS, ensure_ascii=False))

    def test_exec_tool_falls_back_to_selected_slug(self):
        with patch("core.wiki_loop._polish_one", side_effect=self.fake_polish), \
             patch("core.ingest.attach_candidates_to_page",
                   wraps=ingest.attach_candidates_to_page) as core_fn:
            res = wl._exec_tool(
                {"function": {"name": "merge_into", "arguments": json.dumps({
                    "cand_ids": [self.c217.id], "to": "AI泡沫"})}},
                [], selected_slug=self.src)
        self.assertTrue(res["ok"], res)
        self.assertEqual(core_fn.call_args.args[0], self.src,
                         "from 省略时回落当前选中")
        self.assertEqual(res["result"]["draft"], "ai-pao-mo")

    def test_action_lines_and_log_note_report_the_new_draft(self):
        with patch("core.wiki_loop._polish_one", side_effect=self.fake_polish):
            actions = []
            wl._exec_tool(
                {"function": {"name": "merge_into", "arguments": json.dumps({
                    "from": self.src, "cand_ids": [self.c217.id], "to": "ai-pao-mo"})}},
                actions, selected_slug=self.src)
        text = wl._action_lines(actions)
        self.assertIn("merge_into(#", text)
        self.assertIn("已生成 .drafts/ai-pao-mo", text)
        self.assertIn("源页留 1 条候选", text)
        self.assertIn("merge_into 1/1 成功", wl._log_note("试一下", actions))

    def test_rejected_tool_call_returns_reason_for_next_turn(self):
        actions = []
        res = wl._exec_tool(
            {"function": {"name": "merge_into", "arguments": json.dumps({
                "from": self.src, "cand_ids": [self.c217.id], "to": self.src})}},
            actions, selected_slug=self.src)
        self.assertFalse(res["ok"])
        self.assertIn("待审草稿", str(res["result"]))
        self.assertIn("被拒", wl._action_lines(actions))

    def test_max_turns_action_detail_appears_only_once(self):
        """撞上限时【本轮动作】与 reply 曾把同一串动作打印两遍。"""
        from core import wiki_agent as _wa
        tc = {"function": {"name": "read", "arguments": '{"scope":"drafts"}'},
              "id": "c1"}
        with patch.object(wl, "_call", return_value=("", [tc], "")):
            res = wl.run("一直调")
        self.assertIn("轮次上限", res["err"])
        body = _wa.get_or_create_approval_conversation().messages \
            .filter(role="assistant").last().body
        self.assertEqual(body.count("read(drafts) → 成功"), wl.MAX_TURNS,
                         "动作明细只出一份，份数应等于实际轮次")
        self.assertIn("本轮已执行的动作见上", body)


class ViewRefreshTest(AttachBase):
    """「自动卷入」必须看得见：对话发完一句话，列表 + 工作区 OOB 跟着刷。"""

    def test_merge_into_counts_as_panel_write_but_read_does_not(self):
        from apps.wiki.views import _set_succeeded
        action = {"tool": "merge_into", "ok": True,
                  "result": {"to": "ai-pao-mo"}}
        self.assertTrue(_set_succeeded({"ok": True, "actions": [action]}))
        self.assertFalse(_set_succeeded(
            {"ok": True, "actions": [{"tool": "read", "ok": True}]}),
            "纯 read 不该刷面板")
        self.assertFalse(_set_succeeded(
            {"ok": False, "actions": [action]}), "整轮失败不该刷面板")
        self.assertFalse(_set_succeeded(
            {"ok": True, "actions": [dict(action, ok=False)]}),
            "被拒的写动作不该刷面板")

    def test_chat_send_switches_workarea_to_the_new_merge_draft(self):
        from django.test import Client
        ok, payload, _ = self.attach()
        self.assertTrue(ok, payload)
        res = {"ok": True, "err": "", "reply": "已生成 .drafts/ai-pao-mo.md。",
               "actions": [{"tool": "merge_into", "args": {}, "ok": True,
                            "result": payload, "repolished": []}]}

        def fake_run(question, selected_slug=None):
            # run 正常会落库 user/assistant，桩也要按同一形态落，否则对话区空白。
            conv = wa.get_or_create_approval_conversation()
            conv.messages.create(role="user", body=question)
            conv.messages.create(role="assistant",
                                 body="【本轮动作】\n" + wl._action_lines(res["actions"])
                                 + "\n\n" + res["reply"])
            return res

        with patch("apps.wiki.views.wiki_loop.run", side_effect=fake_run) as run:
            resp = Client().post("/wiki/approval/chat/send",
                                 {"body": "#217 并入 AI泡沫页",
                                  "selected_slug": self.src})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(run.call_count, 1)
        html = resp.content.decode("utf-8")
        self.assertEqual(html.count('hx-swap-oob="true"'), 3,
                         "列表 / 工作区 / 头部三个 OOB 片段都要刷")
        self.assertIn('id="approval-list"', html)
        self.assertIn('id="approval-workarea"', html)
        self.assertIn("并入 wiki/ai/ai-pao-mo.md", plain(html))
        self.assertIn('name="selected_slug" value="ai-pao-mo"', html,
                      "选中要跟着切到新生成的合并稿")
        self.assertIn("merge_into(#", html, "对话区要显示本轮动作")
        self.assertNotIn("{% ", html, "模板注释泄漏")
        self.assertNotIn("endcomment", html, "模板注释泄漏")


class TitleTemplateTest(AttachBase):
    def _render(self, slug, merges="", edits="", delete=False):
        ctx = {"slug": slug, "title": "AI泡沫", "candidates": [1, 2],
               "merges": merges, "edits": edits, "delete": delete,
               "merge_page": {"page": "wiki/ai/ai-pao-mo.md"},
               "edit": False, "mutex_msg": ""}
        return plain(render_to_string("wiki/_approval_title.html", ctx,
                                      request=RequestFactory().get("/")))

    def test_merge_draft_shows_target_and_explains_filename(self):
        text = self._render("tui-li-fu-fei-de-ping-jing", merges="ai-pao-mo")
        self.assertIn("草稿 tui-li-fu-fei-de-ping-jing.md", text)
        self.assertIn("并入 wiki/ai/ai-pao-mo.md", text)
        self.assertIn("草稿名沿用改判前的文件", text)

    def test_named_merge_draft_has_no_confusing_note(self):
        text = self._render("ai-pao-mo", merges="ai-pao-mo")
        self.assertIn("并入 wiki/ai/ai-pao-mo.md", text)
        self.assertNotIn("草稿名沿用改判前的文件", text)

    def test_ordinary_new_page_has_no_direction_noise(self):
        text = self._render("xin-ye")
        self.assertNotIn("并入", text)
        self.assertIn("2 条候选", text)
