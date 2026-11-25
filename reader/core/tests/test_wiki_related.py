"""审批态「可能相关的已入库页」：LLM 宽口径推荐 + 指纹缓存（2026-11-11）。

背景：用户问「编译时会看候选是否适合并进已入库页吗」，实测编译 S1 看得见 7 页
目录卡，但 18 条候选一次 `existing:` 都没给（判据是「确认回答同一问题」，拿不准
就新建）。产品缺口是**人审批时看不到「库里可能有相关页」**。用户定方向：
「推荐还是走 LLM 的好」—— 用模型宽口径列候选，人点「改为并入」拍板。

★ 判据分五层，缺一层就会静默：
  1. **花钱边界**：渲染路径零 LLM（`_approval_workarea_ctx` 13 个调用点）、
     只有 `recommend` 调一次、0 页不调、不开翻倍重试；
  2. **失败不静默**：截断 / 调用错 / 非 JSON / 编造 slug / 没理由，都得说出口，
     且**失败一律不写缓存**（截断既不算成功也不算失败）；
  3. **缓存三态**：`none` / `fresh` / `stale` 是三件不同的事 —— 尤其「查过没有」
     不等于「还没查」，「库变了」不能拿旧结论给人点按钮；
  4. **准入判据一处**：`draft_direction` 三处共用，源码里不许再抄一份字面量；
  5. **视图可见**：三种状态都有文字、花钱按钮说清「一次模型调用」、
     只有 `fresh` 才出现「改为并入」。

★ 空集合不假绿：每条判据都先造非空数据再断言具体内容。
★ 打桩打**被调用点**命名空间：`patch("core.wiki_related.citeguard._call")`。
"""
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from core import citeguard
from core import costreport as cr
from core import ingest
from core import wiki as wstore
from core import wiki_related as wr
from core.models import IngestCandidate

PAGE = """---
title: {title}
domain: {domain}
sources: [entry:1001]
updated: 2026-11-08
confidence: medium
tags: [选股, 市值]
candidates: [1]
---

{body}
"""

DRAFT = """---
title: {title}
domain: ai
sources: [entry:1002]
updated: 2026-11-11
confidence: medium
tags: [选股, 小市值]
candidates: [{cands}]
{extra}---

{body}
"""


def plain(html):
    """只取屏幕上看得见的字（模板注释 / 属性不算）。"""
    import re
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def ok_json(*rows):
    return json.dumps({"related": [{"slug": s, "why": w} for s, w in rows]},
                      ensure_ascii=False)


class RelatedBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-related-")
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
        self.mk_page("tou-zi-xin-fa", "看懂生意是选股的唯一途径，市值大小对判断没帮助。",
                     title="投资心法", domain="invest")
        self.mk_page("yun-chang-ding-jia-quan", "审批收紧后供给受限，云厂商定价权修复。",
                     title="云厂商定价权", domain="invest")
        self.cands = [self.mk_cand(claim="小市值里好找标的，但高成长难找"),
                      self.mk_cand(claim="市值不是判断生意的判据")]
        self.slug = "xiao-shi-zhi"
        self.mk_draft(self.slug, "小市值好找，高成长难找。",
                      [c.id for c in self.cands])

    def mk_page(self, slug, body, *, title=None, domain="ai"):
        d = wstore.root() / "wiki" / domain
        d.mkdir(parents=True, exist_ok=True)
        (d / (slug + ".md")).write_text(
            PAGE.format(title=title or slug, domain=domain, body=body),
            encoding="utf-8")

    def mk_cand(self, *, claim):
        return IngestCandidate.objects.create(
            batch="t", source_type="report", source_id="daily-abc",
            kind="decision", claim=claim, quote=claim, quote_from="mine",
            domain="ai", confidence="medium", status="pending",
            published_at=timezone.now())

    def mk_draft(self, slug, body, cands, *, title=None, extra=""):
        box = wstore.root() / ".drafts"
        box.mkdir(parents=True, exist_ok=True)
        (box / (slug + ".md")).write_text(
            DRAFT.format(title=title or slug, body=body,
                         cands=", ".join(str(i) for i in cands), extra=extra),
            encoding="utf-8")

    def info(self, slug=None):
        from core import wiki_agent as wa
        return wa.read("draft", slug or self.slug)

    def pages(self):
        return wstore.pages()

    def cache(self, rec):
        wr._write_cache(self.slug, rec)

    def cached_row(self):
        info = self.info()
        fp = wr.fingerprint(wr._material(info, self.pages()))
        return {"fp": fp, "rows": [], "discarded": [], "at": "now"}


