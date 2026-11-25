"""编译「并入已有页」（阶段 2.3d / 第十三步形态 A）：S1 分组，结论由 polish 统一写。

这一组盯的是**合并不会吃人已经确认过的知识**。合并比新建贵得多：

  新建错了 = 多一页，删掉即可；
  合并错了 = 一页人确认过的判断被少写一条，而页还在、还挺长，
             没有报错、没有断链 —— 几个月后你才发现某句话不见了。

形态 A 的定案（用户拍板）：正文 = 纯 polish 一段，**frontmatter `candidates`
是分组唯一真值**；S2 已退役，`_compose_page` 只产 frontmatter + 空正文 +
`stale: true`，由 `wiki_loop._polish_one` 统一写结论（成功才剥 stale）。
所以「老正文逐行守恒」不再适用 —— 老正文根本不进草稿。

所以这里的断言几乎全是**反证 + 结构守恒**。花钱点全打桩：S1 分组桩在
`core.citeguard._call`（`patch_call`），结论重编桩在**被调用点**
`core.wiki_loop._call`，并配「桩真被用上」反证（正文真出现桩结论）：
  - frontmatter 的 candidates / sources 只增不减（并集口径）；
  - 并入 noop 是程序判据：`set(group cand_ids) <= 老页 candidates`；
  - 故意让候选变少 → `_validate_draft` 拒入库；丢老 wikilink / 引用越界 →
    `wiki_loop._check_links` 拒写（引用守恒）；
  - 正文 == 桩返回的纯 polish 一段，不含三块 / 双层标题 / 候选清单；
  - merges 指向不存在的页、跨域、丢掉老候选 —— 一律拒绝；
  - 已有一份待审批草稿时，新的并入**进那份草稿**（2026-11-04：不再拒掉，
    人审批一次就好）。
"""
import json
import shutil
import tempfile
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from core import ingest
from core import wiki as wstore
from core import wiki_loop as wl
from core.models import IngestCandidate
from core.tests.test_ingest_compile import mk_cand, page_json, patch_call

OLD_ID = 50

OLD_PAGE = """---
title: LLM 护城河在验证器
domain: invest
sources: [entry:6810]
updated: 2026-10-01
confidence: medium
candidates: [50]
---

## 我的判断
- LLM 基座难差异化，护城河在验证器 [文章《大模型的护城河》](/entry/6810?src=wiki)

## 事实 / 他人的观点
- （无）

## 我的在意点
- （无）
"""


def group_json_target(cand_ids, target, title="LLM 护城河在验证器", domain="invest"):
    # ★ 形态 A：S1 的组顺带给 `slug`（ASCII kebab）/ `tags`，否则 `_compose_page`
    #   新建时会 fallback 到 `<domain>-<minid>`，按预期文件名找不到草稿。
    return json.dumps({"groups": [{"title": title, "domain": domain,
                                   "cand_ids": list(cand_ids), "target": target,
                                   "slug": "llm-moat", "tags": ["护城河"],
                                   "reason": "同一问题"}]}, ensure_ascii=False)


def merge_reply(lines, facts=None, hint=""):
    """S2 退役后不再被消费；仅留作 `test_s1_sees_draft_pages_in_catalog` 里
    fake 的「不该被调用」兜底分支（polish 已改走 `wiki_loop._call` 桩）。"""
    return json.dumps({"lines": lines, "facts": facts or [], "hint": hint},
                      ensure_ascii=False)


class MergeBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-merge-")
        cls.ov = override_settings(WIKI_DIR=cls.tmp)
        cls.ov.enable()

    @classmethod
    def tearDownClass(cls):
        cls.ov.disable()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        ingest.title_of.cache_clear()
        shutil.rmtree(self.tmp, ignore_errors=True)
        wstore.ensure_dirs()
        self.put_page("invest", "llm-moat", OLD_PAGE)

    # ---- 夹具
    def put_page(self, domain, slug, text):
        p = wstore.root() / "wiki" / domain / f"{slug}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def page_text(self, domain, slug):
        return (wstore.root() / "wiki" / domain / f"{slug}.md").read_text(encoding="utf-8")

    def draft_text(self, slug):
        return (wstore.root() / ".drafts" / f"{slug}.md").read_text(encoding="utf-8")

    def draft_path(self, slug):
        return wstore.root() / ".drafts" / f"{slug}.md"


