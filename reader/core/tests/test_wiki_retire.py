"""第十步：删页提案的候选去向编排（2026-11-06）。

命题：**「这条判断还留不留」是纯判断，本该人点一下；结果人得绕道拆整页 /
删整页 / 从备份恢复，找清单、判落点、动手改三件脏活全推回给人。**

这一组盯住「把粒度从页降到条」新引入的每一种静默失败：

  1. 废掉的候选是**终态**（`discarded`），不能「回池待审」让删掉的判断复活。
  2. ★★ 两处「回池」是反义的：`wiki._release_candidates` 改 status，
     `wiki_agent._set_discard` 只移草稿。用错不报错，只会过几天复活。
  3. 守恒守卫**自己会响**：交集 / 差集 / 幽灵 / 越界四种坏数据都要被点名。
  4. 写盘前预演：拒写一个字节不动；commit 还要再验一次跨稿守恒。
  5. 权威清单只有一个来源 = 被删页 frontmatter（页面还在时）；删除稿清空了
     `candidates`，认它就会得到空清单。
  6. 旧格式删除稿回退**要说出来**，不能静默「看起来逐条编排生效了」。
  7. 第八步契约一字不动：`is_delete_fm` / 空候选窄豁免 / `dangling`。
  8. 零 LLM：本步没有花钱端点，测试里的桩要配「桩自己会响」的反证。

★ 全程零模型：推荐去向靠确定性相似度，搬迁靠 `pull_page` 形状的 `edits:` 稿。
★ 形态 A：正文 = 纯 polish 一段（不写「候选清单 / 通顺话」双层标题），frontmatter
  `candidates` 是分组唯一真值。程序新建页草稿**正文留空**（半成品）；`_validate_draft`
  新增「正文为空 + 有候选 → 拒入库」闸门，所以本文件里凡是要 commit 的新建稿都得
  先用 `set_polish`（程序写、零 LLM）写回结论。下方 `PAGE` 只是清零前老页的种子
  数据（含旧双层标题、正文非空），用于让删页 / 搬迁稿不受空正文闸门影响。
"""
import json
import shutil
import tempfile
from unittest.mock import patch

from django.test import TestCase, override_settings

from core import ingest
from core import wiki as wstore
from core import wiki_agent as wa
from core import wiki_loop
from core.models import IngestCandidate
from core.tests.test_ingest_compile import mk_cand

PAGE = """---
title: {title}
domain: {domain}
sources: [art-{slug}]
updated: 2026-11-06
confidence: high
tags: []
candidates: {cands}
---

## 候选清单

{body}

## 通顺话

一段通顺的话。
"""

VICTIM = "obsolete-moat"
KEEPER = "harness-cost"


class RetireBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-wiki-10-")
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
        # ★ 前置自包含：候选行、被删页、目标页全在这里造，不靠隔离库的历史残留
        #   （第八步踩过「种子引用不存在的候选，e2e 靠残留才绿」）。
        self.c1 = mk_cand("规模带来议价能力", "护城河来自规模", domain="invest")
        self.c2 = mk_cand("单位成本逐年下降", "成本曲线在下降", domain="invest")
        self.c3 = mk_cand("渠道被新平台分流", "渠道优势被削弱", domain="invest")
        self.c4 = mk_cand("推理成本是主要开销", "端侧推理要算成本", domain="invest")
        self.ids = [self.c1.id, self.c2.id, self.c3.id]
        self.mk_page(VICTIM, cands=self.ids, title="过时的护城河判断")
        self.mk_page(KEEPER, cands=(self.c4.id,), title="成本工程")

    # ---------------- 造数据 ----------------
    def mk_page(self, slug, *, domain="invest", cands=(), body=None, title=None):
        text = PAGE.format(
            title=title or slug, domain=domain, slug=slug,
            cands=json.dumps(list(cands), ensure_ascii=False),
            body=body if body is not None else f"- 一条判断（候选 {slug}）")
        p = wstore.root() / "wiki" / domain / f"{slug}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def mk_draft(self, slug, *, domain="invest", cands="[1]", extra="",
                 body="- 一条判断"):
        text = PAGE.format(title=slug, domain=domain, slug=slug, cands=cands,
                           body=body)
        if extra:
            text = text.replace("\n---\n", "\n%s---\n" % extra, 1)
        (wstore.root() / ".drafts" / f"{slug}.md").write_text(text, encoding="utf-8")
        return text

    def draft_text(self, slug):
        return (wstore.root() / ".drafts" / f"{slug}.md").read_text(encoding="utf-8")

    def draft_fm(self, slug):
        return wstore.parse_front(self.draft_text(slug))[0]

    def draft_files(self):
        return sorted(p.name for p in (wstore.root() / ".drafts").glob("*.md"))

    def page_text(self, slug, domain="invest"):
        return (wstore.root() / "wiki" / domain / f"{slug}.md").read_text(
            encoding="utf-8")

    def snapshot(self):
        """「一个字节不动」的证据：草稿区 + 已入库页的全部文本。"""
        r = wstore.root()
        return {p.relative_to(r).as_posix(): p.read_text(encoding="utf-8")
                for p in sorted(list((r / ".drafts").glob("*.md"))
                                + list((r / "wiki").rglob("*.md")))}

    def propose(self):
        ok, payload = wstore.request_delete(VICTIM)
        self.assertTrue(ok, payload)
        return payload

    def set_retire(self, ids):
        p = wstore.root() / ".drafts" / f"{VICTIM}.md"
        p.write_text(wstore._set_fm_line(
            self.draft_text(VICTIM), "retire",
            "[" + ", ".join(str(i) for i in ids) + "]"), encoding="utf-8")

    def live_ids(self):
        """「还会不会被编译」的**状态**口径，不是 `compile_backlog` 的 todo。

        ★ 这里所有候选都已挂在已入库页的 frontmatter 里，`compile_backlog`
          会按 `done_ids` 把它们全剔掉（面板要说「还有没有活」）。拿 todo
          当「候选还活着」会得到一个恒空集合，测试反而静默失效。
        """
        from core.models import INGEST_LIVE_STATUS
        return set(IngestCandidate.objects.filter(
            status__in=INGEST_LIVE_STATUS).values_list("id", flat=True))


# ================================================================ 状态机 / 语义
class RetireStateTest(RetireBase):
    def test_retired_candidate_is_not_recompiled(self):
        """① 废掉的候选不再进 `compile_backlog()` —— 删掉的页不会原地复活。"""
        self.propose()
        ok, payload = wstore.commit()
        self.assertTrue(ok, payload)
        for c in (self.c1, self.c2, self.c3):
            c.refresh_from_db()
            self.assertEqual(c.status, "discarded")
        live = self.live_ids()
        todo = [c.id for c in ingest.compile_backlog()["cands"]]
        for cid in self.ids:
            self.assertNotIn(cid, live, "废弃是终态，不该再进编译白名单")
            self.assertNotIn(cid, todo)
        self.assertIn(self.c4.id, live, "目标页原有候选不该被牵连")

    def test_release_semantics_is_discarded_not_back_to_pool(self):
        """② ★★ 两处「回池」是反义的：只移草稿 ≠ 废弃，用错不报错、只会复活。"""
        self.mk_draft("tmp", cands=f"[{self.c1.id}]")
        ok, res = wa.set("discard", {"slug": "tmp"})
        self.assertTrue(ok, res)
        self.c1.refresh_from_db()
        self.assertNotEqual(self.c1.status, "discarded",
                            "_set_discard 只移草稿，不改 status")
        self.assertIn(self.c1.id, self.live_ids(),
                      "误用这个口径，删掉的判断下次编译会再被编出来")

        n, err = wstore._release_candidates([self.c1.id])
        self.assertEqual((n, err), (1, ""))
        self.c1.refresh_from_db()
        self.assertEqual(self.c1.status, "discarded")
        self.assertNotIn(self.c1.id, self.live_ids())

    def test_eighth_step_contract_unchanged(self):
        """⑦ 第八步契约一字不动：条数 / 空形态 / 窄豁免 / dangling。"""
        payload = self.propose()
        self.assertEqual(payload["candidates"], 3,
                         "candidates 仍是条数（第八步 e2e 钉着），清单另给 retire")
        self.assertEqual(payload["retire"], self.ids, "默认全废 = 第八步现状")
        fm = self.draft_fm(VICTIM)
        self.assertTrue(wstore.is_delete_fm(fm))
        self.assertEqual(fm.get("candidates"), [])
        self.assertEqual(wstore.missing_fm(fm), [], "删页稿的空候选豁免要继续生效")

        # 普通空候选草稿仍必须被拦（第八步 test_plain_empty_draft_is_still_refused）
        self.mk_draft("blank", cands="[]")
        blank_fm = self.draft_fm("blank")
        self.assertFalse(wstore.is_delete_fm(blank_fm))
        self.assertIn("candidates", wstore.missing_fm(blank_fm))
        # 这份空页草稿只是用来验判据的，验完收走，否则 commit 会（正确地）拒收
        (wstore.root() / ".drafts" / "blank.md").unlink()

        ok, out = wstore.commit()
        self.assertTrue(ok, out)
        self.assertIn(VICTIM, [p.split("/")[-1][:-3] for p in out["deleted"]])
        self.assertIsInstance(out["dangling"], dict)


