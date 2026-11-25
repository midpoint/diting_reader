"""第八步：页面删除（`delete` = `edits` 的空形态，2026-11-06）。

命题：删页不是新动词，是「用空替换那页」——`edits: <slug>` + `candidates: []`。
所以它天然继承第六步 `edits` 的全部护栏（人/程序独占、必须走审批、人 commit
才生效），不需要再给「删除」造一套权限。

这一组盯六件事，每一件都是「删页」自己会引入的新退化：

  1. **删页只能提案，不能直接发生**：`request_delete` 只写 `.drafts/`，原页不动。
     假如它顺手删了，人连「被删的是什么」都看不到（备份还在，但没人知道去哪找）。
  2. **空候选的豁免必须窄**：只有「`edits` + 空候选」是删除。一份**没有** edits
     的空候选草稿仍是「空页」，必须继续被 `missing_fm` 拦 —— 放宽成「空候选一律
     放行」等于把空页校验删掉，而那是这一步最容易被顺手带走的旧契约。
  3. **候选必须一并废弃**（用户 2026-11-06 拍板）：页没了而候选还活着，下次编译
     会把它们重新编进新页 —— 删掉的页原地复活，且没有任何一处报错。
  4. **顺序：备份 → 回池 → 删页**：回池失败就中止，绝不出现「页没了、候选还活着」。
  5. **删页没有候选守恒守卫**（与拆/并相反）：删的就是「这页的判断我不要了」，
     所以**不需要**先把候选搬走；搬走是「想留下判断」的人自己该做的事。
  6. **反链留断链并报数**：指向它的页不断链、不清洗，删完把「还有几页链向它」
     说出来 —— 悄悄清掉，就再没人知道谁在说一件已经不存在的事。

★ 与 `test_wiki_reorganize.py`（第六步：edits 可减）的分界：那边钉「覆盖」，这边钉
  「覆盖成空」。两套松紧都在 `_validate_draft` 一处判，测试必须各自钉住自己那一半。
"""
import json
import shutil
import tempfile

from django.test import TestCase, override_settings

from core import wiki as wstore
from core.models import IngestCandidate
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


class DeleteBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-wiki-8-")
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
        text = PAGE.format(
            title=title or slug, domain=domain, slug=slug,
            cands=json.dumps(list(cands), ensure_ascii=False),
            body=body if body is not None else f"- 一条判断（候选 {slug}）")
        p = wstore.root() / "wiki" / domain / f"{slug}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def mk_draft(self, slug, *, domain="invest", cands="[1]", extra="", body="- 一条判断"):
        text = PAGE.format(title=slug, domain=domain, slug=slug, cands=cands,
                           body=body)
        if extra:
            text = text.replace("\n---\n", "\n%s---\n" % extra, 1)
        (wstore.root() / ".drafts" / f"{slug}.md").write_text(text, encoding="utf-8")
        return text

    def draft_fm(self, slug):
        p = wstore.root() / ".drafts" / f"{slug}.md"
        return wstore.parse_front(p.read_text(encoding="utf-8"))[0]

    def draft_files(self):
        return sorted(p.name for p in (wstore.root() / ".drafts").glob("*.md"))