class PlanTargetTest(MergeBase):
    """S1 的 target 校验：模型说什么不算，清单与 domain 说了算。"""

    def _pages(self):
        return wstore.pages()

    def test_existing_target_is_accepted(self):
        c = mk_cand("护城河在验证器", "护城河在验证器")
        with patch_call([group_json_target([c.id], "existing:llm-moat")]) as call:
            groups, err, _ = ingest.plan_groups([c], self._pages())
        self.assertEqual(call.call_count, 1)
        self.assertEqual(err, "")
        self.assertEqual(groups[0]["target"], "llm-moat")

    def test_target_outside_catalog_falls_back_to_new(self):
        """清单里没有的 slug 一律不认 —— 模型编一个页名也不能建出「并入」的假动作。"""
        c = mk_cand("护城河在验证器", "护城河在验证器")
        with patch_call([group_json_target([c.id], "existing:no-such-page")]):
            groups, err, _ = ingest.plan_groups([c], self._pages())
        self.assertEqual(err, "")
        self.assertEqual(groups[0]["target"], "", "不存在的目标必须退回新建")
        self.assertIn("no-such-page", groups[0]["note"])

    def test_cross_domain_merge_rejected(self):
        c = mk_cand("护城河在验证器", "护城河在验证器", domain="ai")
        with patch_call([group_json_target([c.id], "existing:llm-moat", domain="ai")]):
            groups, _, _ = ingest.plan_groups([c], self._pages())
        self.assertEqual(groups[0]["target"], "", "不跨域")
        self.assertIn("不跨域", groups[0]["note"])

    def test_two_groups_cannot_merge_same_page(self):
        a = mk_cand("护城河在验证器", "护城河在验证器")
        b = mk_cand("验证器决定数据差异化", "验证器决定数据")
        reply = json.dumps({"groups": [
            {"title": "组一", "domain": "invest", "cand_ids": [a.id], "target": "existing:llm-moat"},
            {"title": "组二", "domain": "invest", "cand_ids": [b.id], "target": "existing:llm-moat"},
        ]}, ensure_ascii=False)
        with patch_call([reply]):
            groups, _, _ = ingest.plan_groups([a, b], self._pages())
        # ★ 先钉住条数再断言第二条：少了一组时 `groups[1]` 会 IndexError，
        #   报出来是 ERROR（看着像脚本坏了），而不是「降级逻辑没了」这句人话。
        self.assertEqual(len(groups), 2, "一组就说明「同批第二组退回新建」的降级没了")
        self.assertEqual(groups[0]["target"], "llm-moat")
        self.assertEqual(groups[1]["target"], "", "第二组必须退回新建，否则会盖掉第一组")
        self.assertIn("同一页", groups[1]["note"])

    def test_no_target_means_new(self):
        c = mk_cand("护城河在验证器", "护城河在验证器")
        reply = json.dumps({"groups": [{"title": "t", "domain": "invest",
                                        "cand_ids": [c.id]}]}, ensure_ascii=False)
        with patch_call([reply]):
            groups, _, _ = ingest.plan_groups([c], self._pages())
        self.assertEqual(groups[0]["target"], "", "老模型不写 target 时不能变成并入")


