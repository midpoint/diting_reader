"""第七步：编译链「拆页重组」（2026-11-06）。

命题：编译不只「把新候选并进已有页」，还要能「把已有页里的候选拆出来，和新候选
一起组成新页」。**拆是并的镜像** —— 并走 `merges:`（只增，机器合并），拆走
`edits:`（候选可减，人 commit 才生效）。

这一组盯五件事，每一件都是「拆」自己会引入的新退化：

  1. **渐进式披露**：S1 只看 claim 截短的目录卡（省钱），S1.5 只对被点名的那一页
     付全明细（看得清）。两道材料缺一道，拆页要么看不见、要么贵到不能跑。
  2. **`split` 不许在重建 dict 时丢掉**：丢了不是报错，是「静默按新建处理」——
     界面上只表现为「多了一页」。
  3. **`take` 必须真在那一页里**：模型编一个 id 照收的后果是那条候选**既不在原页
     也不在新页**，页面都在、都挺长，就是少了一条判断。
  4. **迁移守恒**：拆走的候选必须落到某份草稿里。这是本步的铁律落点，用集合差判，
     与「拆几页」「纯拆分」都无关。
  5. **拆完真的能入库**：`commit()` 之后原页候选减少、新页出现 —— 拆是提案，
     落盘要人点，点了要真的成立。

★ 桩纪律（形态 A）：S1 / S1.5 仍 patch `core.citeguard._call`（按 tag 分派、记
  system / payload）；但编译收尾的「结论重编」走 `core.wiki_loop._call`，在
  `SplitCompileTest.setUp` 单独 patch 成固定结论文本（绝不真联网）。「桩真被用上」
  靠编译后草稿正文真的出现那段结论来反证 —— 否则空正文 + `stale` 会让
  `_validate_draft` 拒入库。断言要看**喂进去的材料**，不能只看调用次数：换了提示词
  却忘了换材料，次数照样对，正是「假绿」最典型的形状。
★ 形态 A：正文 = 纯 polish 一段（不含「三块」标题 / `## 候选清单` / `## 通顺话`），
  分组唯一真值是 frontmatter `candidates`。S1 的组必须补 `slug` 作文件名，否则
  `_compose_page` 用 `<domain>-<minid>` 兜底，按 `new-page.md` 找草稿会找不到。
"""
import json
import shutil
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

from django.test import TestCase, override_settings

from core import ingest
from core import wiki as wstore
from core.tests.test_ingest_compile import mk_cand

S1 = "wiki 编译分组"
S15 = "wiki 编译拆页"
S2 = "wiki 编译成页"


def page_md(title, ids, domain="invest"):
    """一页**清零前老页**形态的已入库页（正文非空、带旧三块标题，仅作种子）。

    形态 A 新编出来的页正文是纯 polish 一段；这里保留老正文只是为了让 `pull_page`
    产出的替换稿 / 删页稿有非空正文，不受「正文为空 + 有候选 → 拒入库」闸门影响。
    """
    body = ["## 我的判断"]
    body += ["- 老判断 %d [日报 · 10-31](/summary/2026-10-31)" % i for i in ids]
    body += ["", "## 事实 / 他人的观点", "- （无）", "", "## 我的在意点", "- （无）", ""]
    return ("---\ntitle: %s\ndomain: %s\nsources: [report:daily-2026-10-31]\n"
            "updated: 2026-10-01\nconfidence: medium\ntags: []\ncandidates: [%s]\n---\n\n"
            % (title, domain, ", ".join(str(i) for i in ids))) + "\n".join(body) + "\n"


def s1_json(cand_ids, *, title="新页", domain="invest", split="", target="",
            slug="new-page", tags=None):
    # ★ 形态 A：slug 是草稿文件名（不给就按 `<domain>-<minid>` 兜底，按固定 slug
    #   找草稿的用例会找不到）；tags 是 S1 的模型建议。两者都由 S1 顺带给。
    g = {"title": title, "domain": domain, "cand_ids": list(cand_ids),
         "reason": "同一问题", "slug": slug,
         "tags": ["长期", "现金流"] if tags is None else list(tags)}
    if split:
        g["split"] = split
    if target:
        g["target"] = target
    return json.dumps({"groups": [g]}, ensure_ascii=False)


