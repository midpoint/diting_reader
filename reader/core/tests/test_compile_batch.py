"""S1 分组拆批（2026-11-03）：21 条一次判不动 —— 推理吃光预算、正文 0 字。

起因（真实数据）：21 条候选 + 20 页目录一次判，输入 4322 tokens，模型把 4096、再
翻倍到 8192 的预算**全部用在推理上**，`finish_reason=length`、正文 0 字 —— 连续
两次失败，¥0.057 一条产物都没有。推理量随「要同时比较的候选数」涨得比候选本身快。

这一组盯三件事，每一件都是**拆批自己会引入的新退化**（不是重复测旧逻辑）：
  1. 真的拆了（每批 ≤ 8 条、调用次数 = 批数），且小批量不被无谓拆开；
  2. 同一来源尽量留在同一批（否则批次之间互相看不见，同一概念被各建一页）；
  3. 一批失败不让整轮归零，且**说清哪几条没编**（静默少编译是本环节最贵的错）。

★ 桩纪律：patch 的是 `core.citeguard._call`（ingest 通过模块属性调用），桩自己
  数调用次数 —— 没被用上就等于这次真联网花了钱。
★ 断言要看**喂进去的 payload**，不能只看调用次数：换了 label 却忘了换材料，
  次数照样对，正是「假绿」最典型的形状（本项目为此返工过两次）。
★ 形态 A：S2 不再调模型，编译收尾的结论重编走 `core.wiki_loop._call`。集成用例
  要在被调用点补这段桩（返回固定结论），并断言草稿正文真的出现该结论 —— 否则
  `json.loads("")` 的 S1 桩会被 polish 的空 user 打崩，或真联网花钱。
"""
import json
from contextlib import contextmanager
from unittest import mock
from unittest.mock import patch

from django.test import TestCase

from core import citeguard, costreport as cr, ingest
from core import wiki as wstore
from core.tests.test_compile_merge import MergeBase
from core.tests.test_ingest_compile import CompileBase, mk_cand


def one_group(ids, title=None, domain="invest", target=None):
    g = {"title": title or ("组-%s" % ids[0]), "domain": domain,
         "cand_ids": list(ids), "reason": "同一概念"}
    if target:
        g["target"] = target
    return json.dumps({"groups": [g]}, ensure_ascii=False)


@contextmanager
def patch_s1(plan):
    """S1 桩：`plan(第几批, 本批 cand_id)` 返回回复文本，或 `(text, err)` 表示失败。

    每次调用都记下喂进去的 payload（cand_id / source / label / max_calls），
    用例据此断言「真的按批切了」，而不是只看调用次数。
    """
    seen, state = [], {"bi": 0}

    def _fake(kind, system, user, stats, **kw):
        assert kw.get("tag") == "wiki 编译分组", "S2 不该在这组用例里被调用：%r" % kw.get("tag")
        state["bi"] += 1
        payload = json.loads(user)
        seen.append({
            "ids": [c["cand_id"] for c in payload["candidates"]],
            "sources": [c["source"] for c in payload["candidates"]],
            "pages": len(payload["pages"]),
            "pending": payload.get("pending_groups") or [],
            "label": kw.get("label") or "",
            "max_calls": kw.get("max_calls"),
            # ★ 2026-11-09：多条批不许再走「token 翻倍重试」（撞预算改为拆批），
            #   单条批拆不动才留着重试 —— 这个开关必须能被断言到。
            "retry_on_empty": kw.get("retry_on_empty", True),
        })
        r = plan(state["bi"], seen[-1]["ids"])
        return r if isinstance(r, tuple) else (r, "")

    with patch("core.citeguard._call", side_effect=_fake) as m:
        yield m, seen


