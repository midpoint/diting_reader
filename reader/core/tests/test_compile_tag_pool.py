"""标签收敛（2026-11-16）：S1 起标签时先看库里已在用的词，而不是每页自由造词。

为什么要有这一组：真实库 32 页长出约 96 个标签，其中 **93 个只出现过一次**
（只读统计，2026-11-16）。后果不是「不好看」，是 faceted 筛选做出来也筛不出东西
—— 每个标签只对应一页。病根在 prompt：模型每页自由造词，没有「优先复用」这一眼。

这一组钉三件事：
  1. `_tag_pool()` 的口径：按使用次数降序、次数相同按词、有上限、空/空白标签跳过；
  2. `_plan_payload()` 里「空池」与「没有池」是两个事实 —— 空池**不写这个键**
     （写 `[]` 会让模型以为「库里有标签，但一个都没给我看」）；
  3. prompt 里那句「优先复用」有**程序事实**兜底：`plan_groups` 真的把池喂给了
     S1，并把「复用 N / 新造 M」报进回执 —— 否则又只是一句没人验的提示词承诺。

花钱点全打桩（`core.citeguard._call`），并断言桩真被调用过：桩没被用上就等于
这次真的联网花了钱，而测试照样全绿。
"""
import json
from unittest.mock import patch

from django.test import TestCase

from core import ingest
from core.tests.test_ingest_compile import mk_cand


def page(slug, tags, *, domain="ai", title=None, summary="一句话"):
    """一页 wiki 的最小形态（与 `wstore.pages()` 同形：tags 是字符串列表）。"""
    return {"slug": slug, "title": title or slug, "domain": domain,
            "summary": summary, "tags": list(tags), "candidates": [],
            "updated": "2026-11-16", "draft": False}


def s1_reply(tags, *, title="新页", slug="new-page", domain="ai", cid=1, target="new"):
    return json.dumps({"groups": [{"title": title, "slug": slug, "tags": list(tags),
                                   "domain": domain, "cand_ids": [cid],
                                   "target": target, "reason": "同一个问题"}]},
                      ensure_ascii=False)


class TagPoolFnTest(TestCase):
    """`_tag_pool()` 自身口径。纯函数，不花钱。"""

    def test_orders_by_count_then_word(self):
        """出现次数多的靠前；次数相同按词升序 —— 顺序是「哪些词常用」的信号。

        ★ 同次数比的是 **Unicode 码点**，不是拼音：`AI泡沫` 在 `算力` 前面。
          这条不是口味问题 —— 「按拼音」需要额外的库、且中英混排没有统一答案，
          而这里的顺序只用来表达「谁更常用」，同次数内部谁先谁后不影响判断。
        """
        pages = [page("a", ["算力", "AI泡沫"]), page("b", ["算力"]),
                 page("c", ["Z字头"])]
        self.assertEqual(ingest._tag_pool(pages), ["算力", "AI泡沫", "Z字头"])

    def test_respects_limit(self):
        pages = [page("a", ["一"]), page("b", ["二"]), page("c", ["三"])]
        self.assertEqual(len(ingest._tag_pool(pages, limit=2)), 2)
        self.assertEqual(ingest._tag_pool(pages, limit=0), [])

    def test_skips_blank_and_missing(self):
        """空串 / 纯空格 / 缺 tags 字段都不许变成池里的空标签。"""
        pages = [page("a", ["算力", "", "   "]), {"slug": "b"}]
        self.assertEqual(ingest._tag_pool(pages), ["算力"])

    def test_empty_library_gives_empty_pool(self):
        self.assertEqual(ingest._tag_pool([]), [])
        self.assertEqual(ingest._tag_pool(None), [])

    def test_stable_across_repeated_runs(self):
        """同一份库两次运行必须得到同一份池，否则 prompt 不可复现。"""
        pages = [page("a", ["X", "Y"]), page("b", ["Y", "Z"]), page("c", ["Z", "X"])]
        first = ingest._tag_pool(pages)
        self.assertEqual(first, ingest._tag_pool(list(reversed(pages))))


