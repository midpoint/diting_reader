"""第九步：断链守卫与自动补链（2026-11-06）。

命题：**能自动的自动做，自动不了的才带方案问人。**

这一组盯九件事，每一件都是「自动改别人页的正文」会引入的新退化：

  1. 批末用**最终态**重扫，不用逐份 approve 的快照（第七步 over_split 同形的假警报）。
  2. 全量断链清单揪出**编译/编辑造的**断链，不只是删页造成的。
  3. 自引用不算断链（与 `rebuild_backlinks_index` 同口径）。
  4. 自动层只改正文，frontmatter 一个字节不动（不碰 candidates / 不撤销方向判断）。
  5. 自动层只落 `.drafts/` 副本，绝不直接写 `wiki/`（自动 ≠ 静默）。
  6. 一致性守卫**自己会响**：让改写函数空转，必须测出 guard_errors，而不是提示悄悄变干净。
  7. 引用页拉不出草稿时显式报 blocked，不静默跳过。
  8. 第三段报告必带三动作，且**不占 `.drafts/`**（保护第八步 e2e 的「草稿清空」契约）。
  9. ★ 作用域：只自动处理**本批新造成**的断链；历史断链只报告不自动改。

★ 第 9 条是 2026-11-06 真回归换来的：首版吃全量清单，人只是编辑一页，系统就把
  库里早就存在的 `[[cost-engineering]]` 自动改了、凭空冒出补链草稿，
  `e2e_wiki_edit` / `e2e_wiki_split` 当场红在「入库后 .drafts/ 清空」。
  所以本文件所有「笔误断链」都造在**本批草稿正文里**，commit 那一刻才产生；
  预置在已入库页里的断链会（正确地）被跳过，测试反而静默失效。

★ 全程零 LLM：三段分类靠确定性判据（target 是否本批被删 + slug 相似度），
  所以本文件**不需要给花钱端点打桩** —— 这条链路上没有花钱端点。让模型猜
  「该指向哪页」反而会编造不存在的 slug，见 `_relink_plan` 的 docstring。
"""
import json
import shutil
import tempfile
from unittest.mock import patch

from django.test import TestCase, override_settings

from core import wiki as wstore
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

TYPOPAGE = "trigger"        # 触发草稿 = 带笔误链接的那页（断链在 commit 时新产生）


def _fm_lines(text):
    """frontmatter 原文行（去掉 `edits:` 这种由 pull_page 加的意图行）。"""
    m = wstore._FM_RE.match(text)
    return [ln for ln in (m.group(1).splitlines() if m else [])
            if not ln.startswith("edits:")]


class RelinkBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-wiki-9-")
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

    # ---------------- 造数据 ----------------
    def mk_page(self, slug, *, domain="invest", cands=(1,), body=None, title=None):
        text = PAGE.format(title=title or slug, domain=domain, slug=slug,
                           cands=json.dumps(list(cands), ensure_ascii=False),
                           body=body if body is not None else f"- 一条判断（候选 {slug}）")
        p = wstore.root() / "wiki" / domain / f"{slug}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def mk_draft(self, slug, *, domain="invest", cands="[1]", extra="", body="- 一条判断"):
        text = PAGE.format(title=slug, domain=domain, slug=slug, cands=cands, body=body)
        if extra:
            text = text.replace("\n---\n", "\n%s---\n" % extra, 1)
        (wstore.root() / ".drafts" / f"{slug}.md").write_text(text, encoding="utf-8")
        return text

    def draft_files(self):
        return sorted(p.name for p in (wstore.root() / ".drafts").glob("*.md"))

    def page_text(self, slug, domain="invest"):
        return (wstore.root() / "wiki" / domain / f"{slug}.md").read_text(encoding="utf-8")

    def draft_text(self, slug):
        return (wstore.root() / ".drafts" / f"{slug}.md").read_text(encoding="utf-8")

    def rebuild(self):
        wstore.rebuild_backlinks_index(mark_log=False)

    def backlinks(self):
        return json.loads((wstore.root() / "wiki" / "backlinks.json").read_text(
            encoding="utf-8"))

    def deletable(self, slug="moat"):
        olds = [mk_cand("老判断%d" % i, "老判断%d" % i) for i in range(2)]
        self.mk_page(slug, cands=[c.id for c in olds])
        ok, payload = wstore.request_delete(slug)
        self.assertTrue(ok, payload)
        return olds

    def _typo(self, body="- 见 [[moats]] 那页"):
        """造一处**本批新产生**的断链：触发草稿正文带笔误 `[[moats]]`。

        ★ 断链必须在 commit 那一刻才出现：预置在已入库页里的是「历史断链」，
          第九步只自动处理新增，预置会让这组测试全部静默失效。
        """
        self.mk_page("moat", cands=(1,))     # 唯一高相似候选
        self.rebuild()                       # 此刻无任何断链
        self.mk_draft(TYPOPAGE, cands="[3]", body=body)