# ------------------------------------------------------------------ 1. 花钱边界
class SpendBoundaryTest(RelatedBase):
    def test_render_path_never_calls_llm(self):
        """★★ 最贵的一条：`_approval_workarea_ctx` 有 13 个调用点（含 OOB 刷新），
        任何一次渲染起模型都是「刷一次花一次钱」。load / ctx 都必须零调用。"""
        from apps.wiki import views
        with patch("core.wiki_related.citeguard._call") as call:
            rows, state = wr.load(self.slug, self.info(), self.pages())
            ctx = views._approval_workarea_ctx(self.slug)
            render_to_string("wiki/_approval_workarea.html", ctx,
                             request=RequestFactory().get("/"))
        self.assertEqual(call.call_count, 0, "渲染路径起了模型")
        self.assertEqual((rows, state), ([], "none"))
        self.assertEqual(ctx["related_state"], "none")

    def test_recommend_calls_once_with_wiki_pool_and_no_auto_retry(self):
        text = ok_json(("tou-zi-xin-fa", "都在答市值算不算选股判据"))
        with patch("core.wiki_related.citeguard._call", return_value=(text, "")) as call:
            ok, payload = wr.recommend(self.slug)
        self.assertTrue(ok, payload)
        self.assertEqual(payload["rows"][0]["slug"], "tou-zi-xin-fa")
        self.assertEqual(call.call_count, 1)
        kw = call.call_args.kwargs
        self.assertEqual(call.call_args.args[0], "wiki_related")
        self.assertEqual(kw["pool"], cr.POOL_WIKI)
        self.assertEqual(kw["max_calls"], 1)
        self.assertEqual(kw["max_tokens"], settings.WIKI_RELATED_MAX_TOKENS)
        self.assertIs(kw["retry_on_empty"], False, "静默翻倍会多花一次钱")
        self.assertIs(kw["retry_on_truncated"], False)
        self.assertEqual(kw["label"], self.slug)
        # ★ 材料非空，且目录卡里有页：不能拿空集合糊弄出一次「成功」。
        material = json.loads(call.call_args.args[2])
        self.assertEqual(len(material["pages"]), 2)
        self.assertEqual(len(material["draft"]["claims"]), 2)
        self.assertIn("小市值", material["draft"]["claims"][0])

    def test_prompt_limit_comes_from_one_constant(self):
        """★ 改上限只改一处：SYSTEM 已插值，不能残留写死的「最多 2 条」。"""
        self.assertEqual(wr.RELATED_MAX, 5)
        self.assertIn("最多列 5 条", wr.SYSTEM)
        self.assertNotIn("最多 2 条", wr.SYSTEM)
        self.assertNotIn("{MAX}", wr.SYSTEM)

    def test_no_pages_skips_llm_and_still_records_a_real_result(self):
        shutil.rmtree(wstore.root() / "wiki", ignore_errors=True)
        with patch("core.wiki_related.citeguard._call") as call:
            ok, payload = wr.recommend(self.slug)
        self.assertTrue(ok, payload)
        self.assertTrue(payload.get("no_pages"))
        self.assertEqual(call.call_count, 0, "库里 0 页还花钱问模型")
        rows, state = wr.load(self.slug, self.info(), self.pages())
        self.assertEqual((rows, state), ([], "fresh"), "要留一条「查过」而不是空态")

    def test_only_recommend_is_the_llm_entry(self):
        """静态扫源码：`load` / 模板渲染链里不许出现模型调用。"""
        src = Path(wr.__file__).read_text(encoding="utf-8")
        load_body = src.split("def load(")[1].split("\ndef ")[0]
        self.assertNotIn("_call(", load_body)
        self.assertNotIn("recommend(", load_body)
        views_src = Path(
            Path(wr.__file__).parents[1] / "apps/wiki/views.py"
        ).read_text(encoding="utf-8")
        ctx_body = views_src.split("def _approval_workarea_ctx(")[1].split("\ndef ")[0]
        self.assertNotIn("recommend(", ctx_body)
        self.assertIn("wiki_related.load(", ctx_body)


