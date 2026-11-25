"""编译（阶段 2.3a，形态 A，2026-11-08）：accepted 候选 → `.drafts/<slug>.md`。

这一组盯的仍是**编译会不会替人编观点**。形态 A 之后保守路线的含义变成：

  正文 = 纯 polish 一段结论（不含三块标题 / `## 候选清单` / `## 通顺话` 双层）；
  frontmatter `candidates` 是分组唯一真值，精确出处由详情页折叠溯源区读
  `IngestCandidate` 结构化字段（`wa.trace_rows`）负责；
  编译链 S2 不再调 LLM，`_compose_page` 只产 frontmatter + 空正文 + `stale: true`，
  由 `wiki_loop._polish_one` 统一写结论。

所以这里的断言分两种：
  1. **正向**：合法输入 → frontmatter `candidates` 正确 + 正文 == 桩返回的结论 +
     不含任何结构标题；精确出处改断言 `wa.trace_rows(ids)` 的分组与 link；
  2. **反证**：喂一条越界输入（结论引用不存在的页 / 丢老引用 / 编一个素材里没有的
     数字 / 素材 `#id` 与 frontmatter 对不上 / 空正文硬入库），新守卫必须拒写、
     拒入库 —— 「守卫自己会响」比「守卫存在」重要
     （守卫写死期望值 / 从不触发的守卫，等于没有守卫）。
     旧的三块分类、人称、hint、facts 守卫整套退役，下面每条都改写成
     `_check_links` / `_check_numbers` / `set_polish`(⚠️K) / `_validate_draft`
     的反证，不许因为旧守卫消失就删掉。

★ 打桩纪律（形态 A，2026-11-08）：S2 退役后只剩两处花钱点：
  - S1 分组走 `core.citeguard._call`（共享桩 `patch_call`，replies **只留 S1**）；
  - 结论重编走 `core.wiki_loop._call`（**被调用点**），本文件用 `patch_compile`
    叠加，返回 `("这是一段结论。", [], None)`。
  每条走到编译的用例都要断言桩真被用上：草稿正文真的出现这段结论 /
  polish mock `call_count > 0`；S1 桩的 `call_count` 改成实际消费次数
  （单批 = 1，不再是旧的 2）。绝不许真联网。
"""
import json
import shutil
import tempfile
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from core import ingest
from core import wiki as wstore
from core import wiki_agent as wa
from core import wiki_loop as wloop
from core.models import Highlight, IngestCandidate


def group_json(pairs, title="十年后思维", domain="invest"):
    return json.dumps({"groups": [{"title": title, "domain": domain,
                                   "cand_ids": list(pairs), "reason": "同一概念"}]},
                      ensure_ascii=False)


def page_json(lines, slug="ten-year-thinking", title="十年后思维", facts=None, hint=""):
    return json.dumps({"slug": slug, "title": title, "lines": lines,
                       "facts": facts or [], "hint": hint}, ensure_ascii=False)


def mk_cand(quote, claim, *, quote_from="mine", domain="invest",
            confidence="high", sid="daily-2026-10-31", status="accepted"):
    return IngestCandidate.objects.create(
        batch="b1", source_type="report", source_id=sid, kind="decision",
        claim=claim, quote=quote, quote_from=quote_from,
        domain=domain, confidence=confidence, status=status)


#: 形态 A：正文只留这段结论。所有「桩真被用上」反证都拿它比对。
POLISH_TEXT = "这是一段结论。"
POLISH_REPLY = (POLISH_TEXT, [], None)

#: 三块 / 双层 / 清单的标题一律不许再出现在正文里。
_NO_HEADS = ("## 候选清单", "## 通顺话", "## 我的判断",
             "## 事实 / 他人的观点", "## 我的在意点", "## 可能的联系")


def assert_polish_body(testcase, slug):
    """正文 == 桩结论，且不含三块 / 双层 / 清单标题（形态 A 唯一口径）。

    ★ 放在模块级而不是 `CompileBase` 上：那个类被 5 个测试文件 import，
      本文件私有的断言 helper 不该动共享类。
    """
    fm, body = wstore.parse_front(testcase.draft_text(slug))
    testcase.assertEqual(body.strip(), POLISH_TEXT)
    for head in _NO_HEADS:
        testcase.assertNotIn(head, body)
    return fm, body


def s1_group(pairs, *, slug="ten-year-thinking", title="十年后思维",
             domain="invest", tags=None):
    """形态 A 的 S1 分组桩：groups 必须带 `slug`(ASCII kebab) 与 `tags`。

    ★ 不改共享的 `group_json`（它被 test_compile_running.py 复用），只在
      本文件内补全字段。少了 slug，`_compose_page` 会 fallback 到
      `<domain>-<minid>`，原来按 `ten-year-thinking.md` 找草稿就会 FileNotFoundError。
    """
    data = json.loads(group_json(pairs, title=title, domain=domain))
    g = data["groups"][0]
    g["slug"] = slug
    g["tags"] = list(tags if tags is not None else ["长期", "现金流"])
    return json.dumps(data, ensure_ascii=False)


@contextmanager
def patch_compile(replies, *, polish=POLISH_REPLY):
    """形态 A 双桩：S1(`core.citeguard._call`) + 结论重编(`core.wiki_loop._call`)。

    ★ 不改共享的 `patch_call`（它被 test_compile_merge.py 复用）。S2 退役后
      replies 只需留 S1；polish 单独打在**被调用点** `core.wiki_loop._call`，
      这样 `patch_call` 的 replies 不会被 polish 误消费成结论文本。
    """
    with ExitStack() as stack:
        call = stack.enter_context(patch_call(replies))
        pol = stack.enter_context(
            patch("core.wiki_loop._call", return_value=polish))
        yield SimpleNamespace(s1=call, polish=pol)


class CompileBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-compile-")
        cls.ov = override_settings(WIKI_DIR=cls.tmp)
        cls.ov.enable()

    @classmethod
    def tearDownClass(cls):
        cls.ov.disable()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        ingest.title_of.cache_clear()
        # ★ 每个用例都要一个**空的** wiki 目录：草稿是磁盘文件，不会随事务回滚。
        #   不清的话上一个用例写的 `.drafts/*.md` 还在，`_compiled_ids()` 会认为
        #   候选「已经编译过」而整批跳过 —— 症状是守卫用例全部「ok=True」地假绿
        #   （第一次跑就踩到了：4 个守卫断言全反）。
        shutil.rmtree(self.tmp, ignore_errors=True)
        wstore.ensure_dirs()

    def draft_text(self, slug):
        return (wstore.root() / ".drafts" / f"{slug}.md").read_text(encoding="utf-8")


class CompileHappyTest(CompileBase):
    """正向：合法输入产出结构、出处、结论都对。"""

    def test_writes_draft_with_three_sections_and_sources(self):
        """形态 A：三块/清单/深链不再写进正文；分组真值在 frontmatter candidates，
        精确出处交给折叠溯源区（`wa.trace_rows`）。"""
        a = mk_cand("价值投资看的是十年后的现金流", "价值投资看长期现金流")
        b = mk_cand("十年后无法精确预测，只能毛估估", "长期无法精确预测")
        with patch_compile([s1_group([a.id, b.id])]) as call:
            res = ingest.compile_candidates()
        self.assertTrue(res["ok"], res.get("err"))
        self.assertEqual(len(res["drafts"]), 1)
        self.assertEqual(call.s1.call_count, 1, "S1 一次分组（旧桩的 2 已退役）")
        self.assertGreater(call.polish.call_count, 0,
                           "结论重编桩没被用上就等于真联网了")
        self.assertEqual(res.get("polished"), ["ten-year-thinking"])

        text = self.draft_text("ten-year-thinking")
        fm, body = wstore.parse_front(text)
        self.assertEqual(fm["domain"], "invest")
        self.assertEqual(sorted(fm["sources"]), ["report:daily-2026-10-31"])
        self.assertEqual(fm["confidence"], "high")
        self.assertEqual(fm["updated"], timezone.localdate().isoformat())
        self.assertEqual(sorted(fm["candidates"]), sorted([str(a.id), str(b.id)]))
        self.assertEqual(fm.get("tags"), ["长期", "现金流"], "S1 建议的标签要写入")
        self.assertNotIn(wstore.STALE_FM, fm, "set_polish 成功后 stale 要剥掉")
        # 正文 = 纯结论一段，不含任何结构标题，也不含出处深链
        self.assertEqual(body.strip(), POLISH_TEXT)
        for head in _NO_HEADS:
            self.assertNotIn(head, body)
        self.assertNotIn("/summary/2026-10-31", body)
        # 精确出处由折叠溯源区读结构化字段负责
        trace = wa.trace_rows([a.id, b.id])
        self.assertEqual(trace["total"], 2)
        self.assertEqual([g["key"] for g in trace["groups"]], ["mine"])
        row = trace["groups"][0]["rows"][0]
        self.assertIn("/summary/2026-10-31", row["link"])

    def test_highlight_candidate_goes_to_material_and_gets_mk_link(self):
        """划出的原文 → 溯源区分组 highlight；深链 `?mk=` 也由溯源区给（D3）。"""
        h = Highlight.objects.create(source_type="report", source_id="daily-2026-10-31",
                                     quoted_text="产能至 2029 年已售罄", note="")
        c = mk_cand("产能至 2029 年已售罄", "某公司产能售罄", quote_from="highlight")
        with patch_compile([s1_group([c.id])]) as call:
            res = ingest.compile_candidates()
        self.assertTrue(res["ok"], res.get("err"))
        self.assertGreater(call.polish.call_count, 0, "结论重编桩没被用上")
        assert_polish_body(self, "ten-year-thinking")
        # 「他人观点（但斌）」这类正文标签退役；分组与深链改看 trace_rows
        trace = wa.trace_rows([c.id])
        self.assertEqual([g["key"] for g in trace["groups"]], ["highlight"])
        self.assertEqual(trace["groups"][0]["label"], "素材 / 我在意")
        row = trace["groups"][0]["rows"][0]
        self.assertIn(f"mk={h.id}", row["link"], "划出的原文必须落到那一条划线")
        self.assertEqual(row["source"], "report:daily-2026-10-31")

    @patch("core.wiki.approval_active", return_value=False)
    def test_second_run_skips_already_compiled(self, _no_pause):
        """幂等：编译过的不再编译（否则连点两次 = 两倍钱 + 一堆同名草稿）。

        ★ 第一次编译会留下草稿；2026-11-09 用户澄清恢复审批暂停（有草稿时拒绝
          新编译），所以这里显式 patch 掉 approval_active 以绕过暂停闸，专测
          `_compiled_ids()` 的幂等本身。
        """
        a = mk_cand("价值投资看的是十年后的现金流", "价值投资看长期现金流")
        with patch_compile([s1_group([a.id])]) as call:
            first = ingest.compile_candidates()
        self.assertTrue(first["ok"], first.get("err"))
        self.assertGreater(call.polish.call_count, 0, "第一次编译的结论重编桩没被用上")
        # 第二次：候选都已在草稿的 candidates: 里 ⇒ 不该再调模型
        with patch_compile([]) as call:
            res = ingest.compile_candidates()
        self.assertTrue(res["ok"])
        self.assertEqual(res["drafts"], [])
        self.assertEqual(call.s1.call_count, 0, "已编译过就不该再花钱")
        self.assertIn("已经编译过", res.get("msg") or "")

    def test_slug_is_ascii_and_collision_gets_suffix(self):
        """slug 现在来自 S1 分组的 `slug` 字段，仍必须 ASCII。"""
        a = mk_cand("价值投资看的是十年后的现金流", "看长期现金流")
        with patch_compile([s1_group([a.id], slug="中文标题!!")]):
            res = ingest.compile_candidates()
        slug = res["drafts"][0]
        self.assertRegex(slug, r"^[a-z0-9-]+$", "slug 必须 ASCII（中文 slug 会让 URL/导出变麻烦）")