# ================================================================ request_delete
class RequestDeleteTest(DeleteBase):
    """删页只能**提案**：写一份 `.drafts/` 的稿，原页一个字节不动。"""

    def test_writes_an_empty_replacement_draft_and_leaves_page_alone(self):
        page = self.mk_page("moat", cands=(7, 8))
        before = page.read_text(encoding="utf-8")

        ok, payload = wstore.request_delete("moat")

        self.assertTrue(ok, payload)
        self.assertEqual(payload["candidates"], 2)
        self.assertEqual(self.draft_files(), ["moat.md"])
        fm = self.draft_fm("moat")
        self.assertEqual(str(fm["edits"]), "moat", "删页必须打 edits 标记（它是 edits 的空形态）")
        self.assertNotIn("merges", fm, "删页不是合并：带 merges 会被按「只增不减」校验")
        self.assertEqual(wstore._cand_ids(fm), [], "删页的候选必须清空")
        self.assertEqual(page.read_text(encoding="utf-8"), before, "提案阶段原页不许动")

    def test_clears_multiline_candidate_list_without_leaving_orphans(self):
        """`candidates` 可能是多行列表。只替换首行会把 `- 7` 这些续行留在文件里，
        于是「已清空」在文件里看不出来（解析器还可能把它们当成别的键的续行）。"""
        p = wstore.root() / "wiki" / "invest" / "moat.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "---\ntitle: moat\ndomain: invest\nsources: [a]\nupdated: 2026-11-06\n"
            "confidence: high\ntags: []\ncandidates:\n  - 7\n  - 8\n---\n\n## 候选清单\n\n- x\n",
            encoding="utf-8")

        ok, payload = wstore.request_delete("moat")

        self.assertTrue(ok, payload)
        text = (wstore.root() / ".drafts" / "moat.md").read_text(encoding="utf-8")
        self.assertNotIn("- 7", text, "续行没清掉 ⇒ frontmatter 里留着孤儿列表项")
        self.assertNotIn("- 8", text)
        self.assertEqual(wstore._cand_ids(self.draft_fm("moat")), [])

    def test_refuses_missing_page(self):
        ok, err = wstore.request_delete("no-such-page")
        self.assertFalse(ok)
        self.assertIn("不存在", err)

    def test_refuses_when_a_draft_with_the_same_slug_exists(self):
        """两份并存会让「哪份是最新」不可判定（同 `pull_page` 的判据）。"""
        self.mk_page("moat")
        self.mk_draft("moat", extra="edits: moat\n")
        ok, err = wstore.request_delete("moat")
        self.assertFalse(ok)
        self.assertIn("同名草稿", err)

    def test_logs_the_request(self):
        self.mk_page("moat", cands=(7,))
        wstore.request_delete("moat")
        self.assertTrue(wstore._has_log_action("request-delete"),
                        "没记 log ⇒ 事后查不到这页是什么时候被提删除的")

    def test_delete_draft_is_listed_as_delete_and_complete(self):
        """浮层要能说「删除」而不是「编辑」，且这份稿是**完整**的 —— 否则删页
        永远入不了库（第七步留下的那个停点就是这么来的）。"""
        self.mk_page("moat", cands=(7,))
        wstore.request_delete("moat")
        rows = {d["slug"]: d for d in wstore.list_drafts()}
        self.assertTrue(rows["moat"]["delete"])
        self.assertTrue(rows["moat"]["complete"], "删除稿必须是完整的")


# ================================================================ 空候选的豁免必须窄
class EmptyCandidateExemptionTest(DeleteBase):
    def test_plain_empty_draft_is_still_refused(self):
        """**没有** edits 的空候选草稿仍是「空页」，必须继续被拦。

        放宽成「空候选一律放行」等于把空页校验删掉 —— 而那是这一步最容易被顺手
        带走的旧契约。反证：这条测试在「豁免条件写成 `not _cand_ids(fm)`」时会红。
        """
        self.mk_draft("blank", cands="[]")
        ok, err = wstore.commit()
        self.assertFalse(ok)
        self.assertIn("candidates", err)
        self.assertEqual(self.draft_files(), ["blank.md"], "被拒时草稿要留着等人修")

    def test_edits_draft_with_candidates_is_still_a_replacement(self):
        """回归：`edits` 有候选 = 覆盖**不删**，第七步的拆页语义一个字没变。"""
        self.mk_page("moat", cands=(7, 8))
        self.mk_draft("moat", cands="[7]", extra="edits: moat\n")
        ok, payload = wstore.commit()
        self.assertTrue(ok, payload)
        self.assertEqual(payload["deleted"], [])
        self.assertEqual(payload["edited"], ["wiki/invest/moat.md"])
        self.assertTrue((wstore.root() / "wiki" / "invest" / "moat.md").exists())
        self.assertEqual(len(wstore._cand_ids(wstore.parse_front(
            (wstore.root() / "wiki" / "invest" / "moat.md").read_text(encoding="utf-8"))[0])), 1)