# ------------------------------------------------------------------ 2. 失败不静默
class FailureTest(RelatedBase):
    def test_truncated_result_is_not_cached(self):
        """★★ 截断既不算成功也不算失败：JSON 可能半截，写进缓存比不写更坏。"""
        def fake(kind, system, user, stats, **kw):
            stats["truncated"].append("预算用尽")
            return '{"related":[{"slug":"tou-zi-xin-fa","why":"半截', ""
        with patch("core.wiki_related.citeguard._call", side_effect=fake):
            ok, why = wr.recommend(self.slug)
        self.assertFalse(ok)
        self.assertIn("截断", str(why))
        self.assertEqual(wr.load(self.slug, self.info(), self.pages())[1], "none")

    def test_call_error_is_not_cached_and_reason_is_visible(self):
        with patch("core.wiki_related.citeguard._call", return_value=("", "网络超时")):
            ok, why = wr.recommend(self.slug)
        self.assertFalse(ok)
        self.assertIn("网络超时", str(why))
        self.assertEqual(wr.load(self.slug, self.info(), self.pages())[1], "none")

    def test_bad_json_is_not_silently_treated_as_empty(self):
        with patch("core.wiki_related.citeguard._call",
                   return_value=("我觉得没有相关页", "")):
            ok, why = wr.recommend(self.slug)
        self.assertFalse(ok, "非 JSON 不能被当成「查过，没有」")
        self.assertIn("related", str(why))
        self.assertEqual(wr.load(self.slug, self.info(), self.pages())[1], "none")

    def test_empty_array_is_a_real_checked_result(self):
        """「查过没有」与「还没查」必须在 UI 上是两句话。"""
        with patch("core.wiki_related.citeguard._call",
                   return_value=('{"related": []}', "")):
            ok, payload = wr.recommend(self.slug)
        self.assertTrue(ok, payload)
        self.assertEqual(payload["rows"], [])
        self.assertEqual(wr.load(self.slug, self.info(), self.pages())[1], "fresh")

    def test_fabricated_slug_and_self_link_are_dropped_visibly(self):
        text = ok_json(
            ("tou-zi-xin-fa", "都在答市值算不算选股判据"),
            ("bian-de-slug", "模型编的页"),
            (self.slug, "指向自己"),
        )
        with patch("core.wiki_related.citeguard._call", return_value=(text, "")), \
             patch("core.wiki_related.log.warning") as warn:
            ok, payload = wr.recommend(self.slug)
        self.assertTrue(ok, payload)
        self.assertEqual([r["slug"] for r in payload["rows"]], ["tou-zi-xin-fa"])
        self.assertEqual(len(payload["discarded"]), 2)
        self.assertTrue(any("不合法" in str(c) for c in warn.call_args_list))

    def test_duplicate_no_why_and_non_dict_are_dropped(self):
        raw = json.dumps({"related": [
            {"slug": "tou-zi-xin-fa", "why": "第一个"},
            {"slug": "tou-zi-xin-fa", "why": "重复"},
            {"slug": "yun-chang-ding-jia-quan", "why": ""},
            "我不是对象",
        ]}, ensure_ascii=False)
        with patch("core.wiki_related.citeguard._call", return_value=(raw, "")):
            ok, payload = wr.recommend(self.slug)
        self.assertTrue(ok, payload)
        self.assertEqual([r["slug"] for r in payload["rows"]], ["tou-zi-xin-fa"])
        self.assertGreaterEqual(len(payload["discarded"]), 3)

    def test_three_valid_rows_are_all_kept(self):
        """用户 11-23 定：不再固定 2 条；3 条真相关就 3 条都摆给人看。"""
        self.mk_page("di-san-ye", "第三页", domain="ai")
        text = ok_json(
            ("tou-zi-xin-fa", "一"), ("yun-chang-ding-jia-quan", "二"),
            ("di-san-ye", "三"))
        with patch("core.wiki_related.citeguard._call", return_value=(text, "")):
            ok, payload = wr.recommend(self.slug)
        self.assertTrue(ok, payload)
        self.assertEqual([r["slug"] for r in payload["rows"]],
                         ["tou-zi-xin-fa", "yun-chang-ding-jia-quan", "di-san-ye"])

    def test_more_than_max_valid_rows_is_capped(self):
        """上限就是上限：第 6 条丢掉，前 5 条按模型给的顺序全留。"""
        for name in ("san", "si", "wu", "liu"):
            self.mk_page("di-%s-ye" % name, "第%s页" % name, domain="ai")
        text = ok_json(
            ("tou-zi-xin-fa", "一"), ("yun-chang-ding-jia-quan", "二"),
            ("di-san-ye", "三"), ("di-si-ye", "四"),
            ("di-wu-ye", "五"), ("di-liu-ye", "六"))
        with patch("core.wiki_related.citeguard._call", return_value=(text, "")):
            ok, payload = wr.recommend(self.slug)
        self.assertTrue(ok, payload)
        self.assertEqual(len(payload["rows"]), wr.RELATED_MAX)
        self.assertEqual([r["slug"] for r in payload["rows"]], [
            "tou-zi-xin-fa", "yun-chang-ding-jia-quan", "di-san-ye",
            "di-si-ye", "di-wu-ye"])