class S1BatchSplitTest(TestCase):
    """拆批的形状：几批、每批几条、批里都是谁。"""

    def test_twenty_one_cands_are_split_into_three_batches(self):
        """批大小 8（2026-11-09 用户拍板调回：16 条/批实测频繁撞 4096 顶）
        → 21 条拆 8+8+5 三批。"""
        cs = [mk_cand("判断%d" % i, "判断%d" % i, sid="daily-2026-10-%02d" % (i + 1))
              for i in range(21)]
        with patch_s1(lambda bi, ids: one_group(ids)) as (call, seen):
            groups, err, _notes = ingest.plan_groups(cs)
        self.assertEqual(err, "")
        self.assertEqual(call.call_count, 3, "21 条 / 每批 8 条 = 3 批")
        self.assertEqual([len(s["ids"]) for s in seen], [8, 8, 5])
        for s in seen:
            self.assertLessEqual(len(s["ids"]), ingest.COMPILE_S1_BATCH)
            # ★ max_calls 必须跟着批数走：写死 1 的话第二批一进门就被「达到单次
            #   调用上限」拒掉，而前一批已经花了钱 —— 症状是永远只编出第一批。
            #   ★ 2026-11-09：撞预算会**拆批**，批队列会变长（每拆一次多一批），
            #     所以上界改成「当前队列长度 + 余量」，断言只要求 ≥ 批数。
            self.assertGreaterEqual(s["max_calls"], 2,
                                    "max_calls 必须容得下后面的批，否则直接被上限拒")
            self.assertIn("第", s["label"], "label 要能看出是第几批（日志排查用）")
            # ★ 多条批不许翻倍重试（翻倍在分组这一步是白花钱，见 plan_groups docstring）
            self.assertFalse(s["retry_on_empty"],
                             "多条批应走拆批，不该让 _call 自己把 token 翻倍重打一遍")
        covered = sorted(i for s in seen for i in s["ids"])
        self.assertEqual(covered, sorted(c.id for c in cs), "每条候选都要进且只进一批")
        self.assertEqual(len(groups), 3)

    def test_batch_size_lowered_per_user(self):
        """守卫批大小本身：2026-11-04 曾调到 16，2026-11-09 用户据实测拍板调回 8
        （16 条/批 5 次调用 3 次撞 4096 顶 + 高漏分）——谁再调大到 8 以上，这里要响。"""
        self.assertLessEqual(ingest.COMPILE_S1_BATCH, 8)

    def test_small_batch_is_not_split(self):
        cs = [mk_cand("判断%d" % i, "判断%d" % i, sid="daily-a") for i in range(5)]
        with patch_s1(lambda bi, ids: one_group(ids)) as (call, seen):
            ingest.plan_groups(cs)
        self.assertEqual(call.call_count, 1, "≤8 条就是一批，不许无谓拆开（那是多花钱）")
        self.assertEqual(len(seen[0]["ids"]), 5)

    def test_same_source_is_not_split_across_batches(self):
        """两个来源各 5 条、批大小 7：必须 5+5，不许为了凑满 7 条把来源切开。"""
        a = [mk_cand("A%d" % i, "A%d" % i, sid="daily-a") for i in range(5)]
        b = [mk_cand("B%d" % i, "B%d" % i, sid="daily-b") for i in range(5)]
        with mock.patch.object(ingest, "COMPILE_S1_BATCH", 7):
            with patch_s1(lambda bi, ids: one_group(ids)) as (call, seen):
                ingest.plan_groups(a + b)
        self.assertEqual([len(s["ids"]) for s in seen], [5, 5],
                         "同源候选被切到两批 → 两批互相看不见 → 同一个概念两页")
        for s in seen:
            self.assertEqual(len(set(s["sources"])), 1, "同一批里不许混两个来源")

    def test_single_source_longer_than_batch_is_cut(self):
        """单个来源自己就超批大小：只能切散（硬留一批等于没拆）。"""
        cs = [mk_cand("判断%d" % i, "判断%d" % i, sid="daily-big") for i in range(10)]
        with mock.patch.object(ingest, "COMPILE_S1_BATCH", 4):
            with patch_s1(lambda bi, ids: one_group(ids)) as (call, seen):
                ingest.plan_groups(cs)
        self.assertEqual([len(s["ids"]) for s in seen], [4, 4, 2])