class CompileIdempotencyTest(CompileBase):
    """幂等的真正边界：**草稿入库之后**，同一批候选不能再编译一次。

    ★ 2026-11-02 实际踩到：`wstore.approve()` 只是把文件搬进 `wiki/<domain>/`，
      候选在库里仍是 `accepted`，而「已编译」的唯一证据是页 frontmatter 的
      `candidates:`。判据只扫 `.drafts/` ⇒ 页一入库，这批候选就被判「没编译过」：
      再点编译重跑一整轮（真花钱），并产出与已入库页一模一样的草稿。
      那一轮测试全绿也发现不了它 —— 每条断言都在单次编译内部成立。
    """

    def _compile(self, *cands, slug="ten-year-thinking"):
        """S1 分组桩带 slug（撞名改名靠它），polish 桩让正文非空好入库。"""
        with patch_compile([s1_group([c.id for c in cands], slug=slug)]) as call:
            res = ingest.compile_candidates()
        self.assertTrue(res["ok"], res.get("err"))
        self.assertGreater(call.polish.call_count, 0, "结论重编桩没被用上")
        return res

    def test_compiled_ids_counts_both_drafts_and_approved_pages(self):
        a = mk_cand("价值投资看的是十年后的现金流", "价值投资看长期现金流")
        slug = self._compile(a)["drafts"][0]
        self.assertIn(a.id, ingest._compiled_ids(), "草稿里的 candidates 没被认")
        ok, payload = wstore.approve(slug)
        self.assertTrue(ok, payload)
        self.assertIn(a.id, ingest._compiled_ids(),
                      "已入库页的 candidates 没被认 —— 正是这次修的缺陷")

    def test_approved_page_blocks_recompile(self):
        a = mk_cand("价值投资看的是十年后的现金流", "价值投资看长期现金流")
        slug = self._compile(a)["drafts"][0]
        ok, payload = wstore.approve(slug)
        self.assertTrue(ok, payload)
        with patch_compile([]) as call:
            res = ingest.compile_candidates()
        self.assertTrue(res["ok"], res.get("err"))
        self.assertEqual(res["drafts"], [], "入库之后重编又出了一份草稿")
        self.assertEqual(res["skipped"], 1)
        self.assertEqual(call.s1.call_count, 0, "入库后再点编译不该再花钱")

    def test_discarded_draft_needs_rejudge_to_recompile(self):
        """退稿 = 内容级否定：候选一并废弃、不自动重编；改判回待审才能再编。

        ★ 2026-11-13（晚）语义变更：旧契约是「退稿后回池必重编」，实测退稿
        在下一轮编译原地复活（同名 / 换名都有），用户拍板改为连带废弃。
        归档仍不算「已编译」—— 否则改判后的候选永远编不出来（永久占位）。
        """
        a = mk_cand("价值投资看的是十年后的现金流", "价值投资看长期现金流")
        slug = self._compile(a)["drafts"][0]
        ok, _ = wstore.discard(slug, "不要这内容")
        self.assertTrue(ok)
        a.refresh_from_db()
        self.assertEqual(a.status, "discarded")
        self.assertNotIn(a.id, ingest._compiled_ids(),
                         "退稿的草稿被算成已编译了：改判后的候选再也编不出来")
        with patch_compile([]) as call:
            res = ingest.compile_candidates()
        self.assertFalse(res["ok"], "候选全废弃后没有可编的，应明说而不是空跑")
        self.assertIn("改判", res.get("err", ""), "失败文案要指出反悔路径")
        self.assertEqual(call.s1.call_count, 0, "废弃候选不该再花钱")
        self.assertTrue(ingest.set_status(a.id, "pending")[0], "改判回待审")
        with patch_compile([s1_group([a.id])]) as call:
            res = ingest.compile_candidates()
        self.assertTrue(res["ok"], res.get("err"))
        self.assertEqual(res["drafts"], [slug], "改判后重编应拿回原名（归档不占名）")
        self.assertEqual(call.s1.call_count, 1, "改判后重编必须真的重新分组")

    def test_slug_taken_by_live_page_is_renamed_and_reported(self):
        """已入库页占着名字时，新草稿要改名**并说出来**，且必须还能入库。

        同名页存在而草稿也叫这个名字，`approve()` 会以「已存在同名页」拒收 ——
        那一轮编译的钱白花，人还卡在一份进不去的草稿上。
        ★ 形态 A：撞名用的 slug 现在写在 S1 分组桩里（旧的 `page_json(slug=...)`
          是 S2 回复，S2 退役后不再被消费，写了也不生效）。
        """
        a = mk_cand("价值投资看的是十年后的现金流", "价值投资看长期现金流")
        slug = self._compile(a)["drafts"][0]
        ok, payload = wstore.approve(slug)
        self.assertTrue(ok, payload)

        b = mk_cand("十年后无法精确预测，只能毛估估", "长期只能毛估估")
        res = self._compile(b, slug=slug)
        self.assertEqual(res["drafts"], [slug + "-2"])
        self.assertEqual([c["used"] for c in res.get("collisions") or []],
                         [slug + "-2"], "撞名改名这件事没报给人（静默加后缀）")
        ok, payload = wstore.approve(res["drafts"][0])
        self.assertTrue(ok, payload)