class PlanPayloadTagPoolTest(TestCase):
    """材料层：「空池」与「没有池」必须可区分。"""

    def test_payload_carries_pool(self):
        raw = ingest._plan_payload([], [], 0, None, ["算力", "AI泡沫"])
        self.assertEqual(json.loads(raw)["tag_pool"], ["算力", "AI泡沫"])

    def test_payload_omits_key_when_pool_empty(self):
        """★★ 空池不写这个键 —— 写 `[]` 等于告诉模型「库里有标签但没列给你」。"""
        for empty in ([], None):
            raw = ingest._plan_payload([], [], 0, None, empty)
            self.assertNotIn("tag_pool", json.loads(raw),
                             "空池写成了 %r，模型会以为有标签只是没给它看" % (empty,))


class TagPoolInPromptTest(TestCase):
    """端到端（打桩）：池真的进了 S1 材料，且复用/新造有可见统计。"""

    def setUp(self):
        self.calls = []

    def _patch(self, reply):
        calls = self.calls

        def fake(kind, system, user, stats, **kw):
            calls.append({"kind": kind, "system": system, "user": user, **kw})
            return reply, ""

        return patch("core.citeguard._call", side_effect=fake)

    def test_s1_material_contains_pool(self):
        """池真的出现在 S1 的**用户消息**里（不是只在 system 里提名字）。"""
        c = mk_cand("原文", "十年现金流")
        pages = [page("old-a", ["算力"]), page("old-b", ["算力"])]
        with self._patch(s1_reply(["算力"])):
            ingest.plan_groups([c], pages)
        self.assertEqual(len(self.calls), 1, "桩没被用上 = 这次可能真联网花钱了")
        payload = json.loads(self.calls[0]["user"])
        self.assertEqual(payload["tag_pool"], ["算力"], "池没进材料")

    def test_reports_reuse_and_fresh_with_consistent_sum(self):
        """回执里「复用 + 新造」必须加得回总数，且新造词被点名。"""
        c = mk_cand("原文", "十年现金流")
        pages = [page("old-a", ["算力", "AI泡沫"]), page("old-b", ["算力"])]
        with self._patch(s1_reply(["算力", "新造词"])):
            _groups, err, notes = ingest.plan_groups([c], pages)
        self.assertEqual(err, "")
        text = "；".join(notes)
        self.assertIn("本批建议 2 个", text)
        self.assertIn("复用池内 1 个", text)
        self.assertIn("新造 1 个", text)
        self.assertIn("新造词", text)
        self.assertNotIn("AI泡沫", text.split("新造")[-1], "池内的词被当成了新造")

    def test_library_empty_has_no_tag_note(self):
        """库刚清零时全是新造，报「新造 96 个」纯属噪音 —— 不报。"""
        c = mk_cand("原文", "十年现金流")
        with self._patch(s1_reply(["任意词"])):
            _groups, err, notes = ingest.plan_groups([c], [])
        self.assertEqual(err, "")
        self.assertNotIn("标签：", "；".join(notes))

    def test_merging_group_does_not_count_as_fresh(self):
        """并入已有页时老标签被保留，不产生新词 —— 不能算进「新造」。"""
        c = mk_cand("原文", "补一条判断")
        pages = [page("old-a", ["算力"])]
        with self._patch(s1_reply(["另一词"], target="existing:old-a")):
            _groups, err, notes = ingest.plan_groups([c], pages)
        self.assertEqual(err, "")
        self.assertNotIn("标签：", "；".join(notes), "并入组被算成了新造标签")


class PromptWordingTest(TestCase):
    """提示词本身要说清「逐字照抄」和「没有这个字段」两种情形。"""

    def test_prompt_says_verbatim_reuse(self):
        p = ingest.COMPILE_S1_SYSTEM
        self.assertIn("tag_pool", p)
        self.assertIn("逐字照抄", p)
        self.assertIn("没有** `tag_pool` 这个字段", p,
                      "没交代「空库没有这个字段」，模型会把缺省当成「不许用标签」")