class S1PartialFailureTest(TestCase):
    """一批判不动时：别让整轮归零，也别静默少编。

    ★ 这组钉 `COMPILE_S1_BATCH=8`（不是默认 16）：为的是保住「3 批」的形状，
      好断言「失败的是第 2 批、其余照常」。形状判据与批大小无关，钉小值
      让用例读得懂；默认值本身由 `test_batch_size_was_raised_per_user` 守。
    """

    def _cands(self, n=21):
        return [mk_cand("判断%d" % i, "判断%d" % i, sid="daily-2026-10-%02d" % (i + 1))
                for i in range(n)]

    def test_one_batch_failure_keeps_the_other_batches(self):
        cs = self._cands()
        missed = [c.id for c in cs][8:16]          # 第 2 批那 8 条

        def plan(bi, ids):
            if bi == 2:
                # ★ 用**非撞预算**的失败（2026-11-09）：撞预算现在会触发「对半拆批
                #   重跑」，那条路由 CapOutSplitTest 专门盯。「一批失败不归零」这条
                #   判据要的是「模型没给结果」这种真失败。
                return ("", "wiki 编译分组 · 第 2/3 批：模型没有返回分组结果")
            return one_group(ids)

        with mock.patch.object(ingest, "COMPILE_S1_BATCH", 8):
            with patch_s1(plan) as (call, _seen):
                groups, err, notes = ingest.plan_groups(cs)
        self.assertEqual(err, "", "部分失败不该让整轮归零（那正是这次要修的体验）")
        self.assertEqual(call.call_count, 3, "一批失败后仍要继续跑后面的批")
        got = sorted(i for g in groups for i in g["cand_ids"])
        self.assertEqual(got, sorted(set(c.id for c in cs) - set(missed)))
        joined = "；".join(notes)
        self.assertIn("没编进去", joined)
        self.assertIn("第 2/3 批", joined)
        for i in missed:
            self.assertIn(str(i), joined, "必须点名哪几条漏了，否则人不知道下次还缺什么")

    def test_all_batches_failing_names_each_batch(self):
        cs = self._cands()
        with mock.patch.object(ingest, "COMPILE_S1_BATCH", 8):
            with patch_s1(lambda bi, ids: ("", "预算被推理吃光")) as (call, _seen):
                groups, err, _notes = ingest.plan_groups(cs)
        self.assertEqual(groups, [])
        self.assertEqual(call.call_count, 3, "全失败也要把每一批都试过（否则永远只报第一批）")
        for tag in ("第 1/3 批", "第 2/3 批", "第 3/3 批"):
            self.assertIn(tag, err, "每批的原因都要在错误里，否则只修第一批也没用")

    def test_one_batch_returning_garbage_counts_as_that_batch_failing(self):
        cs = self._cands()

        def plan(bi, ids):
            # 「输出不是 JSON」与「模型说没有组」是两回事，不能混成一句
            return "我觉得这些应该分成三组" if bi == 1 else one_group(ids)

        with mock.patch.object(ingest, "COMPILE_S1_BATCH", 8):
            with patch_s1(plan) as (_call, _seen):
                groups, err, notes = ingest.plan_groups(cs)
        self.assertEqual(err, "")
        self.assertEqual(len(groups), 2)
        joined = "；".join(notes)
        self.assertIn("不是约定的 JSON", joined)
        self.assertIn("第 1/3 批", joined)


