"""共享原语的单元测试：jsontool / mdlines / jobstate / titletext。

这些模块都是「收敛重复实现」的产物（见各模块头）。除了各自的行为，
还钉住 summary / translate 的 re-export 兼容面 —— verify 脚本仍通过旧命名空间
访问这些函数。
"""
import re

from django.test import SimpleTestCase

from core import jobstate, jsontool, mdlines, reporttext, summary, titletext, translate


class StripCodeFenceTest(SimpleTestCase):
    def test_json_fence(self):
        self.assertEqual(jsontool.strip_code_fence('```json\n[1]\n```', "json"), "[1]")
        self.assertEqual(jsontool.strip_code_fence('```\n[1]\n```', "json"), "[1]")

    def test_markdown_fence(self):
        self.assertEqual(
            jsontool.strip_code_fence("```markdown\n# 标题\n```", "markdown", "md"),
            "# 标题")
        self.assertEqual(jsontool.strip_code_fence("```md\nx\n```", "markdown", "md"), "x")

    def test_inner_fence_behavior_matches_legacy(self):
        # ★ 内部围栏也会被首尾匹配剥掉（\s* 跨换行）—— 这是**原实现的既有行为**，
        #   重构保持逐字一致；真实场景里模型只会把整个输出包一道围栏，不会踩到。
        s = "正文\n```python\ncode\n```\n结尾"
        legacy = re.sub(r"^```(?:json)?\s*|\s*```$", "", s.strip(),
                        flags=re.I | re.M).strip()
        self.assertEqual(jsontool.strip_code_fence(s, "json"), legacy)


class IterJsonTest(SimpleTestCase):
    def test_array_first(self):
        self.assertEqual(next(jsontool.iter_json('[{"id": 1}]')), [{"id": 1}])

    def test_prose_wrapped(self):
        got = next(jsontool.iter_json('好的，结果如下：\n[{"id": 4}]\n以上。'))
        self.assertEqual(got, [{"id": 4}])

    def test_fenced(self):
        got = next(jsontool.iter_json('```json\n[{"id": 3}]\n```'))
        self.assertEqual(got, [{"id": 3}])

    def test_dict_then_array_order(self):
        # 先试数组后试对象：数组解析失败时才轮到对象形态
        self.assertEqual(next(jsontool.iter_json('垃圾 [broken {"id": 2}')),
                         {"id": 2})

    def test_garbage_yields_nothing(self):
        self.assertEqual(list(jsontool.iter_json("not json")), [])


class MdLinesTest(SimpleTestCase):
    def test_cells(self):
        self.assertEqual(mdlines.cells("| a | b |"), ["a", "b"])
        self.assertEqual(mdlines.cells("a|b"), ["a", "b"])
        self.assertEqual(mdlines.cells("|  x  |"), ["x"])

    def test_grammar(self):
        self.assertTrue(mdlines.HEAD.match("## 标题"))
        self.assertTrue(mdlines.UL.match("  - 项"))
        self.assertTrue(mdlines.OL.match("1. 项"))
        self.assertTrue(mdlines.BQ.match("> 引用"))
        self.assertTrue(mdlines.HR.match("---"))
        self.assertTrue(mdlines.TABLE_SEP.match("| --- | --- |"))
        self.assertIsNone(mdlines.LINK.match("[x](javascript:alert(1))"))  # 不安全 scheme
        self.assertIsNotNone(mdlines.LINK.match("[x](https://e.com/a)"))


class JobStateTest(SimpleTestCase):
    def test_lifecycle(self):
        job = jobstate.JobState(total=0)
        self.assertFalse(job.running)
        job.begin(total=5, started_at=1.0)
        self.assertTrue(job.running)
        self.assertEqual(job.snapshot()["total"], 5)
        job.update(done=3)
        job.mutate(lambda s: s.update(done=s["done"] + 1, ok=s["done"]))
        self.assertEqual(job.snapshot(), {"running": True, "total": 5,
                                          "started_at": 1.0, "done": 4, "ok": 3})
        job.finish(finished_at=2.0)
        snap = job.snapshot()
        self.assertFalse(snap["running"])
        self.assertEqual(snap["finished_at"], 2.0)

    def test_snapshot_is_copy(self):
        # 快照不能给调用方可变引用（refreshjob 的教训）
        job = jobstate.JobState(feeds=[1, 2])
        snap = job.snapshot()
        snap["feeds"].append(3)
        self.assertEqual(job.snapshot()["feeds"], [1, 2])


class TitleTextTest(SimpleTestCase):
    def test_check_title(self):
        self.assertTrue(titletext.check_title("Nvidia beats expectations")[0])
        self.assertFalse(titletext.check_title("英伟达业绩超预期")[0])
        self.assertFalse(titletext.check_title("Nvidia 业绩超预期")[0])  # 中英混排不翻
        self.assertFalse(titletext.check_title("OK")[0])                 # 拉丁字母太少
        self.assertFalse(titletext.check_title("https://example.com/a")[0])

    def test_hash_stable_over_whitespace(self):
        self.assertEqual(titletext.title_hash("A  B"), titletext.title_hash(" A B "))


class ReexportCompatTest(SimpleTestCase):
    """拆分后旧命名空间必须继续可用（verify 脚本依赖这些名字）。"""

    def test_summary_reexports_reporttext(self):
        self.assertIs(summary.number_audit, reporttext.number_audit)
        self.assertIs(summary.classify_kind, reporttext.classify_kind)
        self.assertIs(summary.build_items, reporttext.build_items)
        self.assertIs(summary.count_cites, reporttext.count_cites)

    def test_translate_reexports_titletext(self):
        self.assertIs(translate.check_title, titletext.check_title)
        self.assertIs(translate.parse_translations, titletext.parse_translations)
        self.assertIs(translate.title_hash, titletext.title_hash)