# ------------------------------------------------------------------ 3. 缓存三态
class CacheStateTest(RelatedBase):
    def test_none_then_fresh(self):
        self.assertEqual(wr.load(self.slug, self.info(), self.pages())[1], "none")
        rec = self.cached_row()
        rec["rows"] = [{"slug": "tou-zi-xin-fa", "why": "同一判据"}]
        self.cache(rec)
        rows, state = wr.load(self.slug, self.info(), self.pages())
        self.assertEqual(state, "fresh")
        self.assertEqual(rows[0]["slug"], "tou-zi-xin-fa")

    def test_draft_body_change_makes_stale(self):
        self.cache(self.cached_row())
        self.mk_draft(self.slug, "结论被审批对话改写了。",
                      [c.id for c in self.cands])
        self.assertEqual(wr.load(self.slug, self.info(), self.pages())[1], "stale")

    def test_new_page_in_library_makes_stale(self):
        """★★ 库变了旧结论就错：指纹必须包含已入库页清单。"""
        self.cache(self.cached_row())
        self.mk_page("xin-ru-ku-de-ye", "刚审批通过的一页", domain="ai")
        self.assertEqual(wr.load(self.slug, self.info(), self.pages())[1], "stale")

    def test_page_summary_change_makes_stale(self):
        self.cache(self.cached_row())
        self.mk_page("tou-zi-xin-fa", "摘要彻底换了一句话。",
                     title="投资心法", domain="invest")
        self.assertEqual(wr.load(self.slug, self.info(), self.pages())[1], "stale")

    def test_corrupt_cache_is_none_not_crash(self):
        wr._path().parent.mkdir(parents=True, exist_ok=True)
        wr._path().write_text("{ 这不是 json", encoding="utf-8")
        self.assertEqual(wr.load(self.slug, self.info(), self.pages())[1], "none")

    def test_cache_stores_only_model_output(self):
        """★ 要验 `recommend` **真实写出来**的那份缓存，不是手造的字典 ——
        手造的永远只有两个键，等于给自己验了一个不存在的世界。"""
        text = ok_json(("tou-zi-xin-fa", "同一判据"))
        with patch("core.wiki_related.citeguard._call", return_value=(text, "")):
            ok, payload = wr.recommend(self.slug)
        self.assertTrue(ok, payload)
        got = wr._read_cache()[self.slug]
        self.assertNotIn("title", got["rows"][0], "展示标题要读时补，不存旧名字")
        self.assertEqual(set(got["rows"][0]), {"slug", "why"})

    def test_fingerprint_is_stable_and_material_has_all_signals(self):
        info, pages = self.info(), self.pages()
        fp = wr.fingerprint(wr._material(info, pages))
        self.assertEqual(fp, wr.fingerprint(wr._material(info, pages)))
        material = wr._material(info, pages)
        self.assertEqual(len(material["pages"]), 2)
        self.assertEqual(len(material["draft"]["claims"]), 2)
        self.assertTrue(all("tags" in p for p in material["pages"]))

    def test_limit_change_invalidates_cached_recommendation(self):
        """★ 上限一改旧缓存就该 stale：否则 2→5 放宽后浮层还显示上次那 2 条。"""
        info, pages = self.info(), self.pages()
        base = wr.fingerprint(wr._material(info, pages))
        self.assertEqual(wr._material(info, pages)["limit"], wr.RELATED_MAX)
        with patch.object(wr, "RELATED_MAX", wr.RELATED_MAX + 1):
            changed = wr.fingerprint(wr._material(info, pages))
        self.assertNotEqual(base, changed)


