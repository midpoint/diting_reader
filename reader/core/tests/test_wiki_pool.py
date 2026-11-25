"""知识库预算池（第 5 池）：结构一致性 + 闸行为。

为什么单独一个池（2026-10-31 搭骨架时的判据）：
  wiki 的开销是**事件驱动**的 —— 人聊一句、批一次编译才花钱，和日报/周报的
  定时批处理节奏完全不同。共用池子两个方向都会错：聊两句可能吃掉当天日报的钱，
  补跑日报也可能把人正在做的编译拦住（与「补跑报告饿死分级」同一条教训）。

这个文件钉两类东西：
  A. **单一来源**：池 → kind / 键 / 默认值的五张表必须互相覆盖、不重不漏。
     新增一个 kind 忘了挂池，钱会记在账上却不在任何预算里（等于不设限）。
  B. **闸的行为**：未知池必须抛错而不是「恒不超」；超限的 why 必须是人话，
     因为 wiki 是按钮驱动的 —— 「点了没反应」比「今天用尽了」糟得多。
"""
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, TestCase

from core import costreport as cr
from core import llm
from core.models import AppSetting, LlmUsage

KIND_CHOICES = dict(LlmUsage.KINDS)


def spend(kind="wiki_chat", cny="0.3"):
    """写一行账（LlmUsage.ts 由 auto_now_add 落，闸只看今天，够用）。"""
    return LlmUsage.objects.create(
        kind=kind, provider="deepseek", model="m",
        prompt_tokens=10, completion_tokens=5,
        cost_cny=Decimal(cny), cost_usd=Decimal("0.04"))


class WikiPoolShapeTest(SimpleTestCase):
    """池的五张表必须自洽 —— 这部分不需要数据库。"""

    def test_pool_is_registered_last(self):
        # 预算卡按 ALL_POOLS 顺序渲染；知识库是主内容池，追加在末尾（不插日报周报之间）
        self.assertIn(cr.POOL_WIKI, cr.ALL_POOLS)
        self.assertEqual(cr.ALL_POOLS[-1], cr.POOL_WIKI)

    def test_kinds_are_the_four_loops_plus_summary(self):
        """wiki 池的构成：四个「环节」+ 一句话总结。

        ★ 2026-11-02 改：原来是 `assertEqual(池, (四个,))` —— 把「恰好四类」
          写死成了契约，加第 5 类（2.4 的 wiki_summary）时它红，但**红得没有
          信息量**：它只说明数字变了，不说明新 kind 该不该在池里。
          现在的写法守住真正的不变量：这五个必须都在，且顺序稳定
          （报表图例按这个顺序出）。覆盖完全性由
          `test_every_kind_has_exactly_one_pool` 双向兜底。
        """
        self.assertEqual(
            cr.POOL_KINDS[cr.POOL_WIKI],
            ("wiki_chat", "wiki_extract", "wiki_compile", "wiki_lint",
             "wiki_summary"))

    def test_kinds_exist_in_model_choices(self):
        # CharField 的 choices **不拦写入**（DB 层没有约束）：kind 不在表里也能落库，
        # 只是报表里没有 label、图例里没有颜色 —— 静默的脏数据。
        missing = [k for k in cr.POOL_KINDS[cr.POOL_WIKI] if k not in KIND_CHOICES]
        self.assertEqual(missing, [])

    def test_every_kind_has_exactly_one_pool(self):
        # 双向覆盖：漏一个 ⇒ 钱花了但不在任何池的预算里；多一个 ⇒ 池里有个不存在的 kind
        pooled = [k for kinds in cr.POOL_KINDS.values() for k in kinds]
        self.assertEqual(len(pooled), len(set(pooled)), "池之间重叠")
        self.assertEqual(set(pooled), set(KIND_CHOICES), "POOL_KINDS 与模型 choices 不同源")

    def test_kind_order_covers_all_kinds(self):
        # 少了 ⇒ 图例有名字柱子里没有（周报隐身过）；多了 ⇒ 图里多一列永远为 0
        self.assertEqual(len(cr.KIND_ORDER), len(set(cr.KIND_ORDER)))
        self.assertEqual(set(cr.KIND_ORDER), set(KIND_CHOICES))

    def test_budget_key_and_default(self):
        self.assertEqual(cr.POOL_BUDGET_KEYS[cr.POOL_WIKI], "wiki_budget_cny")
        self.assertEqual(cr.POOL_DEFAULTS[cr.POOL_WIKI], settings.WIKI_BUDGET_CNY)
        # 键不能与别的池撞：撞了就是「改 A 池把 B 池覆盖掉」（周报池踩过）
        self.assertEqual(len(set(cr.POOL_BUDGET_KEYS.values())), len(cr.ALL_POOLS))

    def test_label(self):
        self.assertEqual(cr.POOL_LABELS[cr.POOL_WIKI], "知识库")

    def test_pool_notes_cover_all_pools(self):
        # 超限后果文案必须每池一条：漏了 → pool_status 直接 KeyError（而不是
        # 悄悄套用别的池的说法，把「日报降级」安到知识库头上）
        self.assertEqual(set(cr.POOL_NOTES), set(cr.ALL_POOLS))
        self.assertTrue(all(cr.POOL_NOTES[p].strip() for p in cr.ALL_POOLS))

    def test_template_has_no_per_pool_branches(self):
        # 模板只许渲染 p.note，不许自己按池写 if/elif —— 那是第二份「池 → 文案」表
        from pathlib import Path
        tpl = (Path(__file__).resolve().parents[2]
               / "templates" / "costs" / "index.html").read_text("utf-8")
        self.assertNotIn('p.pool ==', tpl)
        self.assertIn("{{ p.note }}", tpl)