class CompileGuardTest(CompileBase):
    """反证：越界输入必须被新守卫拒写/拒入库，而且要说得出为什么。

    ★ 形态 A：旧的三块分类 / 人称 / hint / facts / 编数守卫整套退役。这里逐条
      改写成 `_check_links`（引用守恒）、`_check_numbers`（结论数字）、
      `set_polish`（⚠️K 素材 #id 一致）、`_validate_draft`（空正文拒入库）
      的反证，每条都配一条「守卫不是永远响」的正证。
    """

    def test_highlight_cannot_be_written_as_my_judgment(self):
        """★ 原 G1/G3 分类守卫 → `_check_links` 越界引用反证。"""
        c = mk_cand("产能至 2029 年已售罄", "某公司产能售罄", quote_from="highlight")
        bad = "这段结论引用了 [[根本不存在的页]]。"
        with patch_compile([s1_group([c.id])], polish=(bad, [], None)):
            res = ingest.compile_candidates()
        self.assertTrue(res["ok"], "编译本身成功，只是结论重编被拒：%r" % res.get("err"))
        self.assertEqual(len(res.get("polish_failed") or []), 1,
                         "越界引用必须让结论重编失败并留痕")
        self.assertIn("不存在的页", res["polish_failed"][0]["why"])
        slug = "ten-year-thinking"
        fm, body = wstore.parse_front(self.draft_text(slug))
        self.assertEqual(body.strip(), "", "被拒的结论不许写进正文")
        self.assertEqual(fm.get(wstore.STALE_FM), "true", "polish 失败 stale 要留住")
        self.assertTrue(wa.read("draft", slug)["body_stale"])
        ok, why = wstore.approve(slug)
        self.assertFalse(ok, "空正文 + 挂着候选的页不许入库")
        self.assertIn("结论还没生成", why)
        # 守卫不是永远响：合法结论能放行
        ok2, why2 = wloop._check_links(POLISH_TEXT, [], [])
        self.assertTrue(ok2, why2)

    def test_mine_cannot_be_downgraded_to_material(self):
        """★ 原「我敲的字不能降级成素材」→ `_check_links` 越界引用反证。
        丢老引用不再拒写（链接只在相关时出现），但引用不存在的页必须拒写。
        """
        pages = [{"slug": "cash-flow", "title": "现金流"}]
        old_links = ["cash-flow"]
        ok, why = wloop._check_links("这段结论引用了 [[no-such-page]]。", pages, old_links)
        self.assertFalse(ok, "引用不存在的页必须拒写")
        self.assertIn("不存在", why)
        # 丢老引用不再拒写
        ok2, why2 = wloop._check_links("这段结论把老引用弄丢了。", pages, old_links)
        self.assertTrue(ok2, "丢老引用不再拒写——链接只在真正相关时出现")

    def test_first_person_inside_material_block_is_dropped(self):
        """★ 原 G2 人称守卫 → ⚠️K：`set_polish` 素材 `#id` 与 frontmatter 一致守卫。

        故意让 material 少一条 / 多一条 → 必须返回 (False, ...) 并变红；空集合
        不许 `[] == []` 记通过。
        """
        a = mk_cand("价值投资看的是十年后的现金流", "价值投资看长期现金流")
        b = mk_cand("十年后无法精确预测，只能毛估估", "长期无法精确预测")
        with patch_compile([s1_group([a.id, b.id])]):
            res = ingest.compile_candidates()
        self.assertTrue(res["ok"], res.get("err"))
        slug = "ten-year-thinking"
        fm = wstore.parse_front(self.draft_text(slug))[0]
        declared = wstore._cand_ids(fm)
        self.assertEqual(sorted(declared), sorted([a.id, b.id]))
        details = {i: {"claim": "理解", "quote": "原话", "source": "report:s",
                       "quote_from": "mine"} for i in declared}
        good = wstore.candidate_list_text(declared, details)
        # 正证：一致的素材放行，且成功才剥 stale（守卫不是永远响）
        ok, why = wstore.set_polish(slug, POLISH_TEXT, good)
        self.assertTrue(ok, why)
        self.assertNotIn(wstore.STALE_FM,
                         wstore.parse_front(self.draft_text(slug))[0],
                         "set_polish 成功才剥 stale")
        # 反证 1：素材少一条 #id → 拒写
        few = wstore.candidate_list_text(declared[:1], details)
        ok2, why2 = wstore.set_polish(slug, "一段别的结论", few)
        self.assertFalse(ok2, "素材少一条候选必须拒写")
        self.assertIn("对不上", why2)
        # 反证 2：素材多一条幽灵 #id → 拒写
        many = good + "\n- `#999999` （多出来的幽灵候选）"
        ok3, why3 = wstore.set_polish(slug, "又一段结论", many)
        self.assertFalse(ok3, "素材多一条候选必须拒写")
        self.assertIn("对不上", why3)

    def test_invented_number_rejects_page(self):
        """★ G4 数字守卫搬到结论正文（`wiki_loop._check_numbers`）。

        ★ 仍用**三位数**举例：`number_audit` 只核 ≥3 位或带小数的数字（两位数是
          模型自己加总的重灾区，逐个核会全是假警报）。这是它既定的判据。
        """
        a = mk_cand("价值投资看的是十年后的现金流", "看长期现金流")
        bad = "这套方法让胜率提升到 187%。"
        with patch_compile([s1_group([a.id])], polish=(bad, [], None)):
            res = ingest.compile_candidates()
        self.assertTrue(res["ok"], res.get("err"))
        self.assertEqual(len(res.get("polish_failed") or []), 1,
                         "编数必须让结论重编失败")
        self.assertIn("187", res["polish_failed"][0]["why"])
        fm, body = wstore.parse_front(self.draft_text("ten-year-thinking"))
        self.assertEqual(body.strip(), "", "被拒的结论不许写进正文")
        self.assertEqual(fm.get(wstore.STALE_FM), "true")
        # 守卫不是永远响：无数字的合法结论放行
        material = wstore.candidate_list_text([a.id], {
            a.id: {"claim": a.claim, "quote": a.quote,
                   "source": "report:daily-2026-10-31", "quote_from": "mine"}})
        ok, why = wloop._check_numbers(POLISH_TEXT, material)
        self.assertTrue(ok, why)

    def test_check_numbers_accepts_renamed_page_slug(self):
        """★ 2026-11-22：清单里的 slug 带时间戳，照抄不能被数字守卫拒写。

        `_check_links` 要求逐字照抄清单，数字守卫若把时间戳当编造数字，
        两道守卫互锁（旧 slug 被链接守卫拒、正确 slug 被数字守卫拒）。
        """
        material = "候选明细：腾讯 AI 的软肋是入口不足"
        ok, why = wloop._check_numbers(
            "参见 [[zhong-mei-mo-xing-cha-ju-1794620890]]。", material)
        self.assertTrue(ok, why)
        ok, why = wloop._check_numbers("参见 1794620890。", material)
        self.assertFalse(ok, "裸写在正文里的编造数字仍要拒")
        self.assertIn("1794620890", why)

    def test_hint_with_conclusion_is_dropped_but_page_kept(self):
        """★ 原 G5 提示守卫 → 正文形态守卫：S2 不再产出 hint，正文只有纯结论一段，
        越界的提示语不进正文，页仍成立且能入库。"""
        a = mk_cand("价值投资看的是十年后的现金流", "看长期现金流")
        with patch_compile([s1_group([a.id])]) as call:
            res = ingest.compile_candidates()
        self.assertTrue(res["ok"], res.get("err"))
        self.assertGreater(call.polish.call_count, 0, "结论重编桩没被用上")
        self.assertEqual(res.get("polished"), ["ten-year-thinking"])
        self.assertEqual(res.get("polish_failed"), [], "合法结论不该被拒")
        slug = res["drafts"][0]
        fm, body = assert_polish_body(self, slug)
        self.assertNotIn("立刻加仓", body)
        self.assertNotIn(wstore.STALE_FM, fm, "set_polish 成功后 stale 被剥")
        self.assertFalse(wa.read("draft", slug)["body_stale"])
        ok, why = wstore.approve(slug)
        self.assertTrue(ok, why)

    def test_bad_cand_id_from_s1_is_rejected(self):
        """S1 给出不存在的 cand_id / 非法 domain 的分组，一律不采纳。"""
        a = mk_cand("价值投资看的是十年后的现金流", "看长期现金流")
        bad = json.dumps({"groups": [
            {"title": "X", "domain": "unknown-domain", "cand_ids": [a.id]},
            {"title": "Y", "domain": "invest", "cand_ids": [999999]},
        ]})
        with patch_compile([bad]) as call:
            res = ingest.compile_candidates()
        self.assertFalse(res["ok"])
        self.assertIn("不合法", res["err"])
        self.assertEqual(call.s1.call_count, 1, "分组全不合法就不该继续生成页")

    def test_no_group_json_is_an_error_not_zero_pages(self):
        """★ 「输出不合格」必须跟「确实没内容」分开，否则静默返回 0 页看着像成功。"""
        a = mk_cand("价值投资看的是十年后的现金流", "看长期现金流")
        with patch_compile(["这是一段散文，不是 JSON。"]):
            res = ingest.compile_candidates()
        self.assertFalse(res["ok"])
        self.assertIn("没找到 groups", res["err"])

    def test_only_discarded_candidates_never_call_model(self):
        """★ 候选免审后「没活」= **没有未丢弃的候选**，不再是「没采纳」。

        花钱按钮的空跑守卫要留住：全是 discarded 时必须早退，一次模型调用
        都不发。pending 现在会正常进编译，另见 test_candidate_autoapprove 里
        「打桩 plan_groups 证明 pending 真的走到分组」那条。
        """
        mk_cand("价值投资看的是十年后的现金流", "看长期现金流", status="discarded")
        with patch_compile([]) as call:
            res = ingest.compile_candidates()
        self.assertFalse(res["ok"])
        call.s1.assert_not_called()