# ------------------------------------------------------------------ 4. 准入判据一处
class DirectionTest(RelatedBase):
    def test_direction_marks_every_non_ordinary_draft(self):
        for key in wstore.DRAFT_DIRECTION_KEYS:
            with self.subTest(key=key):
                self.mk_draft("gao-" + key.replace("_", "-"), "正文",
                              [c.id for c in self.cands], extra="%s: true\n" % key)
                info = self.info("gao-" + key.replace("_", "-"))
                self.assertEqual(wstore.draft_direction(info["fm"]), key)
        self.assertEqual(wstore.draft_direction(self.info()["fm"]), "")

    def test_non_ordinary_draft_refuses_recommend_without_spending(self):
        self.mk_draft("yi-you-qu-xiang", "正文", [c.id for c in self.cands],
                      extra="merges: tou-zi-xin-fa\n")
        with patch("core.wiki_related.citeguard._call") as call:
            ok, why = wr.recommend("yi-you-qu-xiang")
        self.assertFalse(ok)
        self.assertIn("merges", str(why))
        self.assertEqual(call.call_count, 0)

    def test_direction_literal_exists_only_once_in_production_code(self):
        """★★ 守卫论域：三处调用方谁再抄一份字面量，这条就红（改坏就红靠变异反证）。"""
        root = Path(wr.__file__).parents[1]
        # ★ 论域要收窄到**完整元组形状**：只写前三键会把 `wiki_agent.read("drafts")`
        #   的字段白名单（"slug","title",...,"merges","edits","delete"）也算进来 ——
        #   守卫论域比公式更易错，假红比不红更贵。
        needle = '("merges", "edits", "delete", "retire", "retire_from")'
        hits = []
        for p in list((root / "core").glob("*.py")) + \
                 list((root / "apps/wiki").glob("*.py")):
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if needle in line and not line.lstrip().startswith("#"):
                    hits.append("%s:%d" % (p.relative_to(root), i))
        self.assertEqual(len(hits), 1, hits)
        self.assertTrue(hits[0].startswith("core/wiki.py:"), hits)


# ------------------------------------------------------------------ 5. 视图可见
class ViewTest(RelatedBase):
    def render(self):
        from apps.wiki import views
        ctx = views._approval_workarea_ctx(self.slug)
        return render_to_string("wiki/_approval_workarea.html", ctx,
                                request=RequestFactory().get("/"))

    def test_none_state_says_not_checked_and_offers_button(self):
        html = self.render()
        self.assertIn("还没查过", plain(html))
        self.assertIn('id="related-refresh"', html)
        self.assertIn("一次模型调用", plain(html))
        self.assertNotIn("merge-into-", html, "没推荐过就不该有并入按钮")

    def test_fresh_state_shows_model_reason_and_merge_button(self):
        rec = self.cached_row()
        rec["rows"] = [{"slug": "tou-zi-xin-fa", "why": "都在答市值算不算选股判据"}]
        self.cache(rec)
        html = self.render()
        self.assertIn("模型推荐", plain(html))
        self.assertIn("都在答市值算不算选股判据", plain(html))
        self.assertIn('id="merge-into-tou-zi-xin-fa"', html)
        self.assertIn("投资心法", plain(html), "要显示中文标题，不是 slug")
        self.assertIn("重新查一次", plain(html))

    def test_fresh_empty_result_is_distinct_from_none(self):
        self.cache(self.cached_row())
        html = self.render()
        self.assertIn("没有找到值得看一眼的相关页", plain(html))
        self.assertNotIn("还没查过", plain(html))

    def test_stale_state_says_expired_and_hides_merge_button(self):
        self.cache(self.cached_row())
        self.mk_page("xin-ye", "新入库一页", domain="ai")
        html = self.render()
        self.assertIn("已过期", plain(html))
        self.assertNotIn("merge-into-", html)

    def test_non_ordinary_draft_hides_whole_block(self):
        self.mk_draft("yi-you-qu-xiang", "正文", [c.id for c in self.cands],
                      extra="merges: tou-zi-xin-fa\n")
        from apps.wiki import views
        ctx = views._approval_workarea_ctx("yi-you-qu-xiang")
        html = render_to_string("wiki/_approval_workarea.html", ctx,
                                request=RequestFactory().get("/"))
        self.assertNotIn('id="related-pages"', html)
        self.assertNotIn('id="related-refresh"', html)

    def test_related_post_guard_blocks_spend(self):
        """编译运行中点「找找相关页」：不花钱，且把原因摆到浮层上。"""
        from django.test import Client
        # ★ 打桩打**判据的真实来源**（`_compile_running_guard` 内部唯一的入口），
        #   不 patch views 的函数本身：URLconf 与测试里的 `apps.wiki.views` 若被
        #   加载成两个模块对象，patch 就会静默失效、模型真被调起来。
        with patch("core.ingest.compile_state",
                   return_value={"running": True, "phase": "compile",
                                 "started_at": "12:00", "elapsed_text": "1 分钟"}), \
             patch("core.wiki_related.citeguard._call") as call:
            resp = Client().post("/wiki/approval/related", {"slug": self.slug})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("正在编译成页", plain(resp.content.decode("utf-8")))
        self.assertEqual(call.call_count, 0)