def s15_json(take, *, title="合并后的新页", domain="invest"):
    return json.dumps({"take": list(take), "title": title, "domain": domain,
                       "reason": "这几条在回答同一个问题"}, ensure_ascii=False)


def s2_json(ids, *, slug="new-page", title="新页"):
    lines = [{"cand_id": i, "section": "judge", "type": "judgment",
              "text": "判断%d" % i} for i in ids]
    return json.dumps({"slug": slug, "title": title, "lines": lines,
                       "facts": [], "hint": ""}, ensure_ascii=False)


@contextmanager
def patch_llm(handlers):
    """按 tag 分派的桩。`handlers[tag] = fn(user, rec)`，返回文本或 `(text, err)`。

    ★ 没准备的 tag 一律断言失败：漏一个 handler 就是这次真联网花了钱。
    """
    seen = []

    def _fake(kind, system, user, stats, **kw):
        tag = kw.get("tag") or ""
        rec = {"tag": tag, "system": system, "user": user, "label": kw.get("label") or ""}
        seen.append(rec)
        fn = handlers.get(tag)
        if fn is None:
            raise AssertionError("用例没为 tag「%s」准备桩（那就会真花钱）" % tag)
        r = fn(user, rec)
        return r if isinstance(r, tuple) else (r, "")

    with patch("core.citeguard._call", side_effect=_fake) as m:
        yield m, seen


class SplitBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-split-")
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

    # ---- 夹具
    def put_page(self, domain, slug, text):
        p = wstore.root() / "wiki" / domain / f"{slug}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def page_text(self, domain, slug):
        return (wstore.root() / "wiki" / domain / f"{slug}.md").read_text(encoding="utf-8")

    def draft_path(self, slug):
        return wstore.root() / ".drafts" / f"{slug}.md"

    def draft_fm(self, slug):
        return wstore.parse_front(self.draft_path(slug).read_text(encoding="utf-8"))[0]

    def draft_names(self):
        return sorted(p.stem for p in (wstore.root() / ".drafts").glob("*.md"))

    def old_page_with(self, n=2, domain="invest", slug="llm-moat"):
        """造一页 + 它引用的 n 条**已入库**候选（它们不在本轮待编译集合里）。"""
        olds = [mk_cand("老判断%d" % i, "老判断%d" % i) for i in range(n)]
        self.put_page(domain, slug, page_md("LLM 护城河在验证器",
                                            [c.id for c in olds], domain))
        return olds