class MergeCompileTest(MergeBase):
    """并入的编排：草稿认得出「并入哪一页」，候选/来源只增不减，正文是纯 polish。

    形态 A：S1 分组桩在 `core.citeguard._call`（`patch_call`），结论重编桩在
    被调用点 `core.wiki_loop._call`；每条正向用例都配「桩真被用上」反证
    （草稿正文真出现桩结论 或 mock call_count > 0），绝不真联网。
    """

    # 两个花钱点一起打桩：S1 分组 + 结论重编（形态 A 的 S2 已退役）。
    POLISH = ("这是一段结论。", [], None)

    def _compile(self, group_reply):
        """跑一次编译，返回 (res, s1_mock, polish_mock)。"""
        with patch_call([group_reply]) as s1, \
                patch("core.wiki_loop._call", return_value=self.POLISH) as polish:
            res = ingest.compile_candidates()
        return res, s1, polish

    def test_draft_carries_merges_and_grows_candidates(self):
        c = mk_cand("护城河在验证器，谁掌握差异化验证器谁就有差异化数据",
                    "护城河在差异化验证器", confidence="high")
        res, s1, polish = self._compile(group_json_target([c.id], "existing:llm-moat"))
        self.assertTrue(res["ok"], res.get("err"))
        self.assertEqual(res["drafts"], ["llm-moat"], "合并草稿必须与目标页同名")
        self.assertEqual(s1.call_count, 1, "S2 已退役，分组只调一次 S1")
        self.assertGreater(polish.call_count, 0, "结论重编桩真被用上（没真联网）")

        fm, body = wstore.parse_front(self.draft_text("llm-moat"))
        self.assertEqual(fm["merges"], "llm-moat")
        self.assertEqual(fm["domain"], "invest")
        self.assertEqual([int(x) for x in fm["candidates"]], [OLD_ID, c.id],
                         "候选只增不减，老候选要排在前面")
        self.assertEqual(fm["sources"], ["entry:6810", "report:daily-2026-10-31"],
                         "来源要并集：少了老来源，反向映射就断了")
        self.assertEqual(fm["confidence"], "medium",
                         "置信度取老页原值，不能因为加了一条 high 就整页变 high")
        # 形态 A：正文 = 桩返回的纯 polish 一段，不搬老正文 / 不写清单 / 不带标题
        self.assertEqual(body.strip(), "这是一段结论。",
                         "桩结论真被写进正文（重编桩被用上），且只有这一段")
        for head in (wstore.BODY_HEAD, wstore.POLISH_HEAD, "## 我的判断"):
            self.assertNotIn(head, body, "形态 A 不写三块 / 双层标题")
        self.assertNotIn(f"`#{c.id}`", body, "候选明细清单不落盘（frontmatter 是唯一真值）")
        self.assertNotIn(wstore.STALE_FM, fm, "重编成功才剥 stale")

    def test_old_body_lines_verbatim(self):
        """形态 A：老正文逐行守恒已退役（正文 = 纯 polish 一段，不搬老正文）。
        并入守恒改成「候选集合只增不减」的程序判据：正常草稿 candidates 是老页
        并集、能入库；故意把老候选抹掉（候选变少）→ `_validate_draft` 必须拒。"""
        c = mk_cand("补一条新的", "补一条新的")
        res, _, polish = self._compile(group_json_target([c.id], "existing:llm-moat"))
        self.assertTrue(res["ok"], res.get("err"))
        self.assertGreater(polish.call_count, 0, "结论重编桩真被用上")
        text = self.draft_text("llm-moat")
        fm, body = wstore.parse_front(text)
        self.assertEqual(fm["merges"], "llm-moat", "merges 原样保留")
        self.assertEqual([int(x) for x in fm["candidates"]], [OLD_ID, c.id],
                         "候选只增不减，老候选还在")
        self.assertEqual(body.strip(), "这是一段结论。", "正文是纯 polish，不再搬运老正文")
        self.assertNotIn("## 我的判断", body)
        # 正向：正文非空 + 候选没少，能入库
        ok, why = wstore._validate_draft("llm-moat")
        self.assertTrue(ok, why)
        # 变异反证：故意丢掉老候选 → 并入守恒守卫必须拒
        shrunk = text.replace(f"candidates: [{OLD_ID}, {c.id}]",
                              f"candidates: [{c.id}]")
        self.assertNotEqual(shrunk, text, "篡改必须真的命中 candidates 行")
        self.draft_path("llm-moat").write_text(shrunk, encoding="utf-8")
        ok2, why2 = wstore._validate_draft("llm-moat")
        self.assertFalse(ok2, "并入少一条老候选 = 安静删判断，必须被拒")
        self.assertIn("丢掉原有候选", why2)

    def test_old_numbers_count_as_sourced(self):
        """形态 A：S2 数字守卫已退役，数字改由 `wiki_loop._check_numbers` 守结论正文。
        这里仍验「老页里的数字不挡住并入」：桩结论无数字，编译照常成功、正文是结论。"""
        old = OLD_PAGE.replace("candidates: [50]",
                               "candidates: [50]\nfacts:\n  - 2024 年 12 月样本 1200 条")
        self.put_page("invest", "llm-moat", old)
        c = mk_cand("补一条新的", "补一条新的")
        res, _, polish = self._compile(group_json_target([c.id], "existing:llm-moat"))
        self.assertTrue(res["ok"], res.get("err"))
        self.assertGreater(polish.call_count, 0, "结论重编桩真被用上")
        _, body = wstore.parse_front(self.draft_text("llm-moat"))
        self.assertEqual(body.strip(), "这是一段结论。", "正文只有 polish 一段")

    def test_other_blocks_and_hint_are_kept(self):
        """形态 A：老页里人手写补充块、老的「可能的联系」hint 都**不再搬进正文**
        （正文 = 纯 polish 一段，分组真值是 frontmatter candidates）。
        守恒改看结构：sources / candidates 只增不减、merges 指向并入页。"""
        old = OLD_PAGE + "\n## 补充（我手写的）\n- 这段是我自己加的，编译不该删它\n"
        self.put_page("invest", "llm-moat", old)
        c = mk_cand("补一条新的", "补一条新的")
        res, _, polish = self._compile(group_json_target([c.id], "existing:llm-moat"))
        self.assertTrue(res["ok"], res.get("err"))
        self.assertGreater(polish.call_count, 0, "结论重编桩真被用上")
        fm, body = wstore.parse_front(self.draft_text("llm-moat"))
        self.assertEqual(fm["merges"], "llm-moat")
        self.assertEqual([int(x) for x in fm["candidates"]], [OLD_ID, c.id],
                         "候选只增不减")
        self.assertEqual(fm["sources"], ["entry:6810", "report:daily-2026-10-31"],
                         "老来源并集守恒，反向映射不断")
        self.assertEqual(body.strip(), "这是一段结论。", "正文只有 polish 一段")
        self.assertNotIn("这段是我自己加的", body, "老手写块不再搬进纯 polish 正文")
        self.assertNotIn("## 可能的联系", body, "hint 不再写进正文")
        for head in (wstore.BODY_HEAD, wstore.POLISH_HEAD, "## 我的判断",
                     "## 补充"):
            self.assertNotIn(head, body, "形态 A 不写任何结构标题")

    def test_all_skipped_is_not_a_failure(self):
        """形态 A：并入 noop 是**程序判据** —— 这一组的候选老页全都有了
        （`set(group cand_ids) <= 老页 candidates`）就不该再出一份草稿、不该报错。
        编译链里这条由两层保证：`compile_backlog` 按 `_compiled_ids` 幂等剔除
        已编译候选，以及 `_compose_page` 里那条集合判据（这里都验）。"""
        c = mk_cand("LLM 基座难差异化，护城河在验证器", "护城河在验证器")
        old = OLD_PAGE.replace("candidates: [50]", f"candidates: [50, {c.id}]")
        self.put_page("invest", "llm-moat", old)
        # ① 编译链：老页已含本轮候选 → 幂等跳过，不出草稿、不报错、不调模型
        with patch_call([]) as s1, \
                patch("core.wiki_loop._call", return_value=self.POLISH) as polish:
            res = ingest.compile_candidates()
        self.assertTrue(res["ok"], res.get("err"))
        self.assertEqual(res["drafts"], [])
        self.assertFalse(self.draft_path("llm-moat").exists())
        self.assertEqual(s1.call_count, 0, "候选已编译，连 S1 都不该调")
        self.assertEqual(polish.call_count, 0, "没有草稿就不该花钱重编结论")
        # ② `_compose_page` 的程序 noop 判据：候选是老页候选的子集 → 返回空 text
        ofm, _ = wstore.parse_front(old)
        old_page = {"slug": "llm-moat", "domain": "invest", "fm": ofm,
                    "body": "", "draft": False, "title": "LLM 护城河在验证器",
                    "page": "wiki/invest/llm-moat.md"}
        group = {"title": "同义", "domain": "invest", "cand_ids": [c.id],
                 "slug": "llm-moat", "tags": []}
        text, slug, dropped, err = ingest._compose_page(
            group, {c.id: c}, "", old_page)
        self.assertEqual((text, slug, err), ("", "", ""),
                         "候选集合是老页候选子集 → noop，不出草稿也不算失败")
        self.assertEqual(len(dropped), 1, "noop 要留一条「没有新增」的说明")

    def test_conservation_guard_fires_when_renderer_drops_lines(self):
        """★ 守卫自己会响（形态 A 换守卫）：老正文逐行守恒已退役，并入守恒改由
        「候选集合只增不减」程序守卫守。`_check_links` 只校验越界引用（引用不
        存在的页），不再强制保留老引用——正文应自然流畅，链接只在相关时出现。"""
        pages = [{"slug": "other", "title": "别的页"}]
        old_links = ["other"]
        self.assertEqual(
            wl._check_links("结论关联 [[other]]。", pages, old_links), (True, ""),
            "保留老引用、不越界 → 放行")
        ok, why = wl._check_links("结论里没再提别的页。", pages, old_links)
        self.assertTrue(ok, "丢老引用不再拒写——链接只在真正相关时出现")
        ok2, why2 = wl._check_links("结论关联 [[no-such-page]]。", pages, old_links)
        self.assertFalse(ok2, "引用可引用清单里没有的页必须拒写")
        self.assertIn("不存在", why2)

    @patch("core.wiki.approval_active", return_value=False)
    def test_second_merge_goes_into_the_pending_draft(self, _no_pause):
        """同一页的第二次并入不再被拒：并进**待审批的那份草稿**，一次审批就好。
        形态 A：候选只增不减、merges 原样保留；正文每轮都是纯 polish 一段，
        不再把上一轮并进去的候选清单累进正文。两轮都打桩、都验桩真被用上。"""
        c1 = mk_cand("第一条", "第一条")
        res1, _, p1 = self._compile(group_json_target([c1.id], "existing:llm-moat"))
        self.assertTrue(res1["ok"], res1.get("err"))
        self.assertGreater(p1.call_count, 0, "第一轮结论重编桩真被用上")
        c2 = mk_cand("第二条", "第二条")
        res, _, p2 = self._compile(group_json_target([c2.id], "existing:llm-moat"))
        self.assertTrue(res["ok"], res.get("err"))
        self.assertGreater(p2.call_count, 0, "第二轮结论重编桩真被用上")
        self.assertEqual(res["drafts"], ["llm-moat"], "还是同一份草稿，不是第二份")
        fm, body = wstore.parse_front(self.draft_text("llm-moat"))
        self.assertEqual(fm["merges"], "llm-moat", "merges 原样保留")
        self.assertEqual([int(x) for x in fm["candidates"]],
                         [OLD_ID, c1.id, c2.id], "候选只增不减")
        self.assertEqual(body.strip(), "这是一段结论。", "正文是纯 polish 一段")
        self.assertNotIn("第一条", body, "上一轮的候选明细不再累进正文")
        self.assertNotIn("第二条", body)
        self.assertNotIn(wstore.BODY_HEAD, body)
        self.assertNotIn(wstore.POLISH_HEAD, body)

    @patch("core.wiki.approval_active", return_value=False)
    def test_new_page_draft_is_also_a_merge_target(self, _no_pause):
        """待审批的**新页**草稿也在目录里：并进它，而不是再造一份。
        形态 A：仍是新页（不写 merges）、候选只增不减、正文纯 polish 一段。"""
        self.draft_path("moat-notes").write_text(
            "---\ntitle: 护城河札记\ndomain: invest\nsources: [entry:6810]\n"
            "updated: 2026-11-03\nconfidence: medium\ncandidates: [88]\n---\n\n"
            "## 我的判断\n- 老草稿里的判断 [文章《大模型的护城河》](/entry/6810?src=wiki)\n\n"
            "## 事实 / 他人的观点\n- （无）\n\n## 我的在意点\n- （无）\n",
            encoding="utf-8")
        c = mk_cand("补一条新的", "补一条新的")
        res, _, polish = self._compile(
            group_json_target([c.id], "existing:moat-notes"))
        self.assertTrue(res["ok"], res.get("err"))
        self.assertGreater(polish.call_count, 0, "结论重编桩真被用上")
        self.assertEqual(res["drafts"], ["moat-notes"], "并进草稿，不开第二份")
        fm, body = wstore.parse_front(self.draft_text("moat-notes"))
        self.assertNotIn("merges", fm,
                         "新页草稿并完还是新页；写成 merges 会让 approve 找不存在的覆盖对象")
        self.assertEqual([int(x) for x in fm["candidates"]], [88, c.id], "候选只增不减")
        self.assertEqual(body.strip(), "这是一段结论。", "正文是纯 polish 一段")
        self.assertNotIn("老草稿里的判断", body, "老草稿正文不再搬运")
        self.assertNotIn(wstore.BODY_HEAD, body)
        self.assertNotIn(wstore.POLISH_HEAD, body)

    @patch("core.wiki.approval_active", return_value=False)
    def test_s1_sees_draft_pages_in_catalog(self, _no_pause):
        """S1 的页目录必须包含待审批草稿（draft: true）—— 否则它只会新建。

        ★ 用户问的就是这一条：「编译的时候……会看草稿的页面吗」。2026-11-04 之前
          的答案是「不看，而且并入会被拒」，同一个概念能攒出一沓草稿。
        ★ 形态 A：结论重编桩在 `wiki_loop._call`（被调用点），不真联网。
        """
        self.draft_path("moat-notes").write_text(
            "---\ntitle: 护城河札记\ndomain: invest\nsources: [entry:6810]\n"
            "updated: 2026-11-03\nconfidence: medium\ncandidates: [88]\n---\n\n"
            "## 我的判断\n- 老草稿里的判断\n", encoding="utf-8")
        c = mk_cand("补一条新的", "补一条新的")
        seen = {}

        def fake(_kind, _sys, user, _stats, **kw):
            if kw.get("tag") == "wiki 编译分组":
                seen.update(json.loads(user))
                return group_json_target([c.id], "new"), ""
            # 形态 A：polish 走 wiki_loop._call 桩，不该再到 citeguard 出口
            return (merge_reply([{"cand_id": c.id, "section": "judge",
                                  "type": "judgment", "text": "补一条新的"}]), "")

        with patch("core.citeguard._call", side_effect=fake), \
                patch("core.wiki_loop._call",
                      return_value=("这是一段结论。", [], None)):
            ingest.compile_candidates()
        pages = {p["slug"]: p for p in seen.get("pages") or []}
        self.assertIn("moat-notes", pages,
                      "待审批草稿不在 S1 目录里 → 同一概念必然再建一页")
        self.assertTrue(pages["moat-notes"].get("draft"))


