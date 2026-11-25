"""审批浮层「自己指定并入页」：模型没推荐 ≠ 不许人决定（2026-11-16）。

背景（用户实测，D87 `ai-agent-xie-zuo-pi-lao` 并入「降噪相关」）：
  审批浮层里「改为并入」的目标**只来自模型推荐**（`meta/related.json`）。推荐
  是空的（`rows: []`）时整块没按钮 —— 人知道该并进哪一页，却没有任何入口，
  只能改用对话工具 `merge_into`；而整页草稿（全部候选都要走）又被工具拒收
  （那属于「整页并入」，只能走浮层）。两头堵死，草稿原地卡住。

判据分四层：
  1. **入口存在**：普通新页稿的浮层里有「自己指定一页并入」，选项 = 全部已入库页；
  2. **不给错入口**：去向已定（merges / edits / delete）的稿不出现这块；
     目标只能是已入库页 —— 选项里不许混进待审草稿（两份待审稿互相覆盖）；
  3. **真能并**：目标**不在**推荐缓存里也能 POST 成功，源稿归档、候选全带上
     （整页并入不许丢一条候选）；
  4. **错也看得见**：非法 / 未入库目标回片段 + `merge_err`，不 500、不静默。

★ 空集合不假绿：先造两份已入库页 + 两条候选，再断言具体内容（不是「非空即过」）。
"""
import shutil
import tempfile
from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import Client, RequestFactory, override_settings

from core import wiki as wstore
from core.tests.test_wiki_related import RelatedBase, plain


class ManualTargetTest(RelatedBase):
    """复用 `RelatedBase` 的夹具：2 份已入库页 + 1 份普通新页稿（2 条候选）。"""

    def render(self, slug=None):
        from apps.wiki import views
        ctx = views._approval_workarea_ctx(slug or self.slug)
        return ctx, render_to_string("wiki/_approval_workarea.html", ctx,
                                     request=RequestFactory().get("/"))

    # ---------------------------------------------------------------- 1. 入口
    def test_picker_lists_every_published_page(self):
        ctx, html = self.render()
        slugs = [t["slug"] for t in ctx["merge_targets"]]
        # 两份已入库页都在，且与「模型有没有推荐」无关（夹具里根本没查过推荐）
        self.assertIn("tou-zi-xin-fa", slugs)
        self.assertIn("yun-chang-ding-jia-quan", slugs)
        self.assertEqual(len(slugs), 2, "只列已入库页，一份不多")
        self.assertIn('id="merge-manual-form"', html)
        self.assertIn('name="target"', html)
        self.assertNotIn("merge-into-", html, "没推荐过就不该有推荐行的按钮")
        self.assertIn("自己指定一页并入", plain(html))

    def test_picker_options_show_chinese_titles_not_slugs(self):
        ctx, html = self.render()
        self.assertIn('<option value="tou-zi-xin-fa">投资心法 · invest</option>',
                      html, "下拉要给人看标题 + 域，不是拼音 slug")

    def test_picker_appears_even_when_model_recommended_something(self):
        """有推荐也必须留着手动口 —— 推荐对不上时人不能又被堵一次。"""
        rec = self.cached_row()
        rec["rows"] = [{"slug": "tou-zi-xin-fa", "why": "都在答市值算不算选股判据"}]
        self.cache(rec)
        _ctx, html = self.render()
        self.assertIn('id="merge-into-tou-zi-xin-fa"', html)
        self.assertIn('id="merge-manual-form"', html)

    # ------------------------------------------------------------ 2. 不给错入口
    def test_picker_hidden_for_draft_with_direction(self):
        self.mk_draft("yi-you-qu-xiang", "正文", [c.id for c in self.cands],
                      extra="merges: tou-zi-xin-fa\n")
        ctx, html = self.render("yi-you-qu-xiang")
        self.assertEqual(ctx["merge_targets"], [])
        self.assertNotIn('id="merge-manual-form"', html)

    def test_targets_never_include_pending_drafts(self):
        """待审草稿不是合法目标：两份待审稿互相覆盖 = 谁先入库谁吃掉谁。"""
        self.mk_draft("ling-yi-fen-cao-gao", "另一份", [])
        ctx, _html = self.render()
        slugs = [t["slug"] for t in ctx["merge_targets"]]
        self.assertNotIn("ling-yi-fen-cao-gao", slugs)

    # ---------------------------------------------------------------- 3. 真能并
    def test_merge_into_works_without_any_recommendation(self):
        """D87 现场：推荐为空（rows=[]），手选目标仍然能并成功。"""
        self.cache(self.cached_row())          # rows 空 = 模型说「没有相关页」
        with patch("core.wiki_loop._polish_one", return_value=(True, "")):
            resp = Client().post("/wiki/approval/merge-into",
                                 {"slug": self.slug,
                                  "target": "yun-chang-ding-jia-quan"})
        self.assertEqual(resp.status_code, 200)
        box = wstore.root() / ".drafts"
        new = box / "yun-chang-ding-jia-quan.md"
        self.assertTrue(new.exists(), "目标页要产出一份并入稿")
        fm, _body = wstore.parse_front(new.read_text(encoding="utf-8"))
        self.assertEqual(str(fm.get("merges") or "").strip(), "yun-chang-ding-jia-quan")
        # ★ 整页并入不许丢候选：源稿的全部候选都要在并入稿里
        ids = set(wstore._cand_ids(fm))
        self.assertEqual(ids, {c.id for c in self.cands})
        self.assertFalse((box / f"{self.slug}.md").exists(), "源稿应已归档")
        archived = list((box / ".discarded").glob(f"{self.slug}-*.md"))
        self.assertEqual(len(archived), 1, "归档要留证据（不是删掉）")

    # ------------------------------------------------------------ 4. 错也看得见
    def test_unknown_target_is_rejected_visibly(self):
        with patch("core.wiki_loop._polish_one", return_value=(True, "")):
            resp = Client().post("/wiki/approval/merge-into",
                                 {"slug": self.slug, "target": "mei-you-zhe-ye"})
        self.assertEqual(resp.status_code, 200, "不能 500")
        html = resp.content.decode("utf-8")
        self.assertIn("改判未生效", plain(html))
        self.assertIn("库里没有这一页", plain(html))
        box = wstore.root() / ".drafts"
        self.assertFalse((box / "mei-you-zhe-ye.md").exists(), "打错字不许造存草稿")
        self.assertTrue((box / f"{self.slug}.md").exists(), "失败的并入不许归档源稿")

    def test_empty_submit_says_what_is_missing(self):
        resp = Client().post("/wiki/approval/merge-into",
                             {"slug": self.slug, "target": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("缺少草稿名或目标页", plain(resp.content.decode("utf-8")))
