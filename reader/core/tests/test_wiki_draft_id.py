"""草稿稳定编号 `draft_id` 的反证（2026-11-13 用户拍板）。

位置编号 D1/D2/D3 随合并 / 拆分 / 重排跳变：人说的「D2」下一轮就指了别的稿。
改成类比候选自增 id 的稳定编号后，这里钉死四条不变量，每条都构造能暴露
跳变 / 重号 / 越权的反例（只写期望值不构造反例，守卫写坏了照样全绿）：

  1. 号随稿走：合并保留目标稿的号、拆分新页拿新号、改名 / 重排都不跳变；
  2. 永不重号：退稿归档后新稿拿更大的号；计数器丢了也从已知最大号续；
  3. 入库剥掉：页用 slug 定位，draft_id 不留在人读的页里；
  4. agent 改不动：draft_id 进 LOCKED_FM，拒写时一个字节都不动。

★ 补齐只发生在展示入口（`wa.read("drafts")`）；`list_drafts()` 保持纯读，
  所以守卫的字节快照（`snapshot()`）不会被补号动作污染。
"""
from unittest.mock import patch

from core import ingest
from core import wiki as wstore
from core import wiki_agent as wa
from core import wiki_loop
from core.tests import test_wiki_agent as ta

DOMAIN = ta.DOMAIN


class DraftIdBase(ta.WikiAgentBase):
    def did(self, slug):
        """磁盘上某份草稿的 draft_id（不是列表位置）。"""
        fm = wstore.parse_front(
            (wstore.root() / ".drafts" / f"{slug}.md").read_text(encoding="utf-8"))[0]
        return wstore._draft_id(fm)

    def ids_by_slug(self):
        return {d["slug"]: d["draft_id"] for d in wstore.list_drafts()}

    def mk_page(self, slug, ids, **kw):
        d = wstore.root() / "wiki" / DOMAIN
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{slug}.md"
        p.write_text(ta._draft_text(ids, **kw), encoding="utf-8")
        return p


class BackfillTest(DraftIdBase):
    def test_list_drafts_is_pure_read(self):
        """list_drafts 不补号：编译扫描 / 守卫快照调它不该有副作用。"""
        self.mk_draft("a", [self.mk_cand(1).id])
        before = self.snapshot()
        rows = wstore.list_drafts()
        self.assertIsNone(rows[0]["draft_id"], "纯读：缺号给 None，不偷偷写盘")
        self.assertEqual(self.snapshot(), before)

    def test_backfill_assigns_persists_and_is_idempotent(self):
        self.mk_draft("a", [self.mk_cand(1).id])
        self.mk_draft("b", [self.mk_cand(2).id])
        self.assertEqual(wstore.ensure_draft_ids(), 2)
        self.assertEqual(self.did("a"), 1)
        self.assertEqual(self.did("b"), 2)
        # 落盘：号在 frontmatter 里，不是只活在内存
        fm = wstore.parse_front(
            (wstore.root() / ".drafts" / "a.md").read_text(encoding="utf-8"))[0]
        self.assertEqual(fm.get("draft_id"), "1")
        before = self.snapshot()
        self.assertEqual(wstore.ensure_draft_ids(), 0, "已有号一律不动")
        self.assertEqual(self.snapshot(), before, "幂等：第二次一个字节都不写")

    def test_read_drafts_materializes_and_exposes_id(self):
        """展示入口（浮层 / briefing 共用）补号并把 draft_id 透给 agent。"""
        self.mk_draft("a", [self.mk_cand(1).id])
        res = wa.read("drafts")
        self.assertTrue(res["ok"])
        self.assertEqual(res["drafts"][0]["draft_id"], 1)
        self.assertEqual(self.did("a"), 1, "read 之后号已落盘")

    def test_id_follows_slug_not_list_position(self):
        """★ 核心反证：插入一份排前面的稿，旧稿的 D 编号不许跟着位置跳。"""
        self.mk_draft("a", [self.mk_cand(1).id])
        self.mk_draft("z", [self.mk_cand(2).id])
        wa.read("drafts")
        self.assertEqual(self.ids_by_slug(), {"a": 1, "z": 2})
        # 缺字段的稿排最前（list_drafts 的排序判据）：位置变了，号不能变
        p = wstore.root() / ".drafts" / "m.md"
        p.write_text(ta._draft_text([self.mk_cand(3).id]).replace(
            f"domain: {DOMAIN}\n", ""), encoding="utf-8")
        wa.read("drafts")
        order = [d["slug"] for d in wstore.list_drafts()]
        self.assertEqual(order[0], "m", "缺字段稿确实排到了最前")
        self.assertEqual(self.ids_by_slug(), {"m": 3, "a": 1, "z": 2},
                         "a/z 的号随稿走，不因 m 插到前面而跳变")