# ----------------------------------------------------------------- 目录卡
class PageCatalogTest(SplitBase):
    """S1 的第一层材料：每页一张卡 + 候选摘要。没有它，S1 判不出「这页该拆」。"""

    def test_card_carries_claim_but_not_quote(self):
        """卡上给 claim（够判断在问什么），**不给 quote** —— 出处是噪音，全明细
        留给 S1.5 在真正要拆的那一页上花。"""
        olds = self.old_page_with(2)
        catalog = ingest._page_catalog(wstore.pages())
        card = [c for c in catalog if c["slug"] == "llm-moat"][0]
        self.assertEqual([c["id"] for c in card["candidates"]], [c.id for c in olds])
        for c in card["candidates"]:
            self.assertTrue(c["claim"])
            self.assertNotIn("quote", c, "卡上不许带 quote：那是 S1.5 才付的钱")
            self.assertLessEqual(len(c["claim"]), 40)

    def test_discarded_candidate_is_not_listed(self):
        """已丢弃的候选不进卡：它拆不进任何页（正文写不出），列着只会诱使模型提一个
        注定失败的拆页方案。"""
        live = mk_cand("还在", "还在")
        dead = mk_cand("丢了", "丢了", status="discarded")
        self.put_page("invest", "p1", page_md("页一", [live.id, dead.id]))
        card = [c for c in ingest._page_catalog(wstore.pages()) if c["slug"] == "p1"][0]
        self.assertEqual([c["id"] for c in card["candidates"]], [live.id])

    def test_truncation_says_how_many_are_hidden(self):
        """截断必须说出口：只列 8 条而页里有 10 条却不说，模型会以为那就是全部，
        于是判定「没有该拆的」—— 静默漏掉正是本功能要防的事。"""
        cs = [mk_cand("判断%d" % i, "判断%d" % i) for i in range(10)]
        self.put_page("invest", "big", page_md("大页", [c.id for c in cs]))
        card = [c for c in ingest._page_catalog(wstore.pages()) if c["slug"] == "big"][0]
        self.assertEqual(len(card["candidates"]), ingest.COMPILE_PAGE_CANDS)
        self.assertEqual(card["cands_more"], 10 - ingest.COMPILE_PAGE_CANDS)

    def test_s1_payload_actually_carries_the_candidates(self):
        """反证：造好了卡但没塞进 payload，等于白造（S1 什么也看不见）。"""
        self.old_page_with(1)
        c = mk_cand("新判断", "新判断")
        with patch_llm({S1: lambda u, r: s1_json([c.id])}) as (_m, seen):
            ingest.plan_groups([c], wstore.pages())
        payload = json.loads(seen[0]["user"])
        card = [p for p in payload["pages"] if p["slug"] == "llm-moat"][0]
        self.assertTrue(card["candidates"], "S1 的 payload 里没有候选摘要 → 判不出该拆哪页")