class CompileOverSplitTest(CompileBase):
    """分组退化成「一条候选一页」时必须被看见（静默的坏结果比报错更贵）。"""

    def test_one_candidate_per_page_is_flagged(self):
        cs = [mk_cand(f"判断{i}：这是第{i}条独立的看法", f"看法{i}") for i in range(4)]
        groups = json.dumps({"groups": [
            {"title": f"页{i}", "domain": "invest", "cand_ids": [c.id]} for i, c in enumerate(cs)
        ]}, ensure_ascii=False)
        with patch_compile([groups]) as call:
            res = ingest.compile_candidates()
        self.assertTrue(res["ok"], res.get("err"))
        self.assertGreater(call.polish.call_count, 0, "4 页各重编一次结论，桩没被用上")
        self.assertTrue(res.get("over_split"), "4 条候选拆 4 页必须报「拆得过细」")

    def test_merged_pages_are_not_flagged(self):
        cs = [mk_cand(f"判断{i}：这是第{i}条独立的看法", f"看法{i}") for i in range(4)]
        groups = json.dumps({"groups": [
            {"title": "甲", "domain": "invest", "cand_ids": [cs[0].id, cs[1].id]},
            {"title": "乙", "domain": "invest", "cand_ids": [cs[2].id, cs[3].id]},
        ]}, ensure_ascii=False)
        with patch_compile([groups]) as call:
            res = ingest.compile_candidates()
        self.assertTrue(res["ok"], res.get("err"))
        self.assertGreater(call.polish.call_count, 0, "2 页各重编一次结论，桩没被用上")
        self.assertFalse(res.get("over_split"), "4 条候选 2 页不算过度拆分")