class NeverReuseTest(DraftIdBase):
    def test_new_draft_after_discard_gets_higher_id(self):
        a = self.mk_cand(1)
        self.mk_draft("a", [a.id])
        self.mk_draft("b", [self.mk_cand(2).id])
        wa.read("drafts")
        ok, _ = wa.set("discard", {"slug": "a"})
        self.assertTrue(ok)
        self.mk_draft("c", [self.mk_cand(3).id])
        wa.read("drafts")
        self.assertEqual(self.did("c"), 3, "退稿的号 1 不许被新稿复用")
        archived = list((wstore.root() / ".drafts" / ".discarded").glob("a-*.md"))
        self.assertTrue(archived)
        self.assertEqual(wstore._draft_id(
            wstore.parse_front(archived[0].read_text(encoding="utf-8"))[0]), 1,
            "归档稿保留原号（计数器兜底要扫得到它）")

    def test_counter_seeds_from_existing_when_seq_lost(self):
        self.mk_draft("a", [self.mk_cand(1).id])
        wa.read("drafts")
        (wstore.root() / "meta" / "draft_seq.json").unlink()
        self.mk_draft("b", [self.mk_cand(2).id])
        wa.read("drafts")
        self.assertEqual(self.did("b"), 2, "计数器丢了也从已知最大号 +1 续，不重号")

    def test_counter_seed_scans_discarded_archive(self):
        self.mk_draft("a", [self.mk_cand(1).id])
        wa.read("drafts")
        ok, _ = wa.set("discard", {"slug": "a"})
        self.assertTrue(ok)
        (wstore.root() / "meta" / "draft_seq.json").unlink()
        self.mk_draft("b", [self.mk_cand(2).id])
        wa.read("drafts")
        self.assertEqual(self.did("b"), 2, "归档里的号 1 也算已知，新稿拿 2")


class IdentityAcrossOpsTest(DraftIdBase):
    def test_merge_into_existing_draft_keeps_dst_id(self):
        a, b = self.mk_cand(1), self.mk_cand(2)
        self.mk_draft("p-a", [a.id])
        self.mk_draft("p-b", [b.id])
        wa.read("drafts")
        dst_id = self.did("p-b")
        ok, payload = wa.set("merge", {"pages": ["p-a", "p-b"], "to": "p-b"})
        self.assertTrue(ok, payload)
        self.assertEqual(self.did("p-b"), dst_id, "合并保留目标稿的号")
        self.assertFalse((wstore.root() / ".drafts" / "p-a.md").exists())

    def test_merge_to_new_slug_gives_fresh_id(self):
        a, b = self.mk_cand(1), self.mk_cand(2)
        self.mk_draft("p-a", [a.id])
        self.mk_draft("p-b", [b.id])
        wa.read("drafts")
        ok, payload = wa.set("merge", {"pages": ["p-a", "p-b"], "to": "p-c"})
        self.assertTrue(ok, payload)
        wa.read("drafts")
        self.assertEqual(self.did("p-c"), 3, "新 slug = 新身份，拿新号")

    def test_split_keeps_original_id_and_new_page_gets_fresh(self):
        a, b = self.mk_cand(1), self.mk_cand(2)
        self.mk_draft("old", [a.id, b.id])
        wa.read("drafts")
        old_id = self.did("old")
        ok, payload = wa.set("candidates", {
            "move": [b.id], "from": "old", "to": "new", "title": "新页"})
        self.assertTrue(ok, payload)
        wa.read("drafts")
        self.assertEqual(self.did("old"), old_id, "拆页后原稿号不变")
        self.assertEqual(self.did("new"), 2, "拆出的新页拿新号")

    def test_rename_keeps_id(self):
        self.mk_draft("p", [self.mk_cand(1).id])
        wa.read("drafts")
        ok, payload = wa.set("title", {"slug": "p", "title": "新标题"})
        self.assertTrue(ok, payload)
        self.assertEqual(payload["slug"], "xin-biao-ti")
        self.assertEqual(self.did("xin-biao-ti"), 1, "改名 = 同一份稿，号跟着走")