# ================================================================ 守恒守卫（守卫自己会响）
class RetireGuardTest(RetireBase):
    def test_guard_rings_when_a_candidate_is_both_moved_and_retired(self):
        """③ 一条候选既留又废 —— 两边都以为自己处理了。"""
        self.propose()
        ok, res = wa.apply_retire_action(VICTIM, self.c1.id, "move", target=KEEPER)
        self.assertTrue(ok, res)
        self.set_retire(self.ids)            # 手工把 c1 塞回废弃清单
        why = wstore.check_retire_conservation()
        self.assertIn("冲突", why)
        self.assertIn(str(self.c1.id), why)

    def test_guard_rings_when_a_candidate_vanishes(self):
        """④ 一条候选凭空消失 —— 静默丢知识。"""
        self.propose()
        self.set_retire([self.c1.id, self.c2.id])
        why = wstore.check_retire_conservation()
        self.assertIn("漏了去向", why)
        self.assertIn(str(self.c3.id), why)

    def test_guard_rings_on_ghost_candidate(self):
        """⑤ 搬走了一条已 discarded 的候选。"""
        self.propose()
        ok, res = wa.apply_retire_action(VICTIM, self.c1.id, "move", target=KEEPER)
        self.assertTrue(ok, res)
        IngestCandidate.objects.filter(id=self.c1.id).update(status="discarded")
        why = wstore.check_retire_conservation(live_ids=set(wa.live_candidates()))
        self.assertIn("幽灵候选", why)
        self.assertIn(str(self.c1.id), why)

    def test_guard_rings_when_retire_not_subset_of_source(self):
        """⑥ 废弃了不属于这页的 id：守卫与 `_validate_draft` 都要点名。"""
        self.propose()
        self.set_retire(self.ids + [999999])
        why = wstore.check_retire_conservation()
        self.assertIn("越界", why)
        self.assertIn("999999", why)
        ok, err = wstore._validate_draft(VICTIM)
        self.assertFalse(ok)
        self.assertIn("不属于这一页", str(err))

    def test_target_page_original_candidates_do_not_break_conservation(self):
        """★ `moved` 必须取「∩ 原页清单」：目标页原有候选不属于被删页。

        把守卫改回「搬迁稿 candidates 全并集」，这一条必红 —— 守卫变成假红
        比漏一条更贵（第九步「全量可见 ≠ 全量自动」的同形教训）。
        """
        self.propose()
        ok, res = wa.apply_retire_action(VICTIM, self.c1.id, "move", target=KEEPER)
        self.assertTrue(ok, res)
        self.assertIn(self.c4.id, wstore._cand_ids(self.draft_fm(KEEPER)),
                      "替换稿要保留目标页原有候选")
        self.assertEqual(wstore.check_retire_conservation(), "")

    def test_commit_conservation_guard_itself_rings_and_writes_nothing(self):
        """④b commit 不是只看单份合法：跨稿守恒破了要整批拒、一个字节不动。"""
        self.propose()
        self.set_retire([self.c1.id, self.c2.id])
        before = self.snapshot()
        ok, err = wstore.commit()
        self.assertFalse(ok)
        self.assertIn("漏了去向", str(err))
        self.assertEqual(self.snapshot(), before)

    def test_rejected_action_writes_nothing(self):
        """写盘前预演：守恒或入口判据不过，删除稿与搬迁稿一个字节都不动。"""
        self.propose()
        self.set_retire([self.c1.id, self.c2.id])   # 故意先弄坏守恒
        before = self.snapshot()
        ok, err = wa.apply_retire_action(VICTIM, self.c1.id, "move", target=KEEPER)
        self.assertFalse(ok)
        self.assertIn("漏了去向", str(err))
        self.assertEqual(self.snapshot(), before)


