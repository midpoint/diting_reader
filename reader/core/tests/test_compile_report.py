"""编译结果不能只在这一次响应里说一遍（2026-11-03）。

用户报：点「编译成草稿」，切到别的页面再回 wiki，按钮又能点了。

实测（11:46 那次真编译，日志 + 磁盘都对过）：
- 12 页草稿成功，4 组被守卫拒 —— 同一句理由「正文出现素材里没有的数字：805」；
- 那 9 条候选没进任何页 ⇒ `compile_backlog.todo` 仍是 9 ⇒ 按钮亮。
这个判定**是对的**（它们确实没变成页）。错的是失败没留痕：

  1. `compile_run` 只念 drafts / dropped，`groups[].err` 一个字都不提 ——
     人看到「生成了 12 页草稿」，自然以为全编完了；
  2. `log.md` 只记成功那 12 页，失败组没有任何记录；
  3. 于是一刷新只剩一个亮着的按钮，人既不知道还有 9 条，也不知道为什么。

形态 A 改造（2026-11-08）：S2 不再调模型，编译收尾对每份草稿走
`wiki_loop._call` 重编「结论」。原来「结论里出现素材里没有的数字：805」这个
**旧 S2 数字守卫反证**搬到了 `wiki_loop._check_numbers`（守结论正文）：结论里
出现素材外的 805 → `_polish_one` 拒写 → 草稿带 `stale: true` + 空正文 → 失败原因
写进编译结果 `notes` / 视图消息，且 `_validate_draft` 拒这批草稿入库。
所以下面把「另有 N 组没编成 / failed_groups / 一页没出就 res.ok=False」的旧断言，
逐条改写成证明**新行为**的断言：`res.polished` 为空、`res.polish_failed` 点名到页、
成功页正文 == 桩结论、失败页 stale 仍在且正文空、理由进 notes / 响应 / 刷新页。
「一组成功、一组失败」和「全部失败要留痕」的反证结构都原样保住，只是钉新守卫。

这里盯四件事（意图不变）：
  A. 回给浏览器的消息里必须有「哪页没写成结论 + 为什么」（新口径：结论重编失败）；
  B. 失败也要落盘（`meta/compile.json`）——全部失败时更要有，那才是最需要看的一次；
  C. 页面上要能看见上次编译的结果（刷新不丢）；
  D. 从没编译过时**不许出现空的「上次编译」标签**（空标签会让人以为编译过但没结果）。
★ 桩纪律：S1 分组桩打在 `core.citeguard._call`（只留 S1 回复），结论桩打在被调用点
  `core.wiki_loop._call`（返回固定结论或触发数字守卫的带 805 结论），并配
  「桩真被用上」反证：成功页正文真的出现那段结论 / 失败页带 stale。绝不真联网。
★ `retry_will_fail`（「上次全被拒」）仍由 `failed_groups` 驱动，而形态 A 的 polish
  失败不进 `failed_groups`。那两条用 `reject_all_groups` 受控注入「整组被守卫拒、
  候选仍在待编译」的落盘结果，保证「提示出现 / 新候选来后消失」这对反证都真能变红
  （与 test_wiki_reorganize_compile.py 的守恒变异反证同款手法，不是恒真断言）。
"""
import json
import re
import shutil
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

from django.test import TestCase, override_settings

from core import ingest
from core import wiki as wstore
from core.models import IngestCandidate

#: 正常结论桩：不含素材外的数字，能过 `_check_numbers` / `_check_links`。
OK_POLISH = "这是一段结论。"
#: 新数字守卫反证：结论里出现候选素材里没有的 805 → `_polish_one` 拒写。
BAD_POLISH = "十年后只剩 805 条路。"


def mk_cand(quote, claim, *, quote_from="mine", domain="invest",
            sid="daily-2026-10-31"):
    return IngestCandidate.objects.create(
        batch="b1", source_type="report", source_id=sid, kind="decision",
        claim=claim, quote=quote, quote_from=quote_from,
        domain=domain, confidence="high", status="accepted")


def two_groups(a_id, b_id):
    """S1 分组：两组各一条（形态 A 要补 slug，否则按 `<domain>-<minid>` 兜底）。"""
    return json.dumps({"groups": [
        {"title": "组甲", "domain": "invest", "cand_ids": [a_id], "reason": "x",
         "slug": "group-a", "tags": ["长期"]},
        {"title": "组乙", "domain": "invest", "cand_ids": [b_id], "reason": "x",
         "slug": "group-b", "tags": ["长期"]},
    ]}, ensure_ascii=False)