class ApproveStripsTest(DraftIdBase):
    def test_new_page_approve_strips_draft_id(self):
        self.mk_draft("p", [self.mk_cand(1).id])
        wa.read("drafts")
        ok, payload = wstore.approve("p")
        self.assertTrue(ok, payload)
        fm = wstore.parse_front(
            (wstore.root() / payload["page"]).read_text(encoding="utf-8"))[0]
        self.assertNotIn("draft_id", fm, "页用 slug 定位，编号不留在库里")

    def test_merge_approve_strips_draft_id(self):
        a, b = self.mk_cand(1), self.mk_cand(2)
        self.mk_page("t", [a.id])
        (wstore.root() / ".drafts" / "t.md").write_text(
            ta._draft_text([a.id, b.id]).replace(
                "tags: []", "tags: []\nmerges: t"), encoding="utf-8")
        wa.read("drafts")
        ok, payload = wstore.approve("t")
        self.assertTrue(ok, payload)
        fm = wstore.parse_front(
            (wstore.root() / payload["page"]).read_text(encoding="utf-8"))[0]
        self.assertNotIn("draft_id", fm)
        self.assertNotIn("merges", fm)

    def test_edit_approve_strips_draft_id(self):
        a = self.mk_cand(1)
        self.mk_page("e", [a.id])
        ok, _ = wstore.pull_page("e")
        self.assertTrue(ok)
        wa.read("drafts")
        self.assertIsNotNone(self.did("e"), "拉出来的编辑稿也要有号")
        ok, payload = wstore.approve("e")
        self.assertTrue(ok, payload)
        fm = wstore.parse_front(
            (wstore.root() / payload["page"]).read_text(encoding="utf-8"))[0]
        self.assertNotIn("draft_id", fm)
        self.assertNotIn("edits", fm)


class LockedTest(DraftIdBase):
    def test_agent_cannot_set_draft_id_via_payload(self):
        self.mk_draft("p", [self.mk_cand(1).id])
        wa.read("drafts")
        before = self.snapshot()
        ok, err = wa.set("title", {"slug": "p", "title": "x", "draft_id": 99})
        self.assertFalse(ok)
        self.assertIn("draft_id", err)
        self.assertEqual(self.snapshot(), before, "拒写时一个字节都不能动")
        self.assertEqual(self.did("p"), 1)

    def test_agent_cannot_set_draft_id_via_fields(self):
        self.mk_draft("p", [self.mk_cand(1).id])
        wa.read("drafts")
        before = self.snapshot()
        ok, err = wa.set("frontmatter", {"slug": "p", "fields": {"draft_id": 99}})
        self.assertFalse(ok)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.did("p"), 1)


class BriefingTest(DraftIdBase):
    def test_briefing_shows_stable_id_not_position(self):
        self.mk_draft("a", [self.mk_cand(1).id])
        self.mk_draft("z", [self.mk_cand(2).id])
        wa.read("drafts")
        text = wiki_loop._briefing()
        self.assertIn("D1 a", text)
        self.assertIn("D2 z", text)
        # 插一份排前面的稿：briefing 里 a 仍是 D1（位置编号会把它挤成 D2）
        p = wstore.root() / ".drafts" / "m.md"
        p.write_text(ta._draft_text([self.mk_cand(3).id]).replace(
            f"domain: {DOMAIN}\n", ""), encoding="utf-8")
        text = wiki_loop._briefing()
        self.assertIn("D1 a", text)
        self.assertIn("D3 m", text)