# ================================================================ 动作 / 落地
class RetireActionTest(RetireBase):
    def test_moved_candidate_lands_in_target_frontmatter(self):
        """⑧ 迁走真的落地（不是只改了 UI），废弃真的是终态。"""
        self.propose()
        ok, res = wa.apply_retire_action(VICTIM, self.c1.id, "move", target=KEEPER)
        self.assertTrue(ok, res)
        self.assertIn(f"{KEEPER}.md", self.draft_files())
        self.assertEqual(wstore._retire_ids(self.draft_fm(VICTIM)),
                         [self.c2.id, self.c3.id])
        self.assertIn(self.c1.id, wstore._cand_ids(self.draft_fm(KEEPER)))
        self.assertEqual(self.draft_fm(KEEPER).get("retire_from"), VICTIM)
        self.assertEqual(self.draft_fm(KEEPER).get("edits"), KEEPER,
                         "目标为已入库页必须先按 pull_page 形状打 edits")

        ok, payload = wstore.commit()
        self.assertTrue(ok, payload)
        self.assertFalse((wstore.root() / "wiki" / "invest" / f"{VICTIM}.md").exists())
        keeper_fm = wstore.parse_front(self.page_text(KEEPER))[0]
        self.assertIn(self.c1.id, wstore._cand_ids(keeper_fm))
        self.assertIn(self.c4.id, wstore._cand_ids(keeper_fm), "原有候选不能丢")
        self.assertNotIn("retire_from", keeper_fm, "意图标记入库要剥掉")
        self.c1.refresh_from_db()
        self.assertNotEqual(self.c1.status, "discarded", "留下的候选还活着")
        for c in (self.c2, self.c3):
            c.refresh_from_db()
            self.assertEqual(c.status, "discarded")
        self.assertEqual(payload["retire"]["moved_n"], 1)
        self.assertEqual(payload["retire"]["retired_n"], 2)

    def test_new_page_action_makes_a_draft_and_strips_marker_on_commit(self):
        """③ 留 → 新建页：程序只生成 frontmatter + **空正文**的半成品草稿（零 LLM）。

        形态 A 下正文 = 纯 polish 一段；`_validate_draft` 新增「正文为空 + 挂候选 →
        拒入库」闸门。所以先用 `set_polish` 写回结论（程序写、零 LLM，等价于审批里
        重编成功）再提交，`retire_from` 标记照旧在入库时剥掉。
        """
        self.propose()
        ok, res = wa.apply_retire_action(
            VICTIM, self.c1.id, "move", target=wa.RETIRE_NEW,
            title="留存判断", new_slug="kept-note")
        self.assertTrue(ok, res)
        self.assertIn("kept-note.md", self.draft_files())
        fm = self.draft_fm("kept-note")
        self.assertEqual(wstore._cand_ids(fm), [self.c1.id])
        self.assertEqual(fm.get("retire_from"), VICTIM)
        self.assertEqual(wstore.missing_fm(fm), [], "新页稿的 frontmatter 要完整")

        # 形态 A：新建页草稿正文留空（结论待重编），不能当成品直接入库
        _, empty_body = wstore.parse_front(self.draft_text("kept-note"))
        self.assertFalse(empty_body.strip(), "新建页正文留空，结论由 polish 写回")
        # ★★ 搬迁到新建页 / 已有页两支必须**同口径**打 stale：`_affected_slugs()`
        #   只挑 `body_stale` 的页重编，新建页漏打就永远不会被自动重编 —— 读侧只剩
        #   「结论还没生成」，却没人知道它还需要重编（静默失败最贵）。
        #   2026-11-09 实测缺口：只给 else 分支打标记，这支一直不带 stale。
        self.assertEqual(str(fm.get(wstore.STALE_FM) or "").strip().lower(), "true",
                         "新建页搬迁稿必须带 stale，否则重编链路挑不到它")
        # ★ 新守卫反证：正文为空 + 有候选，入库闸门必须当场拒（不是恒真断言）
        refused, why = wstore.commit()
        self.assertFalse(refused, "空正文的半成品不许入库")
        self.assertIn("结论还没生成", why)

        # 用 set_polish 写回纯 polish 结论（零 LLM；素材按 frontmatter candidates 现算）
        info = wa.read("draft", "kept-note")
        material = wstore.candidate_list_text(
            info["candidates"],
            {c["cand_id"]: c for c in info["candidate_detail"]})
        ok, why = wstore.set_polish("kept-note", "这是一段结论。", material)
        self.assertTrue(ok, why)
        fm2, body = wstore.parse_front(self.draft_text("kept-note"))
        self.assertEqual(body.strip(), "这是一段结论。", "正文只有 polish 一段")
        self.assertNotIn(wstore.STALE_FM, fm2, "重编成功必须剥 stale（警告消失＝真重编过）")
        for head in ("## 候选清单", "## 通顺话", "## 我的判断",
                     "## 事实 / 他人的观点", "## 我的在意点"):
            self.assertNotIn(head, body, "形态 A 正文不许再有任何结构标题")

        ok, payload = wstore.commit()
        self.assertTrue(ok, payload)
        page_fm = wstore.parse_front(self.page_text("kept-note"))[0]
        self.assertNotIn("retire_from", page_fm)
        self.assertEqual(wstore._cand_ids(page_fm), [self.c1.id])

    def test_retire_action_undoes_a_move_and_archives_empty_draft(self):
        """撤回到空稿要归档，不留一份「覆盖别人页却什么都没改」的无用稿。"""
        self.propose()
        ok, _ = wa.apply_retire_action(
            VICTIM, self.c1.id, "move", target=wa.RETIRE_NEW, new_slug="kept")
        self.assertTrue(ok)
        ok, res = wa.apply_retire_action(VICTIM, self.c1.id, "retire")
        self.assertTrue(ok, res)
        self.assertNotIn("kept.md", self.draft_files())
        self.assertEqual(wstore._retire_ids(self.draft_fm(VICTIM)), self.ids)
        self.assertTrue(list((wstore.root() / ".drafts" / ".discarded").glob("kept-*.md")))

    def test_existing_target_draft_is_refused_not_overwritten(self):
        """⑦ 一页一份替换稿：目标已有待审草稿时显式说「先处理」，不悄悄覆盖。"""
        self.propose()
        self.mk_draft(KEEPER, cands=f"[{self.c4.id}]")
        before = self.snapshot()
        ok, err = wa.apply_retire_action(VICTIM, self.c1.id, "move", target=KEEPER)
        self.assertFalse(ok)
        self.assertIn("先处理", str(err))
        self.assertIn(f".drafts/{KEEPER}.md", str(err))
        self.assertEqual(self.snapshot(), before)

    def test_three_rejections_are_worded_differently(self):
        """★ 三种拒绝的修法不同，话要分开说（合成「无法处理」= 推回脏活）。"""
        self.propose()
        self.mk_page("other-domain", domain="tech", cands=(self.c4.id + 1000,))
        cases = [
            ("move", "../escape", "非法"),
            ("move", "other-domain", "不跨域"),
            ("move", "no-such-page", "库里没有这一页"),
        ]
        for action, target, word in cases:
            ok, err = wa.apply_retire_action(VICTIM, self.c1.id, action,
                                             target=target)
            self.assertFalse(ok, (target, err))
            self.assertIn(word, str(err), target)
        self.assertEqual(wstore._retire_ids(self.draft_fm(VICTIM)), self.ids,
                         "三次拒绝都不该改删除稿")

    def test_locked_fm_blocks_agent_from_writing_retire(self):
        """`retire` / `retire_from` 必须进 LOCKED_FM：agent 不能私自改去向。"""
        self.propose()
        ok, err = wa.set("frontmatter", {
            "slug": VICTIM, "fields": {"retire": [self.c1.id]}})
        self.assertFalse(ok)
        self.assertIn("retire", str(err))
        self.assertEqual(wstore._retire_ids(self.draft_fm(VICTIM)), self.ids)

    @patch("core.wiki_loop.run", side_effect=AssertionError("删页提案不该调 LLM"))
    def test_no_llm_call_in_relocation(self, stub):
        """⑬ 零花钱：**本步没有花钱端点**，要写明是「没有出口」而不是漏打桩。"""
        self.propose()
        ok, res = wa.apply_retire_action(VICTIM, self.c1.id, "move", target=KEEPER)
        self.assertTrue(ok, res)
        stub.assert_not_called()
        # ★ 桩自己会响的反证：真调一次必须抛。否则「没被调用」也可能是桩没生效。
        with self.assertRaises(AssertionError):
            wiki_loop.run("probe")