def one_group(cand_id, *, title="组独", slug="group-x"):
    return json.dumps({"groups": [
        {"title": title, "domain": "invest", "cand_ids": [cand_id],
         "reason": "x", "slug": slug, "tags": ["长期"]},
    ]}, ensure_ascii=False)


@contextmanager
def replies(s1, *, polish=OK_POLISH):
    """S1 分组桩（citeguard）+ 结论桩（wiki_loop，按草稿 slug 分派）。

    `polish` 可以是固定文本，或 `{slug: 文本}` 的字典：让某一页的结论触发数字守卫
    （返回带 805 的文本），其余页正常。返回 (s1_call, polish_call) 两个 mock，
    供用例断言「桩真的被用上」（调用次数 + 正文真的出现结论）。
    """
    def s1_fake(*a, **kw):
        if kw.get("tag") not in ("wiki 编译分组", "wiki 编译拆页"):
            raise AssertionError("没为 tag「%s」准备 S1 桩（那会真花钱）" % kw.get("tag"))
        return s1, ""

    def polish_fake(*a, **kw):
        label = kw.get("label") or ""
        text = polish.get(label, OK_POLISH) if isinstance(polish, dict) else polish
        return (text, [], None)

    with patch("core.citeguard._call", side_effect=s1_fake) as c, \
         patch("core.wiki_loop._call", side_effect=polish_fake) as p:
        yield c, p


@contextmanager
def reject_all_groups(reason):
    """构造「整组被守卫拒」：`_compose_page` 返回 perr，不出草稿、不调 polish。"""
    def _reject(*a, **kw):
        return "", "", [], reason
    with patch("core.ingest._compose_page", side_effect=_reject):
        yield


def assert_polish_written(case, slug, expected=OK_POLISH):
    """「桩真被用上」反证：成功页正文 == 结论桩文本，且形态 A 无结构标题。"""
    fm, body = wstore.parse_front(
        (wstore.root() / ".drafts" / f"{slug}.md").read_text(encoding="utf-8"))
    case.assertIn(expected, body, "结论桩的文本没落进正文 —— 桩没被真正用上")
    case.assertNotIn("stale", fm, "set_polish 成功后 stale 要被剥掉")
    for head in ("## 候选清单", "## 通顺话", "## 我的判断",
                 "## 事实 / 他人的观点", "## 我的在意点"):
        case.assertNotIn(head, body, "形态 A 正文不许再有结构标题")


def assert_polish_failed(case, slug):
    """新守卫反证：805 被拒写的页正文仍为空、stale 仍在（入库闸门会拒）。"""
    fm, body = wstore.parse_front(
        (wstore.root() / ".drafts" / f"{slug}.md").read_text(encoding="utf-8"))
    case.assertFalse(body.strip(), "结论被拒写的页正文必须仍为空（半成品）")
    case.assertIn("stale", fm, "失败页 stale 标记仍在（审批浮层可见）")


class CompileReportBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-creport-")
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