class ApproveMergeTest(MergeBase):
    """审批 = 覆盖一页已入库的知识。必须备份、必须只增、必须说清拒绝理由。"""

    def _draft(self, candidates=(OLD_ID, 999), merges="llm-moat", domain="invest",
               body="## 我的判断\n- 新说法\n"):
        return wstore.root() / ".drafts" / "llm-moat.md"

    def _write_draft(self, candidates, body, merges="llm-moat", domain="invest",
                     slug="llm-moat"):
        self.draft_path(slug).write_text(
            "---\ntitle: LLM 护城河在验证器\ndomain: %s\nsources: [entry:6810]\n"
            "updated: %s\nconfidence: medium\ntags: []\ncandidates: [%s]\nmerges: %s\n---\n\n%s"
            % (domain, timezone.localdate().isoformat(),
               ", ".join(str(i) for i in candidates), merges, body),
            encoding="utf-8")

    def test_approve_overwrites_with_backup(self):
        self._write_draft([OLD_ID, 999],
                          "## 我的判断\n- 老判断还在\n- 新判断加进来\n\n"
                          "## 事实 / 他人的观点\n- （无）\n\n## 我的在意点\n- （无）\n")
        ok, payload = wstore.approve("llm-moat")
        self.assertTrue(ok, payload)
        self.assertTrue(payload.get("merged"))
        text = self.page_text("invest", "llm-moat")
        self.assertIn("新判断加进来", text)
        self.assertNotIn("merges:", text, "merges 只对这次合并有意义，不该带进入库页")
        self.assertFalse(self.draft_path("llm-moat").exists(), "草稿要移走，不能留一份")
        bak = list((wstore.root() / wstore.MERGED_DIR).glob("llm-moat-*.md"))
        self.assertEqual(len(bak), 1, "旧版必须留一份")
        self.assertIn("护城河在验证器", bak[0].read_text(encoding="utf-8"))
        acts = [e["action"] for e in wstore.read_log(limit=20)]
        self.assertIn("merge", acts)

    def test_approve_refuses_when_old_candidate_lost(self):
        """并入只能加不能减：少了老候选就是「安静地删掉一条判断」。"""
        self._write_draft([999], "## 我的判断\n- 只剩新的\n")
        ok, why = wstore.approve("llm-moat")
        self.assertFalse(ok)
        self.assertIn("丢掉原有候选", why)
        self.assertIn("护城河在验证器", self.page_text("invest", "llm-moat"),
                      "被拒时目标页一个字都不该动")

    def test_approve_refuses_missing_target(self):
        self._write_draft([OLD_ID, 999], "## 我的判断\n- x\n", merges="no-such-page",
                          slug="dangling")
        ok, why = wstore.approve("dangling")
        self.assertFalse(ok)
        self.assertIn("不存在", why)
        self.assertTrue(self.draft_path("dangling").exists(), "被拒时草稿要留着")

    def test_approve_refuses_cross_domain(self):
        self._write_draft([OLD_ID, 999], "## 我的判断\n- x\n", domain="tech")
        ok, why = wstore.approve("llm-moat")
        self.assertFalse(ok)
        self.assertIn("不跨域", why)


