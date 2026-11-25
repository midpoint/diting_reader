"""编译按钮该亮还是该灰（2026-11-03）：面板要回答「还有没有活」。

事故形态（用户原话：「已采纳 10 条但都已入库，点编译只提示都编译过了」）：

  `已采纳 N 条` 取的是 `status=accepted` 计数，**只增不减**；而「还能不能编译」
  取决于产物（草稿/已入库页的 frontmatter 里有没有这条候选）。两个口径各算一遍，
  结果就是：全编完了，那行字还是原数、按钮还是亮的、点下去 0 元 0 页 + 一句事后
  解释。它不花钱、不丢数据、不报错 —— 消耗的是信任：点一次是空的，下次就不点了；
  等真有 26 条待编译时，人也以为又是空的。

所以这里断言的是**页面**（按钮亮不亮、数字对不对），而不只是函数返回值：口径错位
这种缺陷，函数各算各的都对，错在「两个人算的是两件事」。

★ 另外两条只有真页面才暴露的同类错误（都在 `apps/wiki/views._ctx`）：
  1. 拿**截断后**的草稿列表算「已编译」，第 61 份草稿里的候选会重新变成
     「待编译」→ 数字错、点下去整批跳过。（首页草稿卡 2026-11-09 已下线，
     但 backlog 的输入仍必须扫全量。）
  2. `pages` 会被 `?src=` 过滤 —— 带筛选看页面时，被滤掉的已入库页对应的候选
     同样会「复活」。
  共同点：**输入被裁过，但没人裁「已编译」这个语义**。两条各有一个变异靶子。
"""
import shutil
import tempfile
from unittest import mock

from django.template.loader import render_to_string
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from core import ingest
from core import wiki as wstore
from core.models import IngestCandidate


def mk_cand(claim, *, status="accepted", sid="daily-2026-10-31"):
    return IngestCandidate.objects.create(
        batch="b1", source_type="report", source_id=sid, kind="decision",
        claim=claim, quote=claim, quote_from="mine",
        domain="invest", confidence="high", status=status)


def _fm(slug, cand_ids, sources=("report:daily-2026-10-31",)):
    """草稿/页的 frontmatter。解析器是个小 YAML 子集，`key: [a, b]` 够用。"""
    return "\n".join([
        "---",
        "title: %s" % slug,
        "domain: invest",
        "confidence: high",
        "updated: %s" % timezone.localdate().isoformat(),
        "sources: [%s]" % ", ".join(sources),
        "candidates: [%s]" % ", ".join(str(i) for i in cand_ids),
        "---",
        "",
        "正文（这组测试只关心 frontmatter 里的 candidates）。",
        "",
    ])


