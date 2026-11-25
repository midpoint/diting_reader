"""预算闸与统一调用出口：pool_gate / chat_recorded / 花费口径。

「每次调用必记账 + 预算双闸」是核心原则，这里钉住它的机制面：
  · 恰好到上限即停（>=，宁可早停不可超支）；
  · 三池独立（拆池的教训：补跑报告曾把分级饿死）；
  · prompt_build 的钱算分级池；
  · 闸拦下时不调模型、不落账；
  · 记账带真实传输重试次数与延迟（曾经手抄漏记/错记）；
  · 「今日」按本地零点切，不是 UTC 零点。
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from core import costreport as cr
from core import llm
from core.models import AppSetting, LlmUsage


def spend(kind="triage", cny="0.3", ts=None, feed_id=None):
    """写一行账。ts 需用 update 回填（LlmUsage.ts 是 auto_now_add）。"""
    row = LlmUsage.objects.create(
        kind=kind, provider="deepseek", model="m", feed_id=feed_id,
        prompt_tokens=10, completion_tokens=5,
        cost_cny=Decimal(cny), cost_usd=Decimal("0.04"))
    if ts is not None:
        LlmUsage.objects.filter(pk=row.pk).update(ts=ts)
    return row


class PoolGateTest(TestCase):
    def test_under_limit_passes(self):
        AppSetting.objects.create(key="triage_budget_cny", value="0.5")
        spend("triage", "0.3")
        over, why = cr.pool_gate(cr.POOL_TRIAGE)
        self.assertFalse(over)
        self.assertEqual(why, "")

    def test_at_exact_limit_blocks(self):
        # 恰好花到上限也算超（>=）：预算是硬闸不是软目标
        AppSetting.objects.create(key="triage_budget_cny", value="0.5")
        spend("triage", "0.5")
        over, why = cr.pool_gate(cr.POOL_TRIAGE)
        self.assertTrue(over)
        self.assertIn("0.5", why)

    def test_pools_are_independent(self):
        AppSetting.objects.create(key="triage_budget_cny", value="0.1")
        spend("summary_global", "5")      # 总结池花爆
        over, _ = cr.pool_gate(cr.POOL_TRIAGE)
        self.assertFalse(over)            # 不影响分级池

    def test_prompt_build_counts_in_triage_pool(self):
        AppSetting.objects.create(key="triage_budget_cny", value="0.1")
        spend("prompt_build", "0.2")
        over, _ = cr.pool_gate(cr.POOL_TRIAGE)
        self.assertTrue(over)

    def test_pool_spent_matches_gate_sql(self):
        # 开销页展示与闸门判定必须是同一条 SQL（曾经一个 Python 聚合一个 SQL）
        spend("triage", "0.2")
        spend("prompt_build", "0.1")
        spend("summary_global", "9")
        self.assertAlmostEqual(cr.pool_spent_cny(cr.POOL_TRIAGE), 0.3)
        self.assertAlmostEqual(cr.pool_spent_cny(cr.POOL_SUMMARY), 9.0)


class SpentTodayTest(TestCase):
    def test_local_midnight_boundary(self):
        # 「今日」按本地零点切：UTC 零点差 8 小时，预算会晚到早上八点才翻篇
        midnight = timezone.localtime().replace(hour=0, minute=0, second=0,
                                                microsecond=0)
        spend("triage", "0.2", ts=midnight - timedelta(minutes=1))
        spend("triage", "0.1", ts=midnight + timedelta(minutes=1))
        self.assertAlmostEqual(llm.spent_today_cny(["triage"]), 0.1)

    def test_kind_filter(self):
        spend("translate_title", "0.9")
        self.assertAlmostEqual(llm.spent_today_cny(["triage", "prompt_build"]), 0.0)

    def test_days_window(self):
        midnight = timezone.localtime().replace(hour=0, minute=0, second=0,
                                                microsecond=0)
        spend("triage", "0.2", ts=midnight - timedelta(hours=1))   # 昨天 23:00
        self.assertAlmostEqual(llm.spent_today_cny(["triage"]), 0.0)
        self.assertAlmostEqual(llm.spent_today_cny(["triage"], days=2), 0.2)


class ChatRecordedTest(TestCase):
    def test_gate_blocks_without_calling_or_recording(self):
        AppSetting.objects.create(key="triage_budget_cny", value="0.1")
        spend("triage", "0.2")
        with mock.patch.object(llm, "chat") as chat, \
                mock.patch.object(llm, "record") as record:
            res = cr.chat_recorded("triage", [{"role": "user", "content": "x"}],
                                   pool=cr.POOL_TRIAGE, feed_id=7)
        chat.assert_not_called()
        record.assert_not_called()
        self.assertTrue(res.budget_stop)
        self.assertIsNone(res.usage_row)
        self.assertEqual(res.text, "")
        self.assertIn("已用尽", res.err)

    def test_records_real_transport_retries_and_latency(self):
        # tries=3（两次失败一次成功）⇒ 账本 retries=2；latency 由出口统一测量
        with mock.patch.object(llm, "chat",
                               return_value=("t", {"prompt_tokens": 5}, "", 3)), \
                mock.patch.object(llm, "record") as record:
            res = cr.chat_recorded("triage", [{"role": "user", "content": "x"}],
                                   feed_id=7, category_id=3)
        record.assert_called_once()
        kw = record.call_args.kwargs
        self.assertEqual(kw["retries"], 2)
        self.assertGreaterEqual(kw["latency_ms"], 0)
        self.assertEqual(kw["feed_id"], 7)
        self.assertEqual(kw["category_id"], 3)
        self.assertEqual((res.text, res.err, res.budget_stop), ("t", "", False))

    def test_pool_none_skips_gate(self):
        # pool=None 的调用不判闸（调用方已在更粗粒度判过，如 translate 逐批判）
        AppSetting.objects.create(key="triage_budget_cny", value="0.01")
        spend("triage", "5")
        with mock.patch.object(llm, "chat", return_value=("t", {}, "", 1)), \
                mock.patch.object(llm, "record"):
            res = cr.chat_recorded("translate_title", [], pool=None)
        self.assertFalse(res.budget_stop)


class BudgetDefaultsTest(SimpleTestCase):
    """开源部署没有 AppSetting 行 ⇒ 代码默认就是真实上限，钉住防漂移。

    2026-11-17 按真实库 AppSetting 校准。假设没有 .env 覆盖预算键
    （.env.example 里它们全是注释）——有人调高会在这里当场响，而不是等账单。
    """

    CALIBRATED = {
        cr.POOL_TRIAGE: 0.6,
        cr.POOL_SUMMARY: 1.5,
        cr.POOL_TRANSLATE: 0.3,
        cr.POOL_WEEKLY: 0.5,
        cr.POOL_WIKI: 0.5,
    }

    def test_pool_defaults_match_calibrated_values(self):
        for pool, expected in self.CALIBRATED.items():
            with self.subTest(pool=pool):
                self.assertAlmostEqual(cr.POOL_DEFAULTS[pool], expected)

    def test_default_total_matches_readme(self):
        # README「每天大概花多少钱」写的是五池之和，改默认值要同步改那里
        self.assertAlmostEqual(sum(self.CALIBRATED.values()), 3.4)


class EstimateCostTest(SimpleTestCase):
    """价目按 settings 算（.env 可覆盖单价，断言不能写死 7.1 之类）。"""

    USAGE = {"prompt_tokens": 1000, "cached_tokens": 400, "completion_tokens": 200}

    def test_deepseek_offpeak(self):
        pr = settings.DEEPSEEK_PRICE
        expect = (600 * pr["cache_miss"] + 400 * pr["cache_hit"]
                  + 200 * pr["output"]) / 1e6
        with mock.patch.object(llm, "is_peak_now", return_value=False):
            usd, cny = llm.estimate_cost("deepseek", self.USAGE)
        self.assertAlmostEqual(usd, expect)
        self.assertAlmostEqual(cny, expect * settings.USD_CNY)

    def test_peak_doubles_price(self):
        pr = settings.DEEPSEEK_PRICE
        with mock.patch.object(llm, "is_peak_now", return_value=True):
            usd, _ = llm.estimate_cost(
                "deepseek", {"prompt_tokens": 0, "cached_tokens": 0,
                             "completion_tokens": 100})
        self.assertAlmostEqual(usd, 100 * pr["output"] * 2 / 1e6)

    def test_other_provider_unpriced(self):
        # 非 DeepSeek 没有 cost_cny（预算靠调用次数兜底），别算出假数
        self.assertEqual(llm.estimate_cost("agnes", self.USAGE), (None, None))