class BodyDiffTest(MergeBase):
    def test_marks_insert_delete_and_folds_equal(self):
        old = "\n".join("行%d" % i for i in range(1, 13))
        new = old.replace("行6", "行6-改") + "\n行13"
        diff = wstore.body_diff(old, new)
        kinds = [x["kind"] for x in diff]
        self.assertIn("del", kinds)
        self.assertIn("ins", kinds)
        self.assertIn("gap", kinds, "相同的长段要折叠，否则改动淹没在整页里")
        self.assertIn("行6", [x["text"] for x in diff if x["kind"] == "del"])
        self.assertIn("行6-改", [x["text"] for x in diff if x["kind"] == "ins"])
        # 折叠只是显示，不能把没变的行算成变动
        self.assertNotIn("行3", [x["text"] for x in diff])


class MergeViewTest(MergeBase):
    """审批浮层：必须看得出「这是覆盖」，且能当场看到动了什么。

    ★ 2026-11-09 起断言对象是浮层工作区（/wiki/approval/draft）：首页草稿卡
      已下线，merge 警示 + diff 搬进了浮层 —— 「覆盖一页人的知识必须给 diff」
      这条承诺不随 UI 搬家而消失。
    """

    def setUp(self):
        super().setUp()
        self.draft_path("llm-moat").write_text(
            "---\ntitle: LLM 护城河在验证器\ndomain: invest\nsources: [entry:6810]\n"
            "updated: %s\nconfidence: medium\ncandidates: [50, 999]\n"
            "merges: llm-moat\n---\n\n"
            "## 我的判断\n- LLM 基座难差异化，护城河在验证器 [x](/entry/6810)\n"
            "- 新加的一条\n\n## 事实 / 他人的观点\n- （无）\n\n## 我的在意点\n- （无）\n"
            % timezone.localdate().isoformat(), encoding="utf-8")

    def test_card_says_merge_and_shows_diff(self):
        html = self.client.get("/wiki/approval/draft?slug=llm-moat").content.decode()
        self.assertIn("改写", html)
        self.assertIn("提交入库后覆盖它", html)
        self.assertIn("看改动", html)
        self.assertIn("新加的一条", html, "diff 里要看得到新增的那行")

    def test_normal_draft_still_says_approve(self):
        self.draft_path("llm-moat").unlink()
        self.draft_path("plain-note").write_text(
            "---\ntitle: 普通新页\ndomain: invest\nsources: [entry:1]\n"
            "updated: %s\nconfidence: medium\ncandidates: [7]\n---\n\n"
            "## 我的判断\n- x\n" % timezone.localdate().isoformat(), encoding="utf-8")
        html = self.client.get("/wiki/approval/draft?slug=plain-note").content.decode()
        self.assertNotIn("改写", html)
        self.assertNotIn("merge-bar", html)