class WikiPoolGateTest(TestCase):
    """闸的行为：独立性、四类 kind 的归属、超限原因、未知池。"""

    def test_independent_from_summary_pool(self):
        # 日报池花爆，不该拦知识库（反之亦然）—— 这是分池的全部理由
        AppSetting.objects.create(key="summary_budget_cny", value="0.1")
        AppSetting.objects.create(key="wiki_budget_cny", value="1.0")
        spend("summary_global", "5")
        self.assertFalse(cr.pool_gate(cr.POOL_WIKI)[0])
        self.assertTrue(cr.pool_gate(cr.POOL_SUMMARY)[0])

    def test_all_wiki_kinds_count(self):
        AppSetting.objects.create(key="wiki_budget_cny", value="0.5")
        kinds = cr.POOL_KINDS[cr.POOL_WIKI]
        for kind in kinds:
            spend(kind, "0.2")
        # ★ 期望值随 kind 数走（不是写死的 0.8）：这条测的是「每个 kind 都算进池」，
        #   写死数字的话，加一类 kind 只会让它红，而不告诉你「新 kind 漏了归属」。
        #   任一 kind 漏了归属，这里只会到 0.2 的整数倍减一档（4/5 类 = 0.8 ≠ 1.0）。
        self.assertAlmostEqual(cr.pool_spent_cny(cr.POOL_WIKI), 0.2 * len(kinds))
        self.assertTrue(cr.pool_gate(cr.POOL_WIKI)[0])

    def test_over_limit_reason_is_readable(self):
        # 事件驱动的池超限必须能回显给人：原因里要有池的中文名和具体额度
        AppSetting.objects.create(key="wiki_budget_cny", value="0.2")
        spend("wiki_chat", "0.2")
        over, why = cr.pool_gate(cr.POOL_WIKI)
        self.assertTrue(over)
        self.assertIn("知识库", why)
        self.assertIn("0.2", why)

    def test_unknown_pool_raises(self):
        # ★ 原实现 `.get(pool, ())` ⇒ 空 kinds ⇒ 花费恒 0 ⇒ 永不超限（静默失效）。
        #   拼错池名是开发期 bug，必须当场炸，而不是月底看账单。
        with self.assertRaises(ValueError):
            cr.pool_gate("wiki_tpyo")
        with self.assertRaises(ValueError):
            cr.pool_spent_cny("wiki_tpyo")

    def test_chat_recorded_blocks_without_spending(self):
        # 会花钱的路径必须打桩：断言「模型没被调、账没落」
        AppSetting.objects.create(key="wiki_budget_cny", value="0.1")
        spend("wiki_compile", "0.3")
        with mock.patch.object(llm, "chat") as chat, \
                mock.patch.object(llm, "record") as record:
            res = cr.chat_recorded("wiki_chat", [{"role": "user", "content": "x"}],
                                   pool=cr.POOL_WIKI)
        chat.assert_not_called()
        record.assert_not_called()
        self.assertTrue(res.budget_stop)
        self.assertIn("知识库", res.err)

    def test_chat_recorded_under_limit_records_kind(self):
        # 真调出口（只打桩模型本身）：kind 要真的落进账本，否则池子永远读不到钱
        AppSetting.objects.create(key="wiki_budget_cny", value="1.0")
        with mock.patch.object(llm, "chat",
                               return_value=("t", {"prompt_tokens": 5}, "", 1)):
            res = cr.chat_recorded("wiki_chat", [{"role": "user", "content": "x"}],
                                   pool=cr.POOL_WIKI)
        self.assertFalse(res.budget_stop)
        self.assertEqual(LlmUsage.objects.filter(kind="wiki_chat").count(), 1)
        self.assertGreaterEqual(cr.pool_spent_cny(cr.POOL_WIKI), 0)

    def test_status_card(self):
        AppSetting.objects.create(key="wiki_budget_cny", value="2")
        spend("wiki_lint", "0.5")
        s = cr.pool_status(cr.POOL_WIKI)
        self.assertEqual((s["label"], s["limit_cny"]), ("知识库", 2.0))
        self.assertAlmostEqual(s["spent_cny"], 0.5)
        self.assertAlmostEqual(s["ratio"], 25.0)
        self.assertFalse(s["over"])
        self.assertIn("对话", s["note"])


class WikiPoolSaveViewTest(TestCase):
    """真调视图：`/costs/budget?pool=wiki` 必须写进 wiki 键。

    ★ 为什么不能只断言源码里"用了 POOL_BUDGET_KEYS"：2026-10-29 的事故正是
      代码看着没问题、但保存后**别的键**被改了 —— 返回 ok=true，池子纹丝不动。
      源码断言抓不住「写到哪儿去了」，只有真发一次 POST 再看库才知道。
    """

    def test_writes_wiki_key_only(self):
        res = self.client.post("/costs/budget",
                               {"pool": "wiki", "budget_cny": "2.5"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["ok"])
        self.assertEqual(AppSetting.objects.get(key="wiki_budget_cny").value, "2.5")
        # 不许顺手把日报池改掉（那正是旧事故的形态）
        self.assertFalse(AppSetting.objects.filter(key="summary_budget_cny").exists())

    def test_unknown_pool_is_400_and_writes_nothing(self):
        res = self.client.post("/costs/budget",
                               {"pool": "wiki_tpyo", "budget_cny": "1"})
        self.assertEqual(res.status_code, 400)
        self.assertFalse(AppSetting.objects.exists())

    def test_negative_rejected(self):
        res = self.client.post("/costs/budget",
                               {"pool": "wiki", "budget_cny": "-1"})
        self.assertEqual(res.status_code, 400)
        self.assertFalse(AppSetting.objects.exists())