# ----------------------------------------------------------------- S1.5 拆页判断
class SplitPlanTest(SplitBase):
    """S1 点名 + S1.5 聚焦 → 组的 `split_plan`。"""

    def _run(self, cands, s1, s15=None, pages=None):
        handlers = {S1: lambda u, r: s1}
        if s15 is not None:
            handlers[S15] = lambda u, r: s15
        with patch_llm(handlers) as (_m, seen):
            groups, err, notes = ingest.plan_groups(cands, pages or wstore.pages())
        return groups, err, notes, seen

    def test_split_gets_a_plan_from_the_focus_call(self):
        olds = self.old_page_with(2)
        n = mk_cand("新判断", "新判断")
        groups, err, notes, seen = self._run(
            [n], s1_json([n.id], split="llm-moat"), s15_json([olds[0].id]))
        self.assertEqual(err, "")
        self.assertEqual([s["tag"] for s in seen], [S1, S15], "拆页必须多花一次聚焦调用")
        plan = groups[0]["split_plan"]
        self.assertEqual(plan["from"], "llm-moat")
        self.assertEqual(plan["take"], [olds[0].id])
        self.assertEqual(plan["title"], "合并后的新页")
        self.assertEqual(plan["domain"], "invest")
        self.assertTrue(any("拆走" in x for x in notes), "拆页这件事要说出来")

    def test_focus_prompt_is_its_own_and_material_is_full_detail(self):
        """S1.5 用**独立提示词**，材料是那一页的候选全明细（含 quote）。
        共用一个提示词 = 一个提示词干两件事，两件都会变糊。"""
        olds = self.old_page_with(2)
        n = mk_cand("新判断", "新判断")
        _g, _e, _n, seen = self._run([n], s1_json([n.id], split="llm-moat"),
                                     s15_json([olds[0].id]))
        s15 = [s for s in seen if s["tag"] == S15][0]
        self.assertIn(ingest.COMPILE_S1_SPLIT_SYSTEM, s15["system"])
        payload = json.loads(s15["user"])
        self.assertEqual(payload["page"]["slug"], "llm-moat")
        self.assertEqual(len(payload["page"]["candidates"]), 2, "要全明细，不是摘要")
        self.assertTrue(payload["page"]["candidates"][0]["quote"], "全明细要带 quote")
        self.assertEqual([c["cand_id"] for c in payload["group"]["candidates"]], [n.id])
        self.assertIn("invest", payload["domains"])

    def test_split_is_carried_through_clean_groups(self):
        """★ 反证：`_clean_groups` 重建 dict 时丢掉 `split`，所有拆页判定会**静默**
        变成「新建」—— 界面只表现为「多了一页」，没有任何报错（target 踩过同款）。"""
        raw = [{"title": "新页", "domain": "invest", "cand_ids": [1], "split": "llm-moat"}]
        out = ingest._clean_groups(raw, {1}, set())
        self.assertEqual(out[0]["split"], "llm-moat")

    def test_target_outside_catalog_falls_back_to_new(self):
        n = mk_cand("新判断", "新判断")
        groups, err, notes, _seen = self._run(
            [n], s1_json([n.id], split="no-such-page"), s15_json([]))
        self.assertEqual(err, "")
        self.assertEqual(groups[0]["target"], "")
        self.assertFalse(groups[0].get("split_plan"))
        self.assertTrue(any("清单里没有它" in x for x in notes))

    def test_split_a_pending_draft_falls_back_to_new(self):
        """草稿不能拆（`pull_page` 只认 `wiki/`）：明说「先处理那份草稿」，
        不许让它走到写盘那一步才失败。"""
        n = mk_cand("新判断", "新判断")
        (wstore.root() / ".drafts").mkdir(parents=True, exist_ok=True)
        self.draft_path("dpage").write_text(page_md("草稿页", [n.id]), encoding="utf-8")
        # 与 `_compile_locked` 同形：草稿进目录（同 slug 时盖过已入库页）
        pages = [p for p in wstore.pages() if p["slug"] != "dpage"]
        pages.append({"slug": "dpage", "title": "草稿页", "domain": "invest",
                      "updated": "", "summary": "", "page": ".drafts/dpage.md",
                      "draft": True, "candidates": [n.id]})
        groups, err, notes, _seen = self._run([n], s1_json([n.id], split="dpage"),
                                              s15_json([n.id]), pages=pages)
        self.assertEqual(err, "")
        self.assertFalse(groups[0].get("split_plan"))
        self.assertTrue(any("待审批草稿" in x for x in notes))

    def test_split_across_domains_falls_back_to_new(self):
        """并入不许跨域，拆出来再合成本组候选同样不许（镜像条款）。"""
        self.old_page_with(2, domain="invest")
        n = mk_cand("新判断", "新判断", domain="ai")
        groups, _e, notes, _seen = self._run([n], s1_json([n.id], domain="ai",
                                                          split="llm-moat"),
                                             s15_json([1]))
        self.assertFalse(groups[0].get("split_plan"))
        self.assertTrue(any("不跨域" in x for x in notes))

    def test_take_outside_the_page_is_ignored_and_reported(self):
        """模型编一个 id：忽略它，并**说出来**（不静默）。"""
        olds = self.old_page_with(2)
        n = mk_cand("新判断", "新判断")
        bogus = 99999
        groups, _e, notes, _seen = self._run(
            [n], s1_json([n.id], split="llm-moat"),
            s15_json([olds[0].id, bogus]))
        self.assertEqual(groups[0]["split_plan"]["take"], [olds[0].id])
        self.assertTrue(any(str(bogus) in x and "忽略" in x for x in notes))

    def test_all_take_outside_the_page_means_no_split(self):
        olds = self.old_page_with(2)
        n = mk_cand("新判断", "新判断")
        groups, _e, notes, _seen = self._run(
            [n], s1_json([n.id], split="llm-moat"), s15_json([99999]))
        self.assertFalse(groups[0].get("split_plan"))
        self.assertTrue(any("按新建处理" in x for x in notes))

    def test_empty_take_means_no_split(self):
        self.old_page_with(2)
        n = mk_cand("新判断", "新判断")
        groups, _e, notes, _seen = self._run(
            [n], s1_json([n.id], split="llm-moat"), s15_json([]))
        self.assertFalse(groups[0].get("split_plan"))
        self.assertTrue(any("没有该拆走的候选" in x for x in notes))

    def test_target_and_split_together_prefers_split_and_says_so(self):
        """拆与并互斥：同时给了两个就按拆处理（更保守的那一头），并把丢掉的说出来。"""
        self.old_page_with(2)
        n = mk_cand("新判断", "新判断")
        groups, _e, notes, _seen = self._run(
            [n], s1_json([n.id], split="llm-moat", target="existing:llm-moat"),
            s15_json([1]))
        self.assertEqual(groups[0]["target"], "")
        self.assertTrue(groups[0].get("split_plan"))
        self.assertTrue(any("互斥" in x for x in notes))

    def test_unregistered_domain_from_model_falls_back_to_the_page_domain(self):
        olds = self.old_page_with(1)
        n = mk_cand("新判断", "新判断")
        groups, _e, notes, _seen = self._run(
            [n], s1_json([n.id], split="llm-moat"),
            s15_json([olds[0].id], domain="不存在的域"))
        self.assertEqual(groups[0]["split_plan"]["domain"], "invest")
        self.assertTrue(any("不在册" in x for x in notes))


