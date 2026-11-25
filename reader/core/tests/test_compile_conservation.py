"""编译的**候选守恒**：喂进去的候选不许凭空消失（2026-11-09）。

来源是一次真实事故：清零重来后第一次真跑（真实库 33 个来源），S1 拿到 60 条候选，
最后 15 页草稿的 frontmatter 里只有 **28 条** —— 32 条没了。日志和反馈当时都写
「60 条候选」，人以为全进去了。

根因不是模型坏了一半，而是**程序从没验过**：`COMPILE_S1_SYSTEM` 明写
「每条候选必须且只能出现在一组里。不许丢、不许重复。」，但没有任何代码去比
「喂进去的 id」与「落到草稿里的 id」。提示词承诺 ≠ 程序事实 —— 这正是本项目
最贵的那类坑。

判据：按**磁盘上的草稿**算差集，差额进 `notes`（人可见）+ `log.error`（机器可见）
+ 返回值 `unplaced`；不整轮回滚（草稿是好的，且那些候选仍在「待编译」里，
下一轮会被再试）。
"""
from core import ingest
from core.models import IngestCandidate
from core.tests.test_ingest_compile import (CompileBase, mk_cand, patch_compile,
                                            s1_group)


class UnplacedCandidateTest(CompileBase):
    def test_group_that_misses_a_candidate_is_reported(self):
        """★ 守卫会响：S1 漏掉一条，必须点名报出来。"""
        a = mk_cand("原话 A", "判断 A")
        b = mk_cand("原话 B", "判断 B")
        c = mk_cand("原话 C", "判断 C")
        with patch_compile([s1_group([a.id, b.id])]):
            res = ingest.compile_candidates()
        self.assertTrue(res["ok"], res.get("err"))
        self.assertEqual(res["landed"], 2, "前置：只有两条真落地")
        self.assertEqual(res["unplaced"], [c.id], "漏掉的那条必须被点名")
        joined = "\n".join(res["notes"] or [])
        self.assertIn("没被分到任何一页", joined)
        self.assertIn("#%d" % c.id, joined)
        self.assertIn("下次编译会再试", joined)

    def test_unplaced_stays_compilable_for_next_round(self):
        """★ 漏掉的候选不能被记成「已编译」——它得还能被下一轮吃到。"""
        a = mk_cand("原话 A", "判断 A")
        c = mk_cand("原话 C", "判断 C")
        with patch_compile([s1_group([a.id])]):
            res = ingest.compile_candidates()
        self.assertEqual(res["unplaced"], [c.id])
        bl = ingest.compile_backlog()
        self.assertIn(c.id, [x.id for x in bl["cands"]],
                      "漏掉的候选若不在了，就是被静默吞掉")
        self.assertNotIn(a.id, [x.id for x in bl["cands"]])

    def test_full_coverage_says_nothing(self):
        """反证：全覆盖时不许冒这句，否则人天天读一条无意义告警。"""
        a = mk_cand("原话 A", "判断 A")
        b = mk_cand("原话 B", "判断 B")
        with patch_compile([s1_group([a.id, b.id])]):
            res = ingest.compile_candidates()
        self.assertEqual(res["unplaced"], [])
        self.assertEqual(res["landed"], 2)
        self.assertFalse([n for n in (res["notes"] or []) if "没被分到" in n])

    def test_guard_sees_disk_not_intent(self):
        """★ 守卫的论域是**磁盘产物**：把草稿删掉，差额必须自己长出来。

        这条盯的是「守卫会不会被内存里的计划骗过去」—— `made` 里写着这一组
        有 2 条候选，但文件已经不是那样了。
        """
        a = mk_cand("原话 A", "判断 A")
        b = mk_cand("原话 B", "判断 B")
        with patch_compile([s1_group([a.id, b.id])]):
            res = ingest.compile_candidates()
        self.assertEqual(res["unplaced"], [], "前置：这一轮本来是全覆盖的")
        # 场景对照：把落地的草稿从磁盘上抹掉，`_compiled_ids` 也应回到「没编译」
        for p in (ingest.wstore.root() / ".drafts").glob("*.md"):
            p.unlink()
        self.assertEqual(IngestCandidate.objects.count(), 2)
        bl = ingest.compile_backlog()
        self.assertEqual(
            sorted(x.id for x in bl["cands"]), sorted([a.id, b.id]),
            "草稿不在磁盘上，这批候选就该回到待编译")