# ------------------------------------------------------------------ 6. 一键改判并入
class MergeIntoTest(RelatedBase):
    """`ingest.merge_draft_into` 是上一轮写下的链路，此前只有 e2e 离线夹具的
    产物断言、没有单测判据。它直接改写 `.drafts/*.md`，必须把「不会把一页已确认
    的知识悄悄覆盖」和「候选不丢」钉住。"""

    def draft_fm(self, slug=None):
        text = (wstore.root() / ".drafts" /
                ((slug or self.slug) + ".md")).read_text(encoding="utf-8")
        return wstore.parse_front(text)[0]

    def test_ordinary_draft_becomes_merge_draft_without_losing_candidates(self):
        with patch("core.wiki_loop._polish_one", return_value=(True, "")) as polish:
            ok, payload = ingest.merge_draft_into(self.slug, "tou-zi-xin-fa")
        self.assertTrue(ok, payload)
        self.assertTrue(payload["polished"])
        self.assertEqual(polish.call_count, 1, "结论重编没真被调用")
        self.assertEqual(payload["draft"], "tou-zi-xin-fa",
                         "并入稿以目标页命名，不再沿用源稿文件名")
        self.assertEqual(payload["archived"], [self.slug])
        self.assertFalse((wstore.root() / ".drafts" / f"{self.slug}.md").exists(),
                         "整份并入后源稿归档")
        fm = self.draft_fm("tou-zi-xin-fa")
        self.assertEqual(str(fm.get("merges") or "").strip(), "tou-zi-xin-fa")
        self.assertEqual(set(wstore._cand_ids(fm)),
                         {c.id for c in self.cands}, "改判把候选弄丢了")

    def test_target_must_be_an_ingested_page(self):
        """待审草稿不是合法并入目标：列出来会给出一个注定失败的按钮。"""
        self.mk_draft("ling-yi-fen-cao-gao", "另一份待审草稿", [])
        with patch("core.wiki_loop._polish_one") as polish:
            ok, why = ingest.merge_draft_into(self.slug, "ling-yi-fen-cao-gao")
        self.assertFalse(ok)
        self.assertIn("库里没有这一页", str(why))
        self.assertEqual(polish.call_count, 0, "被拒的路径不该花钱")

    def test_draft_with_direction_is_refused(self):
        self.mk_draft("yi-you-qu-xiang", "正文",
                      [c.id for c in self.cands], extra="edits: tou-zi-xin-fa\n")
        with patch("core.wiki_loop._polish_one") as polish:
            ok, why = ingest.merge_draft_into("yi-you-qu-xiang", "tou-zi-xin-fa")
        self.assertFalse(ok)
        self.assertIn("edits", str(why))
        self.assertEqual(polish.call_count, 0)

    def test_no_candidates_is_refused(self):
        self.mk_draft("mei-you-hou-xuan", "空稿", [])
        with patch("core.wiki_loop._polish_one") as polish:
            ok, why = ingest.merge_draft_into("mei-you-hou-xuan", "tou-zi-xin-fa")
        self.assertFalse(ok)
        self.assertIn("没有挂候选", str(why))
        self.assertEqual(polish.call_count, 0)

    def test_existing_draft_for_target_blocks(self):
        self.mk_draft("tou-zi-xin-fa", "同名的待审稿", [])
        with patch("core.wiki_loop._polish_one") as polish:
            ok, why = ingest.merge_draft_into(self.slug, "tou-zi-xin-fa")
        self.assertFalse(ok)
        self.assertIn("已有一份待审草稿", str(why))
        self.assertEqual(polish.call_count, 0)

    def test_polish_failure_leaves_a_visible_stale_draft(self):
        """★ 先写盘再 polish：写盘成功而重编失败时，草稿仍是一份**可见的、
        带 stale 的**并入稿（提交会被拒），不是「两边都没有」的中间态。"""
        with patch("core.wiki_loop._polish_one", return_value=(False, "模型超时")):
            ok, payload = ingest.merge_draft_into(self.slug, "tou-zi-xin-fa")
        self.assertTrue(ok, payload)
        self.assertFalse(payload["polished"])
        self.assertIn("模型超时", payload["polish_err"])
        fm = self.draft_fm("tou-zi-xin-fa")
        self.assertEqual(str(fm.get("merges") or "").strip(), "tou-zi-xin-fa")
        self.assertTrue(bool(fm.get("stale")), "stale 被悄悄剥掉了")

    def test_view_selects_the_merge_draft_and_source_is_archived(self):
        """点「改为并入」后：选中跟着结果稿走（不再是已归档的源稿）。"""
        from django.test import Client
        with patch("core.wiki_loop._polish_one", return_value=(True, "")):
            resp = Client().post("/wiki/approval/merge-into",
                                 {"slug": self.slug, "target": "tou-zi-xin-fa"})
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn('hx-get="/wiki/approval/draft?slug=tou-zi-xin-fa"', html,
                      "选中/列表要切到结果草稿")
        self.assertIn('approval-draft-row is-active', html)
        self.assertNotIn('hx-get="/wiki/approval/draft?slug=%s"' % self.slug, html,
                         "源稿已归档，不该还在审批列表里")
        self.assertEqual(html.count('hx-swap-oob="true"'), 3,
                         "列表 / 头部 / 对话区三个 OOB 都要刷")
        self.assertIn('name="selected_slug" value="tou-zi-xin-fa"', html,
                      "对话区选中要跟着切到结果稿，不能指着已归档的源稿")
        flash = json.loads(resp["HX-Trigger"])["wikiFlash"]["msg"]
        self.assertIn("已并入 tou-zi-xin-fa", flash)
        self.assertIn("源稿已归档", flash)