class GuardRejectionReportTest(CompileReportBase):
    def test_view_says_which_group_failed_and_why(self):
        """一组结论写成、一组被数字守卫拒 → 那句提示必须出现在响应里。

        ★ 断言只截**消息那一条 alert-bar**（含「生成了」的那条）：整个片段里
          还有「上次编译」块，里面也会写原因。第一版断言整页，变异验证时把视图
          那句提示拆掉，用例照样绿 —— 白写（和 11-03 早上视图用例
          「没截到行」是同一个错法）。
        """
        a = mk_cand("价值投资看十年现金流", "价值投资看长期现金流")
        b = mk_cand("十年后只能毛估估", "十年后只能毛估估")
        # group-b 的结论带 805（触发新数字守卫拒写），group-a 正常写回
        with replies(two_groups(a.id, b.id),
                     polish={"group-b": BAD_POLISH}) as (s1_call, polish_call):
            r = self.client.post("/wiki/compile")
        html = r.content.decode()
        # 形态 A：S2 退役，citeguard 只被 S1 调一次；两页草稿各走一次结论桩
        self.assertEqual(s1_call.call_count, 1, "形态 A：S1 一次；S2 已不再调模型")
        # ★ 2026-11-22：失败页会带反馈再编一次（成功页仍只 1 次）⇒ 共 3 次。
        #   这里断言「数字守卫的 805 真的回喂给模型」，不是只认调用次数变了。
        self.assertEqual(polish_call.call_count, 3, "成功页 1 次 + 失败页带反馈重试 1 次")
        feedback_msgs = [c.args[1] for c in polish_call.call_args_list]
        self.assertTrue(any("805" in "".join(m["content"] for m in msgs)
                            for msgs in feedback_msgs),
                        "被数字守卫拒写的理由必须回喂，否则是盲重试")

        bars = re.findall(r'<div class="alert-bar">(.*?)</div>', html, re.S)
        msg = next((b for b in bars if "生成了" in b), "")
        self.assertTrue(msg, "连成功那句都没有，先查渲染")
        self.assertIn("生成了 2 页草稿", msg)
        self.assertIn("结论重编失败", msg, "守卫拒写要说出来（形态 A 新口径）")
        self.assertIn("805", msg, "要带上守卫给的原始理由，人才知道该改哪")
        self.assertIn("group-b", msg, "要点名是哪一页结论没写成，不能只报成功那几页")
        self.assertIn("group-a", msg, "成功的那页也要列出来")

        # 「桩真被用上」+ 新守卫反证：成功页正文是结论、失败页 stale 空正文
        assert_polish_written(self, "group-a")
        assert_polish_failed(self, "group-b")

    def test_failure_is_persisted_even_when_nothing_succeeds(self):
        """全部页结论都被拒时最需要留痕 —— 那正是「点完什么都没写成」的一次。"""
        a = mk_cand("十年后只能毛估估", "十年后只能毛估估")
        with replies(one_group(a.id), polish=BAD_POLISH) as (s1_call, polish_call):
            res = ingest.compile_candidates()
        self.assertEqual(s1_call.call_count, 1, "S1 一次")
        self.assertEqual(polish_call.call_count, 2,
                         "唯一那页也调了结论重编，且失败后带反馈又编一次（桩真被用上）")
        # 形态 A：草稿仍在（res.ok=True），但结论一页没写成 —— 用这两个新字段证明
        # 新数字守卫真的响了，而不是靠「一页没出」的旧判据（旧判据已不成立）。
        self.assertEqual(res["polished"], [], "没有一页结论写成")
        self.assertEqual(len(res["polish_failed"]), 1, "新数字守卫要真的响")
        self.assertIn("805", res["polish_failed"][0]["why"])

        got = ingest.read_compile_result()
        self.assertTrue(got, "全部失败也必须落盘，否则刷新后彻底没有线索")
        notes = "；".join(got["notes"])
        self.assertIn("结论重编失败", notes, "失败理由要落进 compile.json 的 notes")
        self.assertIn("805", notes, "守卫给的原始理由要留痕")
        self.assertIn("group-x", notes, "要点名哪一页结论没写成")
        self.assertEqual(got["cand_ids"], [a.id], "这批候选要记进结果")
        assert_polish_failed(self, "group-x")


class RetryHintTest(CompileReportBase):
    """待编译的那批如果上次全被守卫拒了，页面要说出来 —— 否则人只会再点一次，
    同样被拒、同样花钱，而且账面上永远是「还有 N 条待编译」。

    ★ `retry_will_fail` 由 `compile.json` 的 `failed_groups` 驱动；形态 A 的 polish
      失败只进 notes、不进 failed_groups。这里用 `reject_all_groups` 受控注入
      「一组被守卫拒、候选仍留在待编译」的落盘结果，保证这对反证真的能变红。
    """
    REASON = "结论里出现素材里没有的数字：805（已拒写，先核候选）"

    def test_warns_when_pending_batch_is_exactly_last_rejected(self):
        a = mk_cand("十年后只能毛估估", "十年后只能毛估估")
        with replies(one_group(a.id)), reject_all_groups(self.REASON):
            res = ingest.compile_candidates()
        self.assertFalse(res["ok"], "整组被拒、一页未出，不能报成功")
        got = ingest.read_compile_result()
        self.assertEqual(got["failed_groups"][0]["cand_ids"], [a.id])
        self.assertIn("805", got["failed_groups"][0]["why"])

        html = self.client.get("/wiki").content.decode()
        self.assertIn("上次全被拒", html,
                      "按钮亮着但重试必然再被拒 —— 必须当场说，别让人反复花钱")

    def test_no_warning_when_new_candidates_arrived(self):
        """来了一条新的已采纳候选 → 不能再喊「上次全被拒」。"""
        a = mk_cand("十年后只能毛估估", "十年后只能毛估估")
        with replies(one_group(a.id)), reject_all_groups(self.REASON):
            ingest.compile_candidates()
        mk_cand("新的一条判断", "新的一条判断", sid="daily-2026-11-01")

        html = self.client.get("/wiki").content.decode()
        self.assertNotIn("上次全被拒", html)