# ================================================================ 真删
class CommitDeleteTest(DeleteBase):
    def _deletable(self):
        olds = [mk_cand("老判断%d" % i, "老判断%d" % i) for i in range(2)]
        self.mk_page("moat", cands=[c.id for c in olds])
        ok, payload = wstore.request_delete("moat")
        self.assertTrue(ok, payload)
        return olds

    def test_deletes_page_draft_index_and_sources(self):
        self._deletable()
        ok, payload = wstore.commit()

        self.assertTrue(ok, payload)
        self.assertEqual(payload["deleted"], ["wiki/invest/moat.md"])
        self.assertFalse((wstore.root() / "wiki" / "invest" / "moat.md").exists(),
                         "提交后页必须真的没了")
        self.assertEqual(self.draft_files(), [], "草稿要清空，否则审批暂停永远放不开")
        index = (wstore.root() / "index.md").read_text(encoding="utf-8")
        self.assertNotIn("wiki/invest/moat.md", index, "index 里还留着行 ⇒ 点开就是 404")
        self.assertNotIn("wiki/invest/moat.md",
                         json.dumps(json.loads((wstore.root() / "meta" / "sources.json")
                                               .read_text(encoding="utf-8"))),
                         "sources.json 还指向已删的页 ⇒ 来源反查会给出幽灵页")
        self.assertTrue(wstore._has_log_action("delete-page"))

    def test_releases_candidates_as_discarded(self):
        """删页 = 页内候选一并废弃（用户 2026-11-06 拍板）。

        反证：删页而候选仍是 accepted ⇒ 下次编译会把它们重新编进新页，删掉的页
        原地复活，且没有任何一处报错。
        """
        olds = self._deletable()
        ok, payload = wstore.commit()
        self.assertTrue(ok, payload)
        self.assertEqual(sorted(payload["deleted"]), ["wiki/invest/moat.md"])
        ids = [c.id for c in olds]
        self.assertEqual(IngestCandidate.objects.filter(
            id__in=ids, status="discarded").count(), len(ids))
        self.assertEqual(sorted(payload.get("deleted")), ["wiki/invest/moat.md"])

    def test_backup_is_kept(self):
        self._deletable()
        wstore.commit()
        box = wstore.root() / ".drafts" / ".merged"
        baks = sorted(p.name for p in box.glob("moat-*.md"))
        self.assertTrue(baks, "删了没备份 ⇒ 人再也看不到当初那一页长什么样")

    def test_single_approve_also_deletes(self):
        """`approve()` 与 `commit()` 共用 `_validate_draft`，单页通过也要走删除分支。"""
        self._deletable()
        ok, payload = wstore.approve("moat")
        self.assertTrue(ok, payload)
        self.assertTrue(payload.get("deleted"))
        self.assertFalse((wstore.root() / "wiki" / "invest" / "moat.md").exists())

    def test_dangling_backlinks_are_reported_not_silently_cleaned(self):
        """指向它的页**不断链、不清洗**，删完把「还有几页链向它」说出来。

        悄悄连断链一起清掉，就再也没人知道谁在说一件已经不存在的事。
        """
        self.mk_page("other", cands=(9,), body="- 见 [[moat]] 那页")
        self._deletable()
        ok, payload = wstore.commit()
        self.assertTrue(ok, payload)
        self.assertEqual(payload["dangling"],
                         {"wiki/invest/moat.md": ["wiki/invest/other.md"]})
        bl = json.loads((wstore.root() / "wiki" / "backlinks.json").read_text(encoding="utf-8"))
        self.assertIn("moat", bl, "断链必须留在索引里，否则它永远发现不了")

    def test_delete_needs_no_candidate_conservation(self):
        """删页**没有**候选守恒守卫（与拆/并相反）：删的就是「这页的判断我不要了」。

        反证：如果照搬 `merges` 的「候选只增不减」，这里会因为「候选 7、8 没搬去
        别处」被拒 —— 那等于要求人删页前必须先搬走，与用户拍板的语义相反。
        """
        self._deletable()
        ok, err = wstore.commit()
        self.assertTrue(ok, err)


# ================================================================ 原子性与顺序
class DeleteSafetyTest(DeleteBase):
    def test_a_bad_draft_blocks_the_whole_batch_including_deletion(self):
        """批量原子性：一批里有一份坏的，删页也不能发生（一个字节不动）。"""
        olds = [mk_cand("老判断", "老判断")]
        self.mk_page("moat", cands=[c.id for c in olds])
        wstore.request_delete("moat")
        # 坏草稿：缺 tags
        (wstore.root() / ".drafts" / "bad.md").write_text(
            "---\ntitle: bad\ndomain: invest\nsources: [x]\nupdated: 2026-11-06\n"
            "confidence: high\ncandidates: [2]\n---\n## 候选清单\n\n- 一条\n",
            encoding="utf-8")

        ok, err = wstore.commit()

        self.assertFalse(ok)
        self.assertIn("bad", err)
        self.assertTrue((wstore.root() / "wiki" / "invest" / "moat.md").exists(),
                        "被拒时页必须还在")
        self.assertEqual(IngestCandidate.objects.get(id=olds[0].id).status, "accepted",
                         "被拒时候选状态也不许动")
        self.assertEqual(self.draft_files(), ["bad.md", "moat.md"])

    def test_candidate_release_failure_aborts_before_deleting(self):
        """废弃失败就中止：绝不出现「页没了、候选还活着」。

        桩打在 `core.wiki._release_candidates` 上 —— 模拟 DB 写失败（真实故障是
        锁表 / 磁盘满），断言页还在、草稿还在。
        """
        olds = [mk_cand("老判断", "老判断")]
        self.mk_page("moat", cands=[c.id for c in olds])
        wstore.request_delete("moat")

        from unittest.mock import patch
        with patch("core.wiki._release_candidates",
                   return_value=(0, "候选废弃失败（模拟）")):
            ok, payload = wstore.approve("moat")

        self.assertFalse(ok)
        self.assertIn("废弃失败", str(payload))
        self.assertIn("这一页没有删", str(payload))
        self.assertTrue((wstore.root() / "wiki" / "invest" / "moat.md").exists(),
                        "废弃失败的页绝对不能已经删掉")
        self.assertEqual(self.draft_files(), ["moat.md"], "草稿要留着，人能重试")