# ------------------------------------------------------------------ 7. 默认调用
class AutoInvokeTest(RelatedBase):
    """2026-11-15 用户定「审批时找找相关页应该是默认调用的」。

    ★ 用户拍板的边界：stale 也自动重查；写明「已自动查过」+ 保留手动重查。
    ★ 不破的铁律：渲染路径零 LLM、缓存命中零调用、失败不自动重试（防循环）、
      手动按钮仍是 force 重查。
    ★ 实现是模板里 `hx-trigger="load"` 自动发那个 POST，**不是**把模型塞进渲染。
    """

    def render(self, **over):
        from apps.wiki import views
        ctx = views._approval_workarea_ctx(self.slug)
        ctx.update(over)
        return render_to_string("wiki/_approval_workarea.html", ctx,
                                request=RequestFactory().get("/"))

    def test_none_state_renders_auto_form(self):
        html = self.render()
        self.assertIn('id="related-auto-form"', html)
        self.assertIn('hx-trigger="load"', html)
        self.assertIn('name="auto" value="1"', html)
        self.assertIn('hx-post="/wiki/approval/related"', html)
        self.assertIn('name="slug" value="%s"' % self.slug, html)
        self.assertIn("正在自动查相关页", plain(html))
        self.assertIn('id="related-refresh"', html, "手动重查按钮要留着")

    def test_stale_state_also_renders_auto_form(self):
        """用户定的边界：草稿或库变了（stale）也自动重查，信息保持最新。"""
        self.cache(self.cached_row())
        self.mk_page("xin-ye", "新入库一页", domain="ai")
        html = self.render()
        self.assertIn("已过期", plain(html))
        self.assertIn('id="related-auto-form"', html)

    def test_fresh_state_has_no_auto_form(self):
        rec = self.cached_row()
        rec["rows"] = [{"slug": "tou-zi-xin-fa",
                        "why": "都在答市值算不算选股判据"}]
        self.cache(rec)
        html = self.render()
        self.assertNotIn('id="related-auto-form"', html)
        self.assertIn("模型推荐", plain(html))

    def test_related_auto_response_never_rearms(self):
        """★★ 防循环闸门：POST 回包带 related_auto=True，绝不再自动点火。"""
        self.assertNotIn('id="related-auto-form"', self.render(related_auto=True))
        bad = self.render(related_auto=True, related_err="推荐没成功：网络超时")
        self.assertNotIn('id="related-auto-form"', bad, "失败态也不许自动重试")
        self.assertIn("网络超时", plain(bad))

    def test_mutex_disables_auto_form_but_explains(self):
        html = self.render(mutex_msg="正在编译成页")
        self.assertNotIn('id="related-auto-form"', html)
        self.assertIn("编译进行中，暂不自动查相关页", plain(html))
        self.assertIn('id="related-refresh"', html)

    def test_auto_post_cache_hit_calls_no_llm(self):
        """★★ 缓存 fresh 时自动 POST 零调用（OOB 反复点火也不会重复花钱）。"""
        from django.test import Client
        rec = self.cached_row()
        rec["rows"] = [{"slug": "tou-zi-xin-fa",
                        "why": "都在答市值算不算选股判据"}]
        self.cache(rec)
        with patch("core.wiki_related.citeguard._call") as call:
            resp = Client().post("/wiki/approval/related",
                                 {"slug": self.slug, "auto": "1"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(call.call_count, 0, "缓存命中还花钱")
        html = resp.content.decode("utf-8")
        self.assertIn("命中指纹缓存", plain(html))
        self.assertNotIn('id="related-auto-form"', html, "回包不许再自动点火")

    def test_auto_post_calls_once_and_disarms(self):
        from django.test import Client
        text = ok_json(("tou-zi-xin-fa", "都在答市值算不算选股判据"))
        with patch("core.wiki_related.citeguard._call",
                   return_value=(text, "")) as call:
            resp = Client().post("/wiki/approval/related",
                                 {"slug": self.slug, "auto": "1"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(call.call_count, 1)
        html = resp.content.decode("utf-8")
        self.assertIn("已自动查过", plain(html))
        self.assertIn("一次模型调用", plain(html))
        self.assertIn("模型推荐", plain(html))
        self.assertNotIn('id="related-auto-form"', html)

    def test_auto_post_failure_disarms_and_says_retry(self):
        """★★ 失败绝不自动重试（否则无限循环），原因与手动按钮都要在。"""
        from django.test import Client
        with patch("core.wiki_related.citeguard._call",
                   return_value=("", "网络超时")):
            resp = Client().post("/wiki/approval/related",
                                 {"slug": self.slug, "auto": "1"})
        html = resp.content.decode("utf-8")
        self.assertIn("网络超时", plain(html))
        self.assertIn("不再自动重试", plain(html))
        self.assertNotIn('id="related-auto-form"', html)
        self.assertIn('id="related-refresh"', html)

    def test_manual_post_still_forces_recheck(self):
        """人点「重新查一次」是 force：缓存 fresh 也照旧调一次模型。"""
        from django.test import Client
        self.cache(self.cached_row())
        text = ok_json(("tou-zi-xin-fa", "都在答市值算不算选股判据"))
        with patch("core.wiki_related.citeguard._call",
                   return_value=(text, "")) as call:
            resp = Client().post("/wiki/approval/related", {"slug": self.slug})
        self.assertEqual(call.call_count, 1)
        self.assertNotIn("已自动查过", plain(resp.content.decode("utf-8")))

    def test_render_path_still_zero_llm_with_auto_form(self):
        """★★ 加了自动表单后，13 个渲染路径仍一次模型都不许调。"""
        with patch("core.wiki_related.citeguard._call") as call:
            html = self.render()
        self.assertEqual(call.call_count, 0)
        self.assertIn('id="related-auto-form"', html)