class CompileChainTest(DraftIdBase):
    def test_compose_page_merge_into_draft_carries_id(self):
        a, b = self.mk_cand(1), self.mk_cand(2)
        old_page = {"slug": "t", "domain": DOMAIN, "title": "T", "draft": True,
                    "fm": {"title": "T", "domain": DOMAIN, "draft_id": "5",
                           "candidates": [str(a.id)], "sources": ["entry:1"]},
                    "body": "旧结论"}
        group = {"cand_ids": [b.id], "title": "T", "domain": DOMAIN,
                 "tags": [], "slug": "t"}
        text, slug, _drop, err = ingest._compose_page(group, {b.id: b}, "", old_page)
        self.assertFalse(err)
        fm = wstore.parse_front(text)[0]
        self.assertEqual(fm.get("draft_id"), "5", "并入待审批草稿 = 同名覆盖，号要续")

    def test_compose_page_merge_into_page_has_no_id(self):
        a, b = self.mk_cand(1), self.mk_cand(2)
        old_page = {"slug": "t", "domain": DOMAIN, "title": "T", "draft": False,
                    "fm": {"title": "T", "domain": DOMAIN,
                           "candidates": [str(a.id)], "sources": ["entry:1"]},
                    "body": "旧结论"}
        group = {"cand_ids": [b.id], "title": "T", "domain": DOMAIN,
                 "tags": [], "slug": "t"}
        text, _slug, _drop, err = ingest._compose_page(group, {b.id: b}, "", old_page)
        self.assertFalse(err)
        self.assertNotIn("draft_id", wstore.parse_front(text)[0],
                         "并入已入库页 = 新合并稿，号由展示入口新发")

    def test_merge_draft_into_pulls_page_with_fresh_id(self):
        """★ 2026-11-13：并入 = 从已入库页拉一份**新草稿**，身份是新发的号。

        旧实现原地改写源稿、沿用源稿号：D1 的号在「新页」和「AI泡沫替换稿」
        两个含义之间跳，人说的 D1 到底指哪一页变得不可判定。
        """
        a, b = self.mk_cand(1), self.mk_cand(2)
        self.mk_draft("src", [a.id])
        self.mk_page("tgt", [b.id])
        wa.read("drafts")
        self.assertEqual(self.did("src"), 1)
        with patch("core.wiki_loop._polish_one", return_value=(True, "")):
            ok, payload = ingest.merge_draft_into("src", "tgt")
        self.assertTrue(ok, payload)
        self.assertEqual(payload["draft"], "tgt")
        self.assertEqual(payload["archived"], ["src"])
        self.assertFalse((wstore.root() / ".drafts" / "src.md").exists(),
                         "整份并入后源稿归档，不留在审批队列里")
        fm = wstore.parse_front(
            (wstore.root() / ".drafts" / "tgt.md").read_text(encoding="utf-8"))[0]
        self.assertEqual(fm.get("merges"), "tgt")
        self.assertEqual(wstore._draft_id(fm), 2, "拉入的稿拿新号，不沿用源稿的 1")
        self.assertEqual(set(wstore._cand_ids(fm)), {a.id, b.id})

    def test_second_merge_into_same_target_edits_the_same_draft(self):
        """★★ 用户实测的 bug：D1、D8 先后并入「AI泡沫」出两份同名稿、内容各缺一半。

        第二次必须**在已有并入稿里编辑**（候选取并集），而不是从已入库页再拉
        一份；源稿归档，审批队列里只剩一份 merges: tgt。
        """
        a, b, c = self.mk_cand(1), self.mk_cand(2), self.mk_cand(3)
        self.mk_draft("src-1", [a.id])
        self.mk_draft("src-2", [b.id])
        self.mk_page("tgt", [c.id])
        wa.read("drafts")
        with patch("core.wiki_loop._polish_one", return_value=(True, "")) as polish:
            ok, first = ingest.merge_draft_into("src-1", "tgt")
            self.assertTrue(ok, first)
            ok, second = ingest.merge_draft_into("src-2", "tgt")
        self.assertTrue(ok, second)
        self.assertEqual(second["draft"], "tgt")
        self.assertTrue(second["existing"], "第二次应识别出已有并入稿")
        self.assertEqual(second["archived"], ["src-2"])
        self.assertFalse((wstore.root() / ".drafts" / "src-1.md").exists())
        self.assertFalse((wstore.root() / ".drafts" / "src-2.md").exists())

        rows = [d for d in wstore.list_drafts() if d["merges"] == "tgt"]
        self.assertEqual([d["slug"] for d in rows], ["tgt"],
                         "同一目标只许一份并入稿")
        fm = wstore.parse_front(
            (wstore.root() / ".drafts" / "tgt.md").read_text(encoding="utf-8"))[0]
        self.assertEqual(set(wstore._cand_ids(fm)), {a.id, b.id, c.id},
                         "第二批候选要并进同一份稿，不能互相覆盖")
        self.assertEqual(self.did("tgt"), 3, "已有并入稿的号不变（不是每次新建）")
        # 第一次新建 + 第二次追加 = 两次重编；第二次没有再拉目标页
        self.assertEqual([c.args[0] for c in polish.call_args_list],
                         ["tgt", "tgt"])

    def test_pull_page_and_request_delete_get_fresh_id(self):
        a = self.mk_cand(1)
        self.mk_page("e", [a.id])
        ok, _ = wstore.pull_page("e")
        self.assertTrue(ok)
        wa.read("drafts")
        self.assertEqual(self.did("e"), 1)
        ok, _ = wstore.approve("e")
        self.assertTrue(ok)
        ok, _ = wstore.request_delete("e")
        self.assertTrue(ok)
        wa.read("drafts")
        self.assertEqual(self.did("e"), 2, "删页稿是新身份，拿新号（不复用编辑稿的 1）")