class CompileViewTest(CompileBase):
    """视图端点 + CSRF。★ 测试客户端默认不校验 CSRF，必须显式开。"""

    def test_post_requires_csrf(self):
        c = Client(enforce_csrf_checks=True)
        self.assertEqual(c.post("/wiki/compile").status_code, 403)

    def test_post_compiles_and_reports(self):
        a = mk_cand("价值投资看的是十年后的现金流", "看长期现金流")
        c = Client()
        token = c.get("/wiki").cookies["csrftoken"].value
        with patch_compile([s1_group([a.id])]) as call:
            resp = c.post("/wiki/compile", {"csrfmiddlewaretoken": token})
        self.assertEqual(resp.status_code, 200)
        self.assertGreater(call.polish.call_count, 0, "视图编译也要真重编结论")
        self.assertIn("生成了 1 页草稿", resp.content.decode())


class CompileFactsTest(CompileBase):
    """facts 整套退役：数字不进散文，精确数字由折叠溯源区读结构化字段负责。"""

    def test_non_numeric_facts_are_dropped(self):
        a = mk_cand("毛利率做到了 47%，同行只有 30%", "毛利率显著高于同行")
        with patch_compile([s1_group([a.id])]) as call:
            res = ingest.compile_candidates()
        self.assertTrue(res["ok"], res.get("err"))
        self.assertGreater(call.polish.call_count, 0, "结论重编桩没被用上")
        fm, body = wstore.parse_front(self.draft_text("ten-year-thinking"))
        self.assertEqual(sorted(fm["candidates"]), [str(a.id)])
        self.assertNotIn("facts", fm, "形态 A 新页不再写 facts（三块整套退役）")
        self.assertEqual(body.strip(), POLISH_TEXT)
        self.assertNotIn("47%", body, "数字不进散文")
        for head in _NO_HEADS:
            self.assertNotIn(head, body)
        # 精确数字由溯源区读 IngestCandidate 字段负责
        trace = wa.trace_rows([a.id])
        self.assertIn("47%", trace["groups"][0]["rows"][0]["quote"])
        # 反证：结论里出现素材没有的数字，新守卫必须响
        material = wstore.candidate_list_text([a.id], {
            a.id: {"claim": a.claim, "quote": a.quote,
                   "source": "report:daily-2026-10-31", "quote_from": "mine"}})
        ok, why = wloop._check_numbers("胜率 187%", material)
        self.assertFalse(ok, "素材里没有 187%，数字守卫必须拒写")
        self.assertIn("187", why)


class LinkForTest(TestCase):
    """深链锚点：候选自己带的 mark_id/msg_id 必须优先于文本反查，直通精确落点。

    ★ 这是 2026-11-02 补的核心洞：编译阶段不再拿 quote 文本去反查「这是哪条
      划线/消息」，而是直接读提取阶段存好的锚点字段 —— 反查有两个死穴
      （同话说过多次配错、quote 是子串误配）。这里 patch 掉 source_url 断言传参，
      不真联网。
    """

    def _cand(self, **kw):
        return IngestCandidate.objects.create(
            batch="b1", source_type="report", source_id="daily-2026-10-31",
            kind="decision", claim="c", quote="q", quote_from="mine", **kw)

    def test_mark_id_from_field_is_used_not_resolved(self):
        c = self._cand(mark_id=99, msg_id=None)
        with patch("core.ingest.sourceblock.source_url", return_value="/x?mk=99") as su:
            link = ingest._link_for(c)
        self.assertEqual(link, "/x?mk=99")
        # 锚点字段存在时，不该再触发任何反查（_mark_id_for 也不会被调）
        su.assert_called_once_with("report", "daily-2026-10-31",
                                   ai=False, mark_id=99, msg_id=None, src="wiki")

    def test_msg_id_triggers_ai_and_msg_params(self):
        c = self._cand(mark_id=None, msg_id=55)
        with patch("core.ingest.sourceblock.source_url", return_value="/x?msg=55") as su:
            ingest._link_for(c)
        # 有 msg_id ⇒ ai=True（弹对话浮窗）+ msg_id 落到具体消息
        su.assert_called_once_with("report", "daily-2026-10-31",
                                   ai=True, mark_id=None, msg_id=55, src="wiki")

    def test_no_anchor_falls_back_to_text_resolution(self):
        # 老候选：两字段都空 ⇒ 退回 _mark_id_for 文本反查（mark_id 传 None 给 source_url）
        c = self._cand(mark_id=None, msg_id=None)
        with patch("core.ingest.sourceblock.source_url", return_value="/x") as su:
            ingest._link_for(c)
        su.assert_called_once()
        kw = su.call_args.kwargs
        self.assertIsNone(kw["msg_id"])
        self.assertFalse(kw["ai"], "没有消息锚点就不该弹对话浮窗")