# ================================================================ 第一/二段：自动
class AutoRelinkTest(RelinkBase):
    def test_typo_is_auto_retargeted_and_original_page_untouched(self):
        """★ 自动 ≠ 静默：只落 `.drafts/` 副本，`wiki/` 一个字节不动。"""
        self._typo()

        ok, payload = wstore.commit()

        self.assertTrue(ok, payload)
        before = self.page_text(TYPOPAGE)
        self.assertIn("moats", payload["broken_links"], "全量清单要揪出编译造的断链")
        self.assertIn("[[moats]]", before, "自动层绝不直接写 wiki/")
        self.assertIn(f"{TYPOPAGE}.md", self.draft_files(), "补链稿要落在 .drafts/")
        draft = self.draft_text(TYPOPAGE)
        self.assertIn("[[moat]]", draft)
        self.assertNotIn("[[moats]]", draft)
        self.assertTrue(payload["needs_approval"], "浮层要靠它重新打开，草稿才不静默")

    def test_auto_layer_never_touches_frontmatter(self):
        """★ 补链不许借机改 candidates / title —— 那会撤销人的方向判断。"""
        self._typo()

        ok, payload = wstore.commit()
        self.assertTrue(ok, payload)
        # ★ commit 之后才读：这页在本批才入库，之前盘上根本没有它
        orig = self.page_text(TYPOPAGE)
        self.assertEqual(_fm_lines(self.draft_text(TYPOPAGE)), _fm_lines(orig))
        self.assertEqual(wstore._cand_ids(
            wstore.parse_front(self.draft_text(TYPOPAGE))[0]), [3])

    def test_retarget_keeps_alias(self):
        """`[[moats|护城河]]` → `[[moat|护城河]]`：别名是写的人的措辞，不抹掉。"""
        self._typo(body="- 见 [[moats|护城河]] 那页")
        ok, payload = wstore.commit()
        self.assertTrue(ok, payload)
        self.assertIn("[[moat|护城河]]", self.draft_text(TYPOPAGE))

    def test_no_similar_page_is_auto_demoted(self):
        """第二段：没有合适替代页 → 自动降成纯文本，不问人。"""
        self.mk_page("moat", cands=(1,))
        self.rebuild()
        self.mk_draft(TYPOPAGE, cands="[3]", body="- 见 [[zzzzz]] 那页")

        ok, payload = wstore.commit()

        self.assertTrue(ok, payload)
        actions = [it["action"] for it in payload["relink"]["auto"]]
        self.assertIn("demote", actions)
        text = self.draft_text(TYPOPAGE)
        self.assertNotIn("[[zzzzz]]", text)
        self.assertIn("zzzzz", text)

    def test_self_link_is_not_a_broken_link(self):
        """自引用不算断链：A 提到 A 不产生 A→A，否则每页都自带一条恒真噪音。"""
        self.mk_page("solo", cands=(1,), body="- 见 [[solo]] 自己")
        self.assertEqual(wstore.broken_links(), {})

    def test_only_dangling_from_delete_would_miss_compile_typos(self):
        """★ 反证：若退回「只看删页的 dangling」当输入，编译笔误断链就漏网。"""
        self._typo()
        ok, payload = wstore.commit()
        self.assertTrue(ok, payload)
        self.assertEqual(payload["dangling"], {}, "本批没有删页")
        self.assertNotEqual(payload["broken_links"], {},
                            "但全量清单不能是空的 —— 断链确实存在")
        self.assertEqual(wstore._relink_plan({}, [])["auto"], [],
                         "对照：喂空清单就一个自动方案都出不来（守卫的输入必须全量）")

    def test_preexisting_broken_link_is_reported_but_not_auto_changed(self):
        """★ 作用域反证（2026-11-06 真回归）：历史断链只报告，不自动改、不冒草稿。

        拆掉「批末 − 批前」差集 → 一次无关的 commit 就会把库里早就存在的断链
        自动降级、凭空生成补链稿，把审批流塞满（e2e_wiki_edit / split 当场红）。
        """
        self.mk_page("moat", cands=(1,))
        self.mk_page("old", cands=(2,), body="- 见 [[zzzzz]] 那页")   # 历史断链
        self.rebuild()
        self.mk_draft(TYPOPAGE, cands="[3]")                            # 无关 commit

        ok, payload = wstore.commit()

        self.assertTrue(ok, payload)
        self.assertIn("zzzzz", payload["broken_links"], "全量清单仍要报告，不能装看不见")
        self.assertEqual(payload["relink"]["auto"], [], "历史断链不自动改")
        self.assertEqual(payload["relink"]["drafts"], [], "不能凭空冒出补链草稿")
        self.assertFalse(payload["needs_approval"], "无关的 commit 不该重新打开审批浮层")