class CrossBatchMergeTest(MergeBase):
    """两批互相看不见时，最容易出的错：同一页被写两份。

    ★ 批内去重的既有语义（同批两组装同一页 → 第二组退回新建）**不改**，
      这里只管跨批：两批各自合理地判了同一页，必须合成一组。
    """

    def _plan(self, plan):
        return patch_s1(plan)

    def _run(self, plan, cands):
        with mock.patch.object(ingest, "COMPILE_S1_BATCH", 1):
            with self._plan(plan) as (_call, seen):
                groups, err, notes = ingest.plan_groups(cands, wstore.pages())
        self.assertEqual(err, "")
        return groups, notes, seen

    def test_two_batches_merging_same_page_become_one_group(self):
        a = mk_cand("护城河在验证器", "护城河在验证器", sid="daily-a")
        b = mk_cand("验证器决定数据差异化", "验证器决定数据", sid="daily-b")

        def plan(bi, ids):
            return one_group(ids, title="护城河组%d" % bi, target="existing:llm-moat")

        groups, notes, seen = self._run(plan, [a, b])
        self.assertEqual(len(seen), 2, "前提：确实是两批（否则这条用例没测到跨批）")
        self.assertEqual(len(groups), 1, "两批都判并入同一页 → 必须合成一组")
        self.assertEqual(sorted(groups[0]["cand_ids"]), sorted([a.id, b.id]),
                         "两批的候选都要在这一组里，一条都不能丢")
        self.assertTrue(any("合成一组" in n for n in notes), "合并这件事要说出来")

    def test_two_batches_new_with_same_title_become_one_group(self):
        a = mk_cand("护城河在验证器", "护城河在验证器", sid="daily-a")
        b = mk_cand("护城河在验证器（补充）", "护城河在验证器", sid="daily-b")

        def plan(bi, ids):
            return one_group(ids, title="护城河在验证器")

        groups, notes, _seen = self._run(plan, [a, b])
        self.assertEqual(len(groups), 1, "同域同名的新建组 → 一页，不是「xxx」和「xxx-2」")
        self.assertEqual(sorted(groups[0]["cand_ids"]), sorted([a.id, b.id]))
        self.assertTrue(any("同名" in n for n in notes))

    def test_different_titles_stay_separate(self):
        """反证：跨批合并不能变成「见谁都合并」（那会把两个概念揉成一页）。"""
        a = mk_cand("护城河在验证器", "护城河在验证器", sid="daily-a")
        b = mk_cand("投资要看十年后现金流", "看十年后现金流", sid="daily-b")

        def plan(bi, ids):
            return one_group(ids, title="组%d" % bi)

        groups, _notes, _seen = self._run(plan, [a, b])
        self.assertEqual(len(groups), 2, "同名才合并；不同名两个概念不许揉成一页")

    def test_same_title_different_domain_stays_separate(self):
        """同名但不同域：两页（domain 不同本来就不能进一页）。"""
        a = mk_cand("护城河在验证器", "护城河在验证器", sid="daily-a", domain="invest")
        b = mk_cand("护城河在验证器", "护城河在验证器", sid="daily-b", domain="ai")

        def plan(bi, ids):
            return one_group(ids, title="护城河在验证器",
                             domain="invest" if bi == 1 else "ai")

        groups, _notes, _seen = self._run(plan, [a, b])
        self.assertEqual(len(groups), 2, "跨域不许合并")


class PriorGroupsJoinTest(TestCase):
    """分批不再各判各的（2026-11-04，用户：「可能不同批之间是可以互相合并的，
    一次审批就好了」）。

    ★ 机制：后面批次的 payload 里带 `pending_groups`（前面批次已定下的组，
      gid 从 1 起）；模型用 `"join": <gid>` 把本批候选并进那一组，程序侧只做
      合法性校验（gid 存在 / domain 一致），不合法降级为常规分组并**说出来**。
    ★ 与既有两条跨批合并（同 target / 同名新建）的关系：那两条是兜底（模型
      自己撞上才触发），join 是正路（模型看得见前面的组，不必猜同名）。
    """

    def _two_sources(self):
        return [mk_cand("护城河在验证器", "护城河在验证器", sid="daily-a"),
                mk_cand("验证器决定数据差异化", "验证器决定数据", sid="daily-b")]

    def test_second_batch_sees_prior_groups_and_joins(self):
        a, b = self._two_sources()

        def plan(bi, ids):
            if bi == 1:
                return one_group(ids, title="护城河在验证器")
            return json.dumps({"groups": [{"title": "护城河在验证器", "domain": "invest",
                                           "cand_ids": ids, "join": 1}]},
                              ensure_ascii=False)

        with mock.patch.object(ingest, "COMPILE_S1_BATCH", 1):
            with patch_s1(plan) as (_call, seen):
                groups, err, notes = ingest.plan_groups([a, b])
        self.assertEqual(err, "")
        # 前提：第二批的 payload 里真的带上了前面批次的组（含 claims）
        self.assertEqual(seen[1]["pending"][0]["gid"], 1)
        self.assertEqual(seen[1]["pending"][0]["title"], "护城河在验证器")
        self.assertTrue(seen[1]["pending"][0]["claims"], "不带 claims，模型没法判断是不是同一概念")
        self.assertEqual(len(groups), 1, "join 进前面批次 → 一组，不是两页")
        self.assertEqual(sorted(groups[0]["cand_ids"]), sorted([a.id, b.id]),
                         "两批的候选都在这一组里")
        self.assertTrue(any("前面批次" in n for n in notes), "合并要说出来")

    def test_join_with_unknown_gid_falls_back_and_says_so(self):
        def plan(bi, ids):
            if bi == 1:
                return one_group(ids, title="护城河在验证器")
            return json.dumps({"groups": [{"title": "另一组", "domain": "invest",
                                           "cand_ids": ids, "join": 99}]},
                              ensure_ascii=False)

        with mock.patch.object(ingest, "COMPILE_S1_BATCH", 1):
            with patch_s1(plan) as (_call, _seen):
                groups, err, notes = ingest.plan_groups(self._two_sources())
        self.assertEqual(err, "")
        self.assertEqual(len(groups), 2, "gid 不存在 → 降级常规分组，不许静默丢")
        self.assertTrue(any("没有这个组" in n for n in notes))

    def test_join_across_domains_falls_back_and_says_so(self):
        a, b = self._two_sources()

        def plan(bi, ids):
            if bi == 1:
                return one_group(ids, title="护城河在验证器")       # invest
            return json.dumps({"groups": [{"title": "另一组", "domain": "ai",
                                           "cand_ids": ids, "join": 1}]},
                              ensure_ascii=False)

        with mock.patch.object(ingest, "COMPILE_S1_BATCH", 1):
            with patch_s1(plan) as (_call, _seen):
                groups, err, notes = ingest.plan_groups([a, b])
        self.assertEqual(err, "")
        self.assertEqual(len(groups), 2, "跨域 join 一律不许")
        self.assertTrue(any("域" in n for n in notes))