class CompileLockTest(CompileBase):
    """编译闸：同一时刻只跑一份。

    ★ 为什么前端禁了按钮还要有这把锁：`hx-disabled-elt` 只管**一个页面**。
      两个标签页、点完刷新再点、或者干脆用 curl 打这个端点，都会并发跑两遍 ——
      那是双倍的钱，而且两遍都看到「这页还没生成」，会出两套同名草稿。
      所以闸必须在花钱的地方（core），且必须**拒绝**而不是排队：排队让人以为
      没反应，接着再点一次，等于没加闸。
    """

    def test_second_call_is_refused_before_spending(self):
        a = mk_cand("价值投资看的是十年后的现金流", "价值投资看长期现金流")
        fd = ingest._try_lock_compile()
        self.assertIsNotNone(fd, "第一次就该拿到锁")
        try:
            with patch_compile([s1_group([a.id])]) as call:
                res = ingest.compile_candidates()
            self.assertFalse(res["ok"])
            self.assertIn("还在跑", res["err"])
            # ★ 关键断言：被挡住时**一次模型都不许调**。少了这条，
            #   「闸在分组之后才生效」这种半截闸也会假绿 —— 钱已经花了。
            self.assertEqual(call.s1.call_count, 0)
            self.assertEqual(res["drafts"], [])
        finally:
            ingest._unlock_compile(fd)

    def test_lock_released_after_run(self):
        # 反证：跑完必须放锁。漏放 = 之后永远点不动（而且没人会想到是锁的问题）
        a = mk_cand("价值投资看的是十年后的现金流", "价值投资看长期现金流")
        with patch_compile([s1_group([a.id])]) as call:
            res = ingest.compile_candidates()
        self.assertTrue(res["ok"], res.get("err"))
        self.assertGreater(call.polish.call_count, 0, "结论重编桩没被用上")
        fd = ingest._try_lock_compile()
        self.assertIsNotNone(fd, "编译结束后锁没释放：下一次点编译会被永远挡住")
        ingest._unlock_compile(fd)

    def test_lock_released_even_when_run_fails(self):
        # 失败路径也要放锁：模型调用失败是最常见的失败，漏放就会「只坏一次，之后全瘫」
        mk_cand("价值投资看的是十年后的现金流", "价值投资看长期现金流")
        with patch("core.citeguard._call", return_value=("", "模拟模型失败")) as call:
            res = ingest.compile_candidates()
        self.assertFalse(res["ok"])
        self.assertEqual(call.call_count, 1, "应当真走到了模型这一层（否则没测到失败路径）")
        fd = ingest._try_lock_compile()
        self.assertIsNotNone(fd, "编译失败后锁没释放 —— 失败一次就永久锁死")
        ingest._unlock_compile(fd)


@contextmanager
def patch_call(replies):
    """按顺序返回固定回复的桩（S1、S2…），yield 出 mock 供断言 `call_count`。

    ★ 为什么一定要断言次数：patch 的是**中间层函数**时，「调用 0 次」这个数字
      是桩自己数的 —— 桩没被用上，就等于这次真的联网花了钱，而测试照样全绿
      （2026-11-01 `summary._call` 就是这么漏的）。
    ★ 为什么是 contextmanager：`patch(...).start()` 手动起停很容易漏 stop，
      漏了会把桩泄漏给后续用例（症状是别的测试莫名其妙不联网）。
    """
    state = {"n": 0}

    def _fake(*a, **kw):
        if not replies:
            raise AssertionError("不该再有 LLM 调用（桩已用完）")
        state["n"] += 1
        return replies.pop(0), ""

    with patch("core.citeguard._call", side_effect=_fake) as m:
        yield m