class WikiDirBase(TestCase):
    """每个用例一个空 wiki 目录：草稿/页是磁盘文件，**不会随事务回滚**。

    ★ 不清的后果不是「脏」，而是「假绿」：上一用例写的草稿让本用例的候选变成
      「已编译」，于是「按钮该亮」那条永远亮不起来、或者反过来全灰。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-backlog-")
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

    def draft(self, slug, cand_ids, sources=("report:daily-2026-10-31",)):
        (wstore.root() / ".drafts" / ("%s.md" % slug)).write_text(
            _fm(slug, cand_ids, sources), encoding="utf-8")

    def archived_draft(self, slug, cand_ids):
        d = wstore.root() / ".drafts" / ".discarded"
        d.mkdir(parents=True, exist_ok=True)
        (d / ("%s.md" % slug)).write_text(_fm(slug, cand_ids), encoding="utf-8")

    def page(self, slug, cand_ids, domain="invest", sources=("report:daily-2026-10-31",)):
        d = wstore.root() / "wiki" / domain
        d.mkdir(parents=True, exist_ok=True)
        (d / ("%s.md" % slug)).write_text(_fm(slug, cand_ids, sources), encoding="utf-8")


class BacklogCountTest(WikiDirBase):
    """`compile_backlog` —— 面板与花钱那条路**共用**的那份判据。"""

    def test_todo_excludes_candidates_already_in_a_draft(self):
        a, b, c = (mk_cand("判断%d" % i) for i in range(3))
        self.draft("p1", [a.id])
        bl = ingest.compile_backlog()
        self.assertEqual(bl["accepted"], 3)
        self.assertEqual(bl["todo"], 2)
        self.assertEqual(bl["skipped"], 1)
        self.assertEqual([x.id for x in bl["cands"]], [b.id, c.id])

    def test_all_compiled_means_nothing_to_do(self):
        a, b = mk_cand("判断A"), mk_cand("判断B")
        self.page("p1", [a.id, b.id])
        bl = ingest.compile_backlog()
        self.assertEqual((bl["todo"], bl["batch"], bl["cands"]), (0, 0, []))
        self.assertEqual(bl["skipped"], 2)

    def test_compiled_are_excluded_before_feeding(self):
        """★ 2026-11-03 的真 bug：先截 60 再剔「已编译」→ 后 5 条**永远够不着**。

        2026-11-09 上限取消后这条守卫仍然成立：验的是「已编译的先剔掉、
        剩下的**全部**喂进花钱那条路」—— 剔除口径漂了，症状依旧是
        「面板说有 5 条，编译说都编完了」。

        ★ 本用例在 `.drafts/` 放了一份「已编译」草稿来造反例；2026-11-09 用户
          澄清恢复审批暂停（有草稿时拒绝新编译），但草稿里的候选仍靠产物口径
          剔除。测试 patch 掉 approval_active 以绕过暂停闸，专测剔除逻辑。
        """
        cands = [mk_cand("判断%03d" % i) for i in range(65)]
        self.draft("done", [c.id for c in cands[:60]])
        bl = ingest.compile_backlog()
        self.assertEqual(bl["todo"], 5, "已编译的先剔掉，剩下的全量进 batch")
        self.assertEqual(bl["batch"], 5)
        self.assertEqual([x.id for x in bl["cands"]], [c.id for c in cands[60:]])
        # 同一件事在**花钱那条路**上也要成立 —— 否则面板说有 5 条，编译说「都编完了」。
        # ★ 打桩在分组之前：只数「喂进去几条」，一次模型调用都不发（这按钮真花钱）。
        # ★ 2026-11-09 用户澄清恢复审批暂停：有草稿时 compile_candidates 会被挡，
        #   所以这里 patch approval_active 让测试能跑到 plan_groups 那一层。
        with mock.patch("core.wiki.approval_active", return_value=False), \
                mock.patch.object(ingest, "plan_groups",
                               return_value=([], "打桩：到此为止，不花钱", [])) as pg:
            res = ingest.compile_candidates()
        self.assertIsNotNone(pg.call_args, "旧实现会在这里早退（认为没活），根本走不到分组")
        self.assertEqual(len(pg.call_args[0][0]), 5)
        self.assertFalse(res["ok"])

    def test_batch_consumes_all_todo(self):
        """★ 2026-11-09 用户拍板取消 60 条截断：提炼藏在一条龙里，候选一次
        消费完，别留积压。谁把截断加回来（batch < todo），这里要响。"""
        for i in range(70):
            mk_cand("判断%03d" % i)
        bl = ingest.compile_backlog()
        self.assertEqual(bl["todo"], 70, "面板要如实说总共还有多少")
        self.assertEqual(bl["batch"], 70, "一次全吃进去，不截断")
        self.assertEqual(len(bl["cands"]), 70, "数字对了候选对象也要真在")

    def test_only_discarded_candidates_are_excluded(self):
        """★ 候选免审（2026-11-05）：**未丢弃即编译**。

        改动前这条断言写死成 `== 1`（只有 accepted 算），改动后 pending 也进
        编译。语义变了，期望跟着变；顺带断言 pending 那条**真的在 cands 里**，
        不只是数字对 —— 数字对了但候选没进去，是最容易蒙混过关的一种假绿。
        """
        a = mk_cand("采纳的", status="accepted")
        mk_cand("丢掉的", status="discarded")
        p = mk_cand("待审的", status="pending")
        bl = ingest.compile_backlog()
        self.assertEqual(bl["accepted"], 2)
        self.assertEqual({c.id for c in bl["cands"]}, {a.id, p.id})

    def test_discarded_draft_does_not_hold_a_candidate(self):
        """退稿的意思是「这一次的结果我不要」→ 那批候选应当**能重新编译**。

        把 `.drafts/.discarded/` 也算「已编译」，等于退稿变成永久占位，而且没有
        任何提示（记忆里的「静默失败最贵」）。
        """
        a = mk_cand("判断A")
        self.archived_draft("old", [a.id])
        self.assertEqual(ingest.compile_backlog()["todo"], 1)

    def test_source_filter_scopes_the_backlog(self):
        mk_cand("A", sid="daily-2026-10-31")
        mk_cand("B", sid="daily-2026-11-01")
        bl = ingest.compile_backlog(source=("report", "daily-2026-11-01"))
        self.assertEqual((bl["accepted"], bl["todo"]), (1, 1))


class PanelTest(WikiDirBase):
    """页面：按钮亮不亮、数字对不对。这才是人看见的东西。"""

    def html(self, query=""):
        return Client().get("/wiki" + query).content.decode("utf-8")

    def assert_button_live(self, html):
        self.assertIn('hx-post="/wiki/compile"', html, "有待编译时必须给可点的编译入口")
        self.assertNotIn('type="button" disabled', html)

    def assert_button_dead(self, html):
        self.assertNotIn('hx-post="/wiki/compile"', html,
                         "没活了就不该再摆一个点了没用的花钱按钮")
        self.assertIn('type="button" disabled', html, "按钮要灰着而不是消失（消失像坏了）")
        self.assertIn("都已变成页", html, "要说明为什么不能点")

    def test_panel_shows_todo_and_enabled_button(self):
        a, b = mk_cand("判断A"), mk_cand("判断B")
        self.page("p", [a.id])
        html = self.html()
        self.assertIn("1 条待编译", html, "说的是「还有多少活」，不是「采纳了多少」")
        self.assert_button_live(html)
        self.assertNotIn("都已变成页", html)

    def test_panel_disables_button_when_nothing_left(self):
        a, b = mk_cand("判断A"), mk_cand("判断B")
        self.page("p", [a.id, b.id])
        html = self.html()
        self.assert_button_dead(html)

    def test_panel_pauses_compile_while_drafts_await_approval(self):
        """审批期（.drafts/ 非空）按钮必须灰（2026-11-09 用户澄清恢复）。

        草稿待审批时不该开新一轮编译，否则草稿越堆越多、审批上下文被打散。
        编译中由 compile_run.running 挡，编译后待审批由 approval_active 挡。
        """
        a, b = mk_cand("判断A"), mk_cand("判断B")
        self.draft("p", [a.id])          # 审批期 + b 还待编译
        html = self.html()
        self.assertNotIn('hx-post="/wiki/compile"', html,
                         "审批期间不该摆一个点了必被拒的花钱按钮")
        self.assertIn('type="button" disabled', html)
        self.assertIn("份草稿待审批", html, "要灰着并说清为什么")
        self.assertIn("打开审批浮层", html, "下一步的入口要在")
        # 反证：草稿裁决完（入库）后，同一条候选不再挡编译
        shutil.rmtree(self.tmp, ignore_errors=True)
        wstore.ensure_dirs()
        self.page("p", [a.id])
        self.assert_button_live(self.html())

    def test_panel_offers_the_approve_link_when_drafts_exist(self):
        """草稿待审批但已无活：按钮灰，审批入口要在。

        「没活」的文案要指出有草稿待审批 —— 不能让人以为这批候选凭空消失了；
        同时审批浮层入口是下一步该去的地方。
        """
        a = mk_cand("判断A")
        self.draft("p", [a.id])
        html = self.html()
        self.assertNotIn('hx-post="/wiki/compile"', html)
        self.assertIn('type="button" disabled', html)
        self.assertIn("打开审批浮层", html)
        self.assertIn("份草稿待审批", html, "要说清为什么按钮灰着")
        # 已入库时没有草稿可审批，就别摆这个入口
        shutil.rmtree(self.tmp, ignore_errors=True)
        wstore.ensure_dirs()
        self.page("p", [a.id])
        html = self.html()
        self.assert_button_dead(html)
        self.assertNotIn("打开审批浮层", html)

    def test_panel_button_live_with_new_traces_but_zero_todo(self):
        """★ 2026-11-09 修的卡死点：候选全编完（todo=0）但有新痕迹 → 按钮必须亮。

        这枚按钮是「提炼+编译」一条龙的唯一入口。只看待编译候选就把按钮灰掉，
        新痕迹永远提炼不了 —— 页面 200、按钮灰着、理由还写着「都已变成页」，
        人只会以为没活了，实际是流水线断了。
        """
        from core.models import Annotation
        a = mk_cand("判断A")
        self.page("p", [a.id])           # 候选全编完，todo=0
        Annotation.objects.create(source_type="entry", source_id="9999",
                                  body="我认为这条新痕迹值得沉淀")
        html = self.html()
        self.assert_button_live(html)
        self.assertIn("1 处新痕迹", html, "要告诉人点下去会先提炼")

    def test_panel_counts_drafts_beyond_the_display_cap(self):
        """★ 变异靶子 1：拿**截断后**的草稿列表算「已编译」。

        候选写在**第 61 份**草稿里（slug 排序最后）。拿截断后的列表算，
        这条候选就「复活」成待编译 —— 数字错、点下去整批跳过。
        （首页草稿卡 2026-11-09 已下线，但 backlog 仍必须扫全量草稿。）
        """
        a = mk_cand("判断A")
        for i in range(61):
            self.draft("draft-%03d" % i, [a.id] if i == 60 else [])
        html = self.html()
        self.assertIn("编译 · 0 条待编译", html)
        self.assertNotIn('hx-post="/wiki/compile"', html)

    def test_src_filter_does_not_resurrect_compiled_candidates(self):
        """★ 变异靶子 2：`?src=` 会把 `pages` 过滤掉。

        被滤掉的已入库页，它的候选不能因此变回「待编译」。
        """
        a, b = mk_cand("判断A"), mk_cand("判断B")
        self.page("p", [a.id, b.id], sources=("report:daily-2026-10-31",))
        html = self.html("?src=report:no-such-source")
        self.assert_button_dead(html)

    def test_compile_message_matches_the_panel(self):
        """点下去以后，那条消息说的要和面板说的一致（同一个判据的两种呈现）。"""
        a = mk_cand("判断A")
        self.page("p", [a.id])
        resp = Client().post("/wiki/compile")
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode("utf-8")
        self.assertIn("都已经编译过", body)
        self.assert_button_dead(body)

    def test_missing_backlog_fails_open_not_closed(self):
        """★ 上下文里没有 `compile_backlog` 时必须**保留**编译入口。

        真事故（2026-11-03，本轮当场踩到）：dev server 用 `--noreload` 跑着旧 Python，
        模板已经是新的 → `compile_backlog` 不在上下文里 → Django 把未定义变量当成
        空值 → 判据若写成 `{% if not compile_backlog.todo %}` 就为真 → 悄悄走进
        「都已变成页」分支：HTTP 200、没有报错、日志干净，而 36 条里 26 条没编译。
        这就是「兜底分支安静骗人」：**兜底的方向写错了，比没有兜底更糟**。

        所以判据是 `compile_backlog.todo == 0`：'' 不等于 0 → 落到「有活」分支。
        宁可多摆一个可点的按钮（点一次最多被告知没事可做），也不要静默关掉
        所有编译入口 —— 后者没有任何信号，人只会以为「功能坏了」或「没活了」。
        """
        html = render_to_string("wiki/_body.html", {
            "ingest_stats": {"accepted": 3, "discarded": 0, "pending": 0},
            "processed_groups": [{"title": "x", "source_type": "report",
                                  "source_id": "d", "source_url": "", "cands": []}],
            "stats": {"drafts": 0, "pages": 0, "low_confidence": 0, "raw": 0,
                      "domains": []},
            "drafts": [], "groups": [], "log_rows": [], "ingest_rows": [],
            "cand_groups": [], "candidates": [],
        })
        self.assertIn('hx-post="/wiki/compile"', html, "缺上下文时必须 fail-open")
        self.assertNotIn("都已变成页", html, "不能凭空宣布「没活了」")