class CompileNotesTest(CompileBase):
    """集成：分组说「哪批失败」还不够，得真的走到编译结果里。

    ★ 这是「静默失败最贵」的具体形状：`plan_groups` 返回了 notes，而 `_compile_locked`
      里新开一个 `notes = []` 把它盖掉 —— 每一条单元测试都绿，人却只看到
      「生成了 2 页」，永远不知道还有 8 条没编。
    """

    def test_failed_batch_is_visible_in_compile_result(self):
        cs = [mk_cand("判断%d" % i, "判断%d" % i, sid="daily-2026-10-%02d" % (i + 1))
              for i in range(21)]
        group_ids = {}          # S1 判的组 → S2 要用它生成 lines
        state = {"bi": 0}

        def fake(kind, system, user, stats, **kw):
            tag = kw.get("tag") or ""
            payload = json.loads(user)
            if tag == "wiki 编译分组":
                state["bi"] += 1
                ids = [c["cand_id"] for c in payload["candidates"]]
                if state["bi"] == 2:
                    # ★ 非撞预算的失败（撞预算会走「拆批重跑」，见 CapOutSplitTest）
                    return "", "模型没有返回分组结果"
                title = "组%d" % state["bi"]
                group_ids[title] = ids
                return json.dumps({"groups": [{"title": title, "domain": "invest",
                                               "cand_ids": ids}]}, ensure_ascii=False), ""
            # S2：按组标题取回本组的候选，逐条写成「我的判断」
            ids = group_ids[kw["label"]]
            lines = [{"cand_id": i, "section": "judge", "type": "judgment",
                      "text": "判断%d" % i} for i in ids]
            slug = "grp-%d" % ids[0]
            return json.dumps({"slug": slug, "title": kw["label"], "lines": lines,
                               "facts": [], "hint": ""}, ensure_ascii=False), ""

        # 钉 8 是为了保住「3 批、第 2 批失败」的形状（见 S1PartialFailureTest 的说明）
        # ★ 形态 A：编译收尾会对每份草稿调 `wiki_loop._call` 重编结论。桩打在被调用点
        #   （不再走 citeguard 那份 `json.loads(user)` 的 S1 桩），且配「桩真被用上」反证。
        with mock.patch.object(ingest, "COMPILE_S1_BATCH", 8):
            with patch("core.citeguard._call", side_effect=fake), \
                 patch("core.wiki_loop._call",
                       return_value=("这是一段结论。", [], None)) as polish:
                res = ingest.compile_candidates()
        self.assertTrue(res["ok"], res.get("err"))
        self.assertEqual(len(res["drafts"]), 2, "第 1、3 批照常出页")
        # ★ 桩真被用上：两页各重编一次，且结论真的写进了草稿正文（不是空正文假绿）
        self.assertEqual(polish.call_count, 2, "两页草稿各调一次结论重编")
        self.assertEqual(sorted(res["polished"]), sorted(res["drafts"]),
                         "两页都要 polish 成功写回（stale 才会被剥）")
        for slug in res["drafts"]:
            text = (wstore.root() / ".drafts" / f"{slug}.md").read_text(encoding="utf-8")
            fm, body = wstore.parse_front(text)
            self.assertIn("这是一段结论。", body, "polish 桩的结论没落进正文 —— 桩没被真正用上")
            self.assertNotIn("stale", fm, "set_polish 成功后 stale 要被剥掉")
        joined = "；".join(res["notes"])
        self.assertIn("没编进去", joined, "失败那批必须在编译结果里说出来")
        self.assertIn("第 2/3 批", joined)
        for c in cs[8:16]:
            self.assertIn(str(c.id), joined)