class ArticleCandidateCompileTest(CompileBase):
    """`quote_from=article`（正文事实）的候选必须能编进页里，但**不会被
    标成我的判断 / 我在意**。

    ★ 形态 A：三块分类守卫（judge/material/interest）整套退役。`quote_from`
      在提炼阶段已由 `verify_quotes` 程序锁死，折叠溯源区（`wa.trace_rows`）
      按它分组：mine→我的判断 / article→事实 / highlight→我在意。
      「正文不是我的话」不再靠模型守，而是靠这个程序分组保证 —— 下面几条
      都是「分组不许串台」的反证。
    """

    CLAIM = "某作者计划明天卖出某公司的看跌期权"
    QUOTE = "嗯嗯，明天卖点某公司的 put。"

    def test_article_candidate_goes_to_material_as_fact(self):
        c = mk_cand(self.QUOTE, self.CLAIM, quote_from="article")
        with patch_compile([s1_group([c.id])]) as call:
            res = ingest.compile_candidates()
        self.assertTrue(res["ok"], res.get("err"))
        self.assertGreater(call.polish.call_count, 0, "结论重编桩没被用上")
        fm, body = wstore.parse_front(self.draft_text("ten-year-thinking"))
        self.assertEqual(sorted(fm["candidates"]), [str(c.id)],
                         "正文事实没进页 —— 采纳了也白采纳")
        self.assertEqual(body.strip(), POLISH_TEXT)
        for head in _NO_HEADS:
            self.assertNotIn(head, body)
        # 「事实」这块标题退役；分组与精确出处改看 trace_rows
        trace = wa.trace_rows([c.id])
        self.assertEqual([(g["key"], g["label"]) for g in trace["groups"]],
                         [("article", "事实 / 他人的观点")])
        row = trace["groups"][0]["rows"][0]
        self.assertEqual(row["claim"], self.CLAIM)
        self.assertEqual(row["quote"], self.QUOTE)

    def test_article_candidate_still_cannot_masquerade_as_judgement(self):
        """反证：正文事实的分组由 quote_from 程序锁死，不会被标成「我的判断」。"""
        c = mk_cand(self.QUOTE, self.CLAIM, quote_from="article")
        mine = mk_cand("我自己敲的话", "我自己的判断", quote_from="mine")
        trace = wa.trace_rows([c.id, mine.id])
        groups = {g["key"]: g for g in trace["groups"]}
        self.assertIn("article", groups)
        self.assertIn("mine", groups)
        self.assertIn(c.id, [r["cand_id"] for r in groups["article"]["rows"]])
        self.assertNotIn(c.id, [r["cand_id"] for r in groups["mine"]["rows"]],
                         "正文事实被当成了「我的判断」")
        self.assertEqual(groups["article"]["label"], "事实 / 他人的观点",
                         "理由要说清是哪一层，别一律写成「划出的原文」")

    def test_article_candidate_cannot_be_my_interest(self):
        c = mk_cand(self.QUOTE, self.CLAIM, quote_from="article")
        hl = mk_cand("我划的原文", "我在意这条", quote_from="highlight")
        trace = wa.trace_rows([c.id, hl.id])
        groups = {g["key"]: g for g in trace["groups"]}
        self.assertIn("highlight", groups)
        # 对照正证：分组判据真的按 quote_from 分流，不是恒把谁都塞进同一组
        self.assertIn(hl.id, [r["cand_id"] for r in groups["highlight"]["rows"]],
                      "highlight 候选确实该进「我在意」组")
        self.assertNotIn(c.id, [r["cand_id"] for r in groups["highlight"]["rows"]],
                         "「我在意」只能来自我自己划的原文")
        self.assertEqual(groups["highlight"]["label"], "素材 / 我在意")

    def test_prompt_tells_model_claim_is_the_draft(self):
        """契约测试（不是行为测试）：形态 A 后唯一还在用的 LLM 提示词是
        `wiki_loop.POLISH_SYSTEM`，里面必须留住「结论 = 正文全部」、
        「候选明细当素材」、「页间引用 [[slug]]」这三条。

        ★ 旧形态里测的是 S2 的三块分类提示词（整套退役），这里不再断言它；
          但「提示词必须留住关键承诺」这条契约测试本身留了下来。
        """
        p = wloop.POLISH_SYSTEM
        self.assertIn("结论", p, "「结论是正文全部」这条被删了")
        self.assertIn("候选明细", p, "提示词没说结论该照着哪份素材写")
        self.assertIn("[[", p, "页间引用（[[slug]]）的写法被删了")


class SourceTitleNumberTest(CompileBase):
    """出处标题里的数字不是模型编的（2026-11-03，805 事件）。

    ★ 形态 A：程序不再把来源标题拼进结论正文（正文只有纯 polish 一段），
      标题里带 #805 这类期号自然不会触发数字守卫；精确标题交给折叠溯源区。
      数字守卫现在守的是「结论正文 vs 候选明细素材」（`_check_numbers`）。
    """

    def test_number_in_source_title_is_not_hallucination(self):
        # ★ 夹具仍是 **entry** 来源：报告来源压根不调用 title_of —— 第一版就用错
        #   了来源，变异时用例照样绿（什么都没测）。
        a = IngestCandidate.objects.create(
            batch="b1", source_type="entry", source_id="6991", kind="decision",
            claim="读者认可：利益相关只能解释发言动机",
            quote="利益相关只能解释发言动机", quote_from="mine",
            domain="invest", confidence="high", status="accepted")
        with patch.object(ingest, "title_of",
                          return_value="#805.彼得·蒂尔：AI 的真正危险并非你所想"):
            with patch_compile([s1_group([a.id])]) as call:
                res = ingest.compile_candidates()
        self.assertEqual(call.s1.call_count, 1, "S1 一次分组（桩没被用上就等于真花了钱）")
        self.assertGreater(call.polish.call_count, 0, "结论重编必须真的跑过")
        self.assertTrue(res["ok"], res.get("err"))
        self.assertEqual(res.get("polished"), ["ten-year-thinking"],
                         "标题期号不进结论正文，数字守卫不该误拦整页")
        fm, body = wstore.parse_front(self.draft_text("ten-year-thinking"))
        self.assertEqual(body.strip(), POLISH_TEXT)
        self.assertNotIn("#805", body, "程序不再把来源标题拼进正文")
        self.assertNotIn(wstore.STALE_FM, fm, "set_polish 成功后剥 stale")
        # 精确出处由折叠溯源区读结构化字段负责
        trace = wa.trace_rows([a.id])
        self.assertEqual(trace["total"], 1)
        self.assertEqual(trace["groups"][0]["rows"][0]["source"], "entry:6991")
        # 反证：结论里真出现素材没有的数字，新守卫必须响
        material = wstore.candidate_list_text([a.id], {
            a.id: {"claim": a.claim, "quote": a.quote,
                   "source": "entry:6991", "quote_from": "mine"}})
        ok, why = wloop._check_numbers("这套方法胜率 805 次里对 187 次", material)
        self.assertFalse(ok, "素材里没有 805/187，数字守卫必须拒写")
        self.assertIn("805", why)

    def test_payload_carries_source_title(self):
        a = mk_cand("x", "x")
        with patch.object(ingest, "title_of", return_value="某篇文章"):
            payload = ingest._payload([a])
        self.assertIn("某篇文章", payload,
                      "标题要进 S1 材料：分组模型得知道它是什么")