# ================================================================ 权威清单 / 旧格式
class RetireTruthTest(RetireBase):
    def test_authoritative_list_is_read_from_original_page(self):
        """⑤ 清单来自被删页 frontmatter，不从清空后的删除稿读。"""
        self.propose()
        self.assertEqual(self.draft_fm(VICTIM).get("candidates"), [],
                         "删除稿的 candidates 已被清空，认它就得到空清单")
        p = wa.retire_proposal(VICTIM)
        self.assertTrue(p["ok"])
        self.assertEqual(p["source_ids"], self.ids)
        self.assertEqual([x["id"] for x in p["items"]], self.ids)
        self.assertEqual(p["retired_n"], 3)
        self.assertEqual(p["moved_n"], 0)
        self.assertFalse(p["old_format"])
        self.assertFalse(p["guard_rings"])

        # 反证：原页清单改了，守卫立刻响（说明它读的真是原页）
        pg = wstore.root() / "wiki" / "invest" / f"{VICTIM}.md"
        pg.write_text(wstore._set_fm_line(
            self.page_text(VICTIM), "candidates", f"[{self.c1.id}]"),
            encoding="utf-8")
        self.assertIn("越界", wstore.check_retire_conservation())

    def test_old_format_delete_draft_falls_back_visibly(self):
        """⑥ 旧稿回退**要说出来**：日志、commit 返回值、面板都要点名。"""
        self.mk_draft(VICTIM, cands="[]", extra=f"edits: {VICTIM}\n")
        ok, info = wstore._validate_draft(VICTIM)
        self.assertTrue(ok, info)
        self.assertTrue(info["retire_legacy"])
        self.assertIn("旧格式", info["retire_note"])
        self.assertEqual(info["removed"], self.ids, "回退到原页全量废弃")

        ok, payload = wstore.commit()
        self.assertTrue(ok, payload)
        self.assertTrue(payload["retire"]["notes"], "commit 返回值要点名")
        self.assertIn("旧格式", payload["retire"]["notes"][0])

    def test_old_format_proposal_refuses_action_instead_of_silent_fallback(self):
        """旧稿上点「留」必须拒，不能静默兜底成「看起来生效了」。"""
        self.mk_draft(VICTIM, cands="[]", extra=f"edits: {VICTIM}\n")
        p = wa.retire_proposal(VICTIM)
        self.assertTrue(p["old_format"])
        ok, err = wa.apply_retire_action(VICTIM, self.c1.id, "move", target=KEEPER)
        self.assertFalse(ok)
        self.assertIn("旧格式", str(err))

    def test_commit_is_atomic_when_one_relocation_draft_fails(self):
        """⑪ 一份搬迁稿不合法 → 整批不动（单份预检归预检，守恒归守恒）。"""
        self.propose()
        ok, res = wa.apply_retire_action(VICTIM, self.c1.id, "move", target=KEEPER)
        self.assertTrue(ok, res)
        # 破坏搬迁稿：抽掉 domain 行
        p = wstore.root() / ".drafts" / f"{KEEPER}.md"
        p.write_text(self.draft_text(KEEPER).replace("domain: invest\n", ""),
                     encoding="utf-8")
        before = self.snapshot()
        ok, err = wstore.commit()
        self.assertFalse(ok)
        self.assertIn("校验未通过", str(err))
        self.assertEqual(self.snapshot(), before)