class BudgetLogTest(TestCase):
    """`_call` 的「翻倍重试」日志必须与真实行为一致。

    ★ 2026-11-03 实测的假日志：循环只有两轮（4096 → 8192），第二轮吃完预算后
      仍然打「预算翻倍重试（→ 16384）」—— 16384 那次**从来没发生过**。排查时
      人以为「都试到 16384 了还不行」，正好在最需要准确的地方骗人。
    """

    def _empty(self):
        return cr.ChatResult("", {"finish_reason": "length", "prompt_tokens": 4322,
                                  "completion_tokens": 8192}, "", False, None)

    def test_last_attempt_does_not_claim_another_double(self):
        with patch.object(cr, "chat_recorded", return_value=self._empty()):
            with self.assertLogs("core.citeguard", level="WARNING") as cm:
                text, err = citeguard._call(
                    "wiki_compile", "sys", "user", citeguard.fresh_stats(),
                    max_tokens=4096, tag="wiki 编译分组", label="第 1/3 批")
        self.assertEqual(text, "")
        self.assertIn("推理占满", err)
        logs = "\n".join(cm.output)
        self.assertIn("→ 8192", logs, "第一轮之后确实会翻倍一次")
        self.assertNotIn("16384", logs, "第二轮是最后一次，不许再宣称翻倍到 16384")
        self.assertIn("不再翻倍", logs, "最后一轮要说清「这就是最后一次」")

    def test_retry_is_exactly_twice(self):
        with patch.object(cr, "chat_recorded", return_value=self._empty()) as m:
            citeguard._call("wiki_compile", "sys", "user", citeguard.fresh_stats(),
                            max_tokens=4096, tag="t")
        self.assertEqual(m.call_count, 2, "只翻倍一次；不能变成无限重试（那是无限花钱）")

    def test_success_first_try_has_no_budget_warning(self):
        ok = cr.ChatResult("正文", {"finish_reason": "stop"}, "", False, None)
        with patch.object(cr, "chat_recorded", return_value=ok):
            with self.assertNoLogs("core.citeguard", level="WARNING"):
                text, err = citeguard._call(
                    "wiki_compile", "sys", "user", citeguard.fresh_stats(),
                    max_tokens=4096, tag="t")
        self.assertEqual((text, err), ("正文", ""))