# ----------------------------------------------------------------- 落地：两份草稿
class SplitCompileTest(SplitBase):
    """真正写盘：原页的替换稿（候选减少）+ 新页草稿（拆出的 + 本轮的）。"""

    def setUp(self):
        super().setUp()
        # ★ 形态 A：编译收尾对每份非替换稿调 `wiki_loop._call` 重编结论。桩打在
        #   被调用点（patch_llm 那份 citeguard 桩不接 polish，否则会真花钱），
        #   配 `_assert_polish_used` 的「桩真被用上」反证。
        patcher = patch("core.wiki_loop._call",
                        return_value=("这是一段结论。", [], None))
        self.polish = patcher.start()
        self.addCleanup(patcher.stop)

    def _assert_polish_used(self, res):
        """『桩真被用上』反证：结论桩返回的文本真的写进了对应草稿正文。"""
        self.assertGreater(self.polish.call_count, 0,
                           "wiki_loop._call 桩没被调用（那会真花钱）")
        self.assertTrue(res["polished"], "没有一页被成功 polish 写回")
        self.assertFalse(res["polish_failed"], "不该有结论重编失败：%s" % res["polish_failed"])
        for slug in res["polished"]:
            fm, body = wstore.parse_front(self.draft_path(slug).read_text(encoding="utf-8"))
            self.assertIn("这是一段结论。", body,
                          "polish 桩的结论没进草稿正文 —— 桩没被真正用上")
            self.assertNotIn("stale", fm, "set_polish 成功后 stale 要被剥掉")
            self.assertNotIn("## 候选清单", body)
            self.assertNotIn("## 通顺话", body)

    def _compile(self, take_n=1, *, page_slug="new-page"):
        olds = self.old_page_with(2)
        n1 = mk_cand("新判断一", "新判断一")
        n2 = mk_cand("新判断二", "新判断二")
        take = [o.id for o in olds[:take_n]]

        # ★ 形态 A：S2 不再调模型，`_compose_page` 只组装 frontmatter + 空正文；
        #   原来「S2 payload 里候选 = 拆出的 + 本轮的」那条验证改由草稿的
        #   frontmatter `candidates` 断言（见 test_writes_a_replacement_draft_and_a_new_page）。
        handlers = {
            S1: lambda u, r: s1_json([n1.id, n2.id], split="llm-moat", slug=page_slug),
            S15: lambda u, r: s15_json(take),
        }
        with patch_llm(handlers) as (m, _seen):
            res = ingest.compile_candidates()
        self._assert_polish_used(res)
        return res, olds, [n1, n2], m

    def test_writes_a_replacement_draft_and_a_new_page(self):
        res, olds, _new, _m = self._compile()
        self.assertTrue(res["ok"], res.get("err"))
        self.assertEqual(self.draft_names(), ["llm-moat", "new-page"])

        rep = self.draft_fm("llm-moat")
        self.assertEqual(rep["edits"], "llm-moat", "替换稿必须打 edits 标记（不是 merges）")
        self.assertNotIn("merges", rep, "拆不是并：带 merges 会被按「只增不减」校验")
        self.assertEqual([int(i) for i in rep["candidates"]], [olds[1].id],
                         "拆走的那条要从原页的 candidates 里去掉")

        new = self.draft_fm("new-page")
        self.assertEqual([int(i) for i in new["candidates"]],
                         [olds[0].id, _new[0].id, _new[1].id])
        self.assertFalse(new.get("merges"))
        self.assertFalse(new.get("edits"))

    def test_original_page_is_untouched_until_commit(self):
        """★ 副本不是移动：编译期间原页原地不动，否则反链断、index 缺行、
        `[[wikilink]]` 404（第六步的判据，拆页同样适用）。"""
        self._compile()
        fm = wstore.parse_front(self.page_text("invest", "llm-moat"))[0]
        self.assertEqual(len(fm["candidates"]), 2, "原页候选一条都不许在编译期少")

    def test_commit_reduces_source_page_and_adds_the_new_one(self):
        """拆是提案，人点了才落盘；点了要真的成立。"""
        self._compile()
        ok, payload = wstore.commit()
        self.assertTrue(ok, payload)
        self.assertEqual(payload["edited"], ["wiki/invest/llm-moat.md"])
        self.assertEqual(payload["committed"], ["wiki/invest/new-page.md"])
        self.assertEqual(len(wstore.parse_front(
            self.page_text("invest", "llm-moat"))[0]["candidates"]), 1)
        self.assertTrue((wstore.root() / "wiki" / "invest" / "new-page.md").exists())
        self.assertEqual(self.draft_names(), [])

    def test_pure_split_becomes_a_delete_request(self):
        """纯拆分（把整页搬空）= 删页（第八步落地后）。

        ★ 第七步它是个「安全的停点」（空候选被 `missing_fm` 拦住、入不了库）；
          第八步把那个停点变成了**显式的删除语义**：`edits` + 空候选 = 删除请求，
          提交后该页移除、候选一并回池废弃。所以这里钉的是新契约：
          ① 编译必须当场说清「这一轮等于在提一个删除」，不能让人以为出了个新页；
          ② 这份稿是**完整**的（否则删除永远入不了库）；
          ③ 提交后页真没了，候选变 discarded。
        """
        res, _olds, _new, _m = self._compile(take_n=2)
        self.assertTrue(res["ok"], res.get("err"))
        joined = "；".join(res["notes"])
        self.assertIn("删除请求", joined)
        drafts = {d["slug"]: d for d in wstore.list_drafts()}
        self.assertTrue(drafts["llm-moat"]["delete"], "空候选 + edits 必须认成删除稿")
        self.assertTrue(drafts["llm-moat"]["complete"], "删除稿是完整的（不是空页）")

        ok, payload = wstore.commit()
        self.assertTrue(ok, payload)
        self.assertEqual(payload["deleted"], ["wiki/invest/llm-moat.md"])
        self.assertFalse((wstore.root() / "wiki" / "invest" / "llm-moat.md").exists())
        self.assertTrue((wstore.root() / "wiki" / "invest" / "new-page.md").exists())
        self.assertEqual(self.draft_names(), [])
        # 候选一并废弃：这正是「删页 = 页内判断也不要了」的落点
        from core.models import IngestCandidate
        old_ids = [c.id for c in _olds]
        self.assertEqual(IngestCandidate.objects.filter(
            id__in=old_ids, status="discarded").count(), len(old_ids))

    def test_conservation_guard_rolls_back_when_a_taken_candidate_vanishes(self):
        """★ 本步的铁律：拆走的候选必须落到某份草稿里。

        反证构造成「程序写错了」——新页的 frontmatter 少了一条拆出来的候选。
        此时原页的替换稿已经把它移走、新页又没收，那条判断就从整个系统里消失了：
        两页都在、都挺长，没有任何一处报错。所以守卫必须整轮回滚。
        """
        real = ingest._compose_page
        olds = self.old_page_with(2)
        n = mk_cand("新判断", "新判断")
        take = [olds[0].id]

        def mangle(g, cands_by_id, payload, old_page=None):
            text, slug, drop, err = real(g, cands_by_id, payload, old_page)
            if text and not err:
                fm, _b = wstore.parse_front(text)
                keep = [i for i in wstore._cand_ids(fm) if i not in take]
                text = wstore._set_fm_line(text, "candidates",
                                           "[%s]" % ", ".join(str(i) for i in keep))
            return text, slug, drop, err

        handlers = {S1: lambda u, r: s1_json([n.id], split="llm-moat"),
                    S15: lambda u, r: s15_json(take),
                    S2: lambda u, r: s2_json(take + [n.id], slug="new-page")}
        with patch("core.ingest._compose_page", side_effect=mangle):
            with patch_llm(handlers):
                res = ingest.compile_candidates()
        self.assertFalse(res["ok"], "守恒破了还报成功，就是把人往坑里带")
        self.assertIn(str(olds[0].id), res["err"], "要点名是哪一条没了")
        self.assertEqual(self.draft_names(), [], "整轮回滚：一个字节都不留")

    def test_replacement_draft_is_not_counted_as_a_new_page(self):
        """拆页会凭空多出一份草稿：不剔掉的话，一个正常分组会被误判成
        「一条候选一页」（假警报会教人无视警报）。"""
        res, _olds, _new, _m = self._compile()
        self.assertFalse(res.get("over_split"))

    def test_merging_into_a_page_split_in_the_same_round_is_refused(self):
        """一轮里既拆 P 又往 P 里并：后写的会拿**老底本**重写 P，把刚拆走的候选
        原样写回来（于是它们同时在 P 和新页里）。必须拒，且理由要指向「拆」。

        ★ 这条不是假想：模型完全可以为一批新候选判「并进 P」、为另一批判「从 P 拆」。
        """
        olds = self.old_page_with(2)
        n1 = mk_cand("新判断一", "新判断一")
        n2 = mk_cand("新判断二", "新判断二")
        split_group = {"title": "新页", "domain": "invest", "cand_ids": [n1.id],
                       "split": "llm-moat", "reason": "同问"}
        merge_group = {"title": "并入组", "domain": "invest", "cand_ids": [n2.id],
                       "target": "existing:llm-moat", "reason": "同问"}
        handlers = {
            S1: lambda u, r: json.dumps({"groups": [split_group, merge_group]},
                                        ensure_ascii=False),
            S15: lambda u, r: s15_json([olds[0].id]),
            S2: lambda u, r: s2_json([olds[0].id, n1.id], slug="new-page"),
        }
        with patch_llm(handlers):
            res = ingest.compile_candidates()
        self._assert_polish_used(res)
        self.assertTrue(res["ok"], res.get("err"))
        bad = [m for m in res["groups"] if m.get("err")]
        self.assertEqual(len(bad), 1, res["groups"])
        self.assertIn("拆成替换稿", bad[0]["err"])
        rep = self.draft_fm("llm-moat")
        self.assertEqual(rep["edits"], "llm-moat", "替换稿没被合并稿盖掉")
        self.assertEqual([int(i) for i in rep["candidates"]], [olds[1].id],
                         "拆走的候选不许被合并稿写回原页")

    def test_compile_record_keeps_splits_out_of_merged(self):
        """落盘的编译结果里，拆页与「并入已有页」必须分开。

        ★ 两者都会写 `target`（替换稿有目标页），最省事的写法就是混进 `merged` ——
          而面板会照着 `merged` 说「其中 N 页并入已有页，通过后会覆盖原页」，
          于是**一次纯拆分**被说成一次合并：人以为原页内容被并进来了。
        """
        res, olds, _new, _m = self._compile()
        self.assertTrue(res["ok"], res.get("err"))
        rec = ingest.read_compile_result()
        self.assertEqual([s["from"] for s in rec["splits"]], ["llm-moat"])
        self.assertEqual(rec["splits"][0]["cand_ids"], [olds[0].id])
        self.assertEqual(rec["merged"], [], "拆页不许混进 merged")
        self.assertEqual(sorted(rec["pages"]), ["llm-moat", "new-page"])