# ================================================================ 面板 / 可见性
class RetirePanelTest(RetireBase):
    def test_proposal_panel_lists_every_candidate_with_visible_text(self):
        """★ 断言只看可见文字：逐条候选、默认全废、动作按钮都要真渲染。"""
        self.propose()
        html = self.client.get("/wiki/approval").content.decode()
        for word in ("删页提案 · 逐条定候选去向", "默认全废", "已留 0 条",
                     "护城河来自规模", "成本曲线在下降", "渠道优势被削弱",
                     "规模带来议价能力", "留 → 去这页", "新建页",
                     "库里没有相近的页，建议新建一页"):
            self.assertIn(word, html, f"面板上缺可见文字：{word}")
        for junk in ("{%", "{#", "endcomment"):
            self.assertNotIn(junk, html, "多行注释残留只有真渲染看得见")

    def test_move_action_via_post_updates_retire_and_makes_draft(self):
        """点一下就真发 POST、真落搬迁稿、真改删除稿的 retire。"""
        self.propose()
        resp = self.client.post("/wiki/retire/apply", {
            "slug": VICTIM, "cand_id": self.c1.id, "action": "move",
            "target": KEEPER})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(f"{KEEPER}.md", self.draft_files())
        self.assertEqual(wstore._retire_ids(self.draft_fm(VICTIM)),
                         [self.c2.id, self.c3.id])
        html = resp.content.decode()
        self.assertIn("已留 →", html, "工作区要立刻显示新去向")
        self.assertIn("approval-list", html, "列表要用 oob 一起刷新")

    def test_failed_action_shows_reason_not_silent(self):
        """失败原因留在面板上，不是只发一句 toast。"""
        self.propose()
        self.mk_draft(KEEPER, cands=f"[{self.c4.id}]")
        resp = self.client.post("/wiki/retire/apply", {
            "slug": VICTIM, "cand_id": self.c1.id, "action": "move",
            "target": KEEPER})
        html = resp.content.decode()
        self.assertIn("未生效", html)
        self.assertIn("先处理", html)

    def test_old_format_fallback_visible_in_commit_flash(self):
        """⑥ 旧稿回退在 toast 里也要说出来（加兜底前先问出错时谁会知道）。"""
        self.mk_draft(VICTIM, cands="[]", extra=f"edits: {VICTIM}\n")
        resp = self.client.post("/wiki/approval/commit")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data["ok"], data)
        self.assertIn("旧格式", data["flash"])

    def test_commit_flash_reports_moved_and_retired_separately(self):
        """「M 条已搬去 X、N 条已废弃」要单独说，不能并进「已入库」。"""
        self.propose()
        ok, res = wa.apply_retire_action(VICTIM, self.c1.id, "move", target=KEEPER)
        self.assertTrue(ok, res)
        resp = self.client.post("/wiki/approval/commit")
        data = json.loads(resp.content)
        self.assertTrue(data["ok"], data)
        self.assertIn("1 条候选已搬去别的页", data["flash"])
        self.assertIn("2 条已废弃", data["flash"])