class CapOutSplitTest(TestCase):
    """撞预算 → **对半拆批重跑**，不是「token 翻倍重打一遍」（2026-11-09）。

    真实数据（LlmUsage 逐行，清零重来第一次真跑）：第 4/4 批 16 条 → 4096 撞顶
    （out=4096、正文空、29.7 s、¥0.041）→ 翻倍 8192 重打 → **8192 也撞顶**
    （out=8192、57.1 s、¥0.070）→ 那 16 条候选最后一条都没进去。
    两次共烧 12288 output tokens、87 秒，收益 0。

    原因：输出量随**候选条数**涨，不随 max_tokens 涨。所以撞顶的正解是把任务
    变小（拆批），不是把额度调大。这条用例钉住三件事：
      ① 多条批不再让 `_call` 自己翻倍（`retry_on_empty=False`）；
      ② 撞顶那一批被拆成两半重跑，且**所有候选最后都有组**；
      ③ 拆这件事写进 notes（人要知道「钱多花在拆批上」）。
    """

    def _cs(self, n=21):
        return [mk_cand("判断%d" % i, "判断%d" % i, sid="daily-2026-10-%02d" % (i + 1))
                for i in range(n)]

    def test_cap_out_splits_the_batch_instead_of_doubling_tokens(self):
        cs = self._cs()

        def plan(bi, ids):
            # 第一次（8 条）撞顶，其余（拆出来的 4+4 等）正常分组
            return ("", "wiki 编译分组：推理占满 4096 tokens 预算，正文为空") \
                if bi == 1 else one_group(ids)

        with mock.patch.object(ingest, "COMPILE_S1_BATCH", 8):
            with patch_s1(plan) as (call, seen):
                groups, err, notes = ingest.plan_groups(cs)

        self.assertEqual(err, "", "拆批后应当全部成功")
        # 8/8/5 三批；第一批撞顶拆成 4+4 → 共 5 次调用
        self.assertEqual(call.call_count, 5, "撞顶那批要拆成两半各跑一次")
        self.assertEqual([len(s["ids"]) for s in seen[:3]], [8, 4, 4],
                         "拆出来的两半要紧接着跑，且各占原批的一半")
        for s in seen:
            self.assertFalse(s["retry_on_empty"],
                             "多条批不许再走 token 翻倍（那是白花钱的那条路）")
        covered = sorted(i for g in groups for i in g["cand_ids"])
        self.assertEqual(covered, sorted(c.id for c in cs), "拆批不许丢候选")
        self.assertIn("对半拆成 4+4 条重跑", "；".join(notes))

    def test_truncated_json_also_splits_not_just_empty_text(self):
        """★ 撞顶的第二种形态：有正文但 JSON 被切断 —— 也必须拆批。

        实测 2026-11-09 那次 i=3835 / out=4096 就是这一形态：`_call` **不报错**
        （finish_reason=length 但 content 非空，只往 `stats["truncated"]` 记一笔），
        正文是一段没闭合的 JSON。只判「推理占满」的话，这批会被当成「模型输出
        不合格」记一笔失败，8 条候选白丢 —— 那正是当时真实发生的事。
        """
        cs = self._cs()
        state = {"bi": 0}

        def fake(kind, system, user, stats, **kw):
            state["bi"] += 1
            payload = json.loads(user)
            ids = [c["cand_id"] for c in payload["candidates"]]
            if state["bi"] == 1:
                # 真·半截 JSON + stats 记一笔截断（真实 `_call` 的行为）
                stats.setdefault("truncated", []).append("wiki 编译分组（预算 4096 tokens）")
                return '{"groups":[{"title":"组-1","domain":"invest","cand_ids":[', ""
            return one_group(ids), ""

        with mock.patch.object(ingest, "COMPILE_S1_BATCH", 8):
            with patch("core.citeguard._call", side_effect=fake) as call:
                groups, err, notes = ingest.plan_groups(cs)

        self.assertEqual(err, "", "拆批后应当全部成功")
        self.assertEqual(call.call_count, 5, "撞顶那批要拆成两半各跑一次")
        covered = sorted(i for g in groups for i in g["cand_ids"])
        self.assertEqual(covered, sorted(c.id for c in cs), "拆批不许丢候选")
        self.assertIn("输出被截断", "；".join(notes))

    def test_single_candidate_batch_still_allowed_to_retry(self):
        """拆不动的单条批：仍然允许 `_call` 翻倍自愈（否则只剩硬失败）。"""
        cs = self._cs(2)
        with mock.patch.object(ingest, "COMPILE_S1_BATCH", 1):
            with patch_s1(lambda bi, ids: one_group(ids)) as (_call, seen):
                ingest.plan_groups(cs)
        self.assertEqual([s["retry_on_empty"] for s in seen], [True, True],
                         "单条批没有「拆」这条路，翻倍重试必须留着")