# ================================================================ 第三段：带方案问人
class AskRelinkTest(RelinkBase):
    def test_delete_asks_with_three_actions_and_does_not_occupy_drafts(self):
        """★ 保护第八步 e2e：删页造成的断链只带方案问人，不落草稿。"""
        self.mk_page("other", cands=(9,), body="- 见 [[moat]] 那页")
        self.deletable("moat")            # .drafts/moat.md = 删页稿
        self.rebuild()

        ok, payload = wstore.commit()

        self.assertTrue(ok, payload)
        self.assertEqual(payload["dangling"],
                         {"wiki/invest/moat.md": ["wiki/invest/other.md"]},
                         "第八步 dangling 契约不能变")
        self.assertEqual(self.draft_files(), [],
                         "第三段不占 .drafts/ —— 否则 e2e_wiki_delete 的「草稿清空」会红")
        self.assertEqual(payload["relink"]["drafts"], [])
        self.assertFalse(payload["needs_approval"])
        self.assertTrue(payload["needs_decision"])
        ask = payload["relink"]["ask"]
        self.assertEqual(len(ask), 1)
        self.assertEqual(ask[0]["target"], "moat")
        self.assertEqual(ask[0]["actions"], list(wstore.RELINK_ACTIONS),
                         "报告必带动作，不能只给一张裸清单")
        self.assertIn("moat", self.backlinks(), "断链仍留在索引里，不许被清洗掉")


# ================================================================ 守卫与反证
class RelinkGuardTest(RelinkBase):
    def test_final_state_rescan_avoids_stale_snapshot_false_alarm(self):
        """★ 假警报反证：删页稿排在前、引用页编辑稿排在后，快照会误报断链。"""
        self.mk_page("aaa", cands=(1,))
        self.mk_page("bbb", cands=(2,))
        self.mk_page("zzz", cands=(9,), body="- 见 [[aaa]] 那页")
        # 同批：删 aaa（slug 排前）+ 把 zzz 的链接改指 bbb
        self.deletable("aaa")
        self.mk_draft("zzz", cands="[9]", extra="edits: zzz\n",
                      body="- 见 [[bbb]] 那页")
        self.rebuild()

        ok, payload = wstore.commit()

        self.assertTrue(ok, payload)
        self.assertEqual(payload["dangling"], {},
                         "批末最终态已无断链；逐份快照会在删页那一刻误报 zzz")
        self.assertNotIn("aaa", payload["broken_links"])

    def test_guard_itself_rings_when_rewrite_noops(self):
        """★ 守卫自己会响：改写函数空转时，必须测出 guard_errors。"""
        self._typo()
        with patch.object(wstore, "_rewrite_wikilinks",
                          side_effect=lambda text, items, display=None: text):
            ok, payload = wstore.commit()

        self.assertTrue(ok, payload)
        self.assertTrue(payload["relink"]["guard_errors"],
                        "「警告消失 ≠ 内容一致」：不能让提示悄悄变干净")
        self.assertTrue(payload["needs_decision"])

    def test_blocked_referrer_is_reported_not_silently_skipped(self):
        """★ 引用页拉不出草稿时必须显式点名，不能静默跳过。"""
        self._typo()
        with patch.object(wstore, "pull_page",
                          return_value=(False, "已有一份同名草稿")):
            ok, payload = wstore.commit()

        self.assertTrue(ok, payload)
        self.assertEqual(payload["relink"]["drafts"], [])
        self.assertEqual([b["page"] for b in payload["relink"]["blocked"]], [TYPOPAGE])
        self.assertTrue(payload["needs_decision"])

    def test_ambiguous_candidates_are_asked_not_guessed(self):
        """多个高相似候选时不猜，转第三段让人挑 —— 改错页比留断链更贵。"""
        self.mk_page("moats", cands=(1,))
        self.mk_page("moats2", cands=(2,))
        self.rebuild()
        self.mk_draft(TYPOPAGE, cands="[3]", body="- 见 [[moat]] 那页")

        ok, payload = wstore.commit()

        self.assertTrue(ok, payload)
        self.assertEqual(payload["relink"]["auto"], [])
        ask = payload["relink"]["ask"]
        self.assertEqual(len(ask), 1)
        self.assertEqual(ask[0]["reason"], "ambiguous")
        self.assertGreaterEqual(len(ask[0]["candidates"]), 2)
        self.assertEqual(ask[0]["actions"], list(wstore.RELINK_ACTIONS))


# ================================================================ 面板 / 三动作
class RelinkPanelTest(RelinkBase):
    def _ask_scene(self):
        """删页 + 别的页链向它 → 第三段 ask 落盘，`.drafts/` 保持清空。"""
        self.mk_page("other", cands=(9,), body="- 见 [[moat]] 那页")
        self.deletable("moat")
        self.rebuild()
        ok, payload = wstore.commit()
        self.assertTrue(ok, payload)
        return payload

    def test_ask_is_visible_with_two_actions_and_retire_hint(self):
        """★ 断言只看可见文字：清单、两个动作、引用页、第三动作的下线说明。

        ★ 第十步把「先拆走再删」前移到删页提案阶段：这里不再摆假按钮，但
          必须留一句常驻说明 —— 悄悄删掉一个曾经承诺过的动作同样不可以。
          此断言同时钉住「文字还在」与「不再是按钮」两件事。
        """
        self._ask_scene()
        self.assertTrue(wstore.relink_ask()["has_items"], "待决清单要落盘、刷新不丢")
        html = self.client.get("/wiki").content.decode()
        for word in ("断链待处理", "[[moat]]", "改指向", "降级为纯文本",
                     "先拆走再删", "已下线", "删页提案阶段",
                     "还链向它", "wiki/invest/other.md"):
            self.assertIn(word, html, f"面板上缺可见文字：{word}")
        self.assertNotIn(">先拆走再删</button>", html,
                         "第三动作已下线，不该还留着点了没用的按钮")

    def test_retarget_action_makes_a_draft_and_clears_that_ask(self):
        """点「改指向」→ 真落一份 edits 补链草稿 + 自动弹浮层 + 清单删档。"""
        self._ask_scene()
        self.mk_page("moat2", cands=(5,))
        self.rebuild()

        resp = self.client.post("/wiki/relink/apply",
                                {"target": "moat", "action": "retarget",
                                 "slug": "moat2"})

        self.assertEqual(resp.status_code, 200)
        self.assertIn("other.md", self.draft_files(), "改指向要真落一份 edits 草稿")
        self.assertIn("[[moat2]]", self.draft_text("other"))
        self.assertFalse(wstore.relink_ask()["has_items"],
                         "处理完清单要删档，不留一个空横幅")
        self.assertIn("wikiApprovalOpen", resp.headers.get("HX-Trigger", ""),
                      "刚生成的补链稿必须自己弹出来，不能让人去找")

    def test_demote_action_makes_a_draft(self):
        self._ask_scene()
        resp = self.client.post("/wiki/relink/apply",
                                {"target": "moat", "action": "demote"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("other.md", self.draft_files())
        self.assertNotIn("[[moat]]", self.draft_text("other"))
        self.assertIn("moat", self.draft_text("other"), "降级成纯文本，不是删掉那行")

    def test_retarget_refuses_a_nonexistent_page(self):
        """★ 不能用一条断链换另一条断链：目标页不存在要拒，且清单不消失。"""
        self._ask_scene()
        resp = self.client.post("/wiki/relink/apply",
                                {"target": "moat", "action": "retarget",
                                 "slug": "no-such-page"})
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("目标页不存在", html, "失败原因必须看得见")
        self.assertTrue(wstore.relink_ask()["has_items"], "没生效就不能把清单清掉")

    def test_failed_action_keeps_ask_and_shows_the_reason(self):
        """★ 拉不出草稿时清单不能消失，失败原因不能因为「没有草稿卡」而静默。"""
        self._ask_scene()
        with patch.object(wstore, "pull_page",
                          return_value=(False, "已有一份同名草稿")):
            resp = self.client.post("/wiki/relink/apply",
                                    {"target": "moat", "action": "demote"})
        html = resp.content.decode()
        self.assertIn("补链没能落草稿", html)
        self.assertIn("已有一份同名草稿", html)
        self.assertTrue(wstore.relink_ask()["has_items"])

    def test_ask_file_is_deleted_when_nothing_is_pending(self):
        """下线就删档：没有待决项时文件不该留一个空壳。"""
        self._ask_scene()
        p = wstore.root() / wstore.RELINK_ASK_REL
        self.assertTrue(p.exists())
        wstore._write_relink_ask([], [], [])
        self.assertFalse(p.exists())
        self.assertEqual(wstore.relink_ask(), {"ask": [], "blocked": [],
                                               "guard_errors": [],
                                               "has_items": False})
