"""分级投票与聚合的硬规则（anti-FOMO 不变量）。

这组测试钉的是 README 承诺过的四条硬约束，任何一条红了都是抗 FOMO 底座被破坏：
  1. 有效票 < 2 张时唯一 L0 票落 default_level（证据不足，不直接判 L0）；
  2. 全票 L0 才判 L0；
  3. 多数说 L0 但有分歧 → 保留（至少 L1 / default_level 取高）；
  4. 平票取更高级别；无有效票 → fail-open 掉到 default_level。

全部纯函数，不碰库、不调模型。
"""
from datetime import timedelta

from django.conf import settings
from django.test import SimpleTestCase
from django.utils import timezone

from core import triage
from core.models import EntryRating


class NormalizeLevelTest(SimpleTestCase):
    def test_formats(self):
        self.assertEqual(triage.normalize_level("L2"), 2)
        self.assertEqual(triage.normalize_level("2"), 2)
        self.assertEqual(triage.normalize_level(" l3 "), 3)
        self.assertEqual(triage.normalize_level(0), 0)

    def test_invalid(self):
        self.assertIsNone(triage.normalize_level(None))
        self.assertIsNone(triage.normalize_level("L9"))    # 4-9 不在四级内
        self.assertIsNone(triage.normalize_level("垃圾"))


class AggregateLevelTest(SimpleTestCase):
    def v(self, *levels):
        return [{"level": lv} for lv in levels]

    def test_no_votes_fail_open_to_default(self):
        level, info = triage.aggregate_level([], 2)
        self.assertEqual(level, 2)
        self.assertIn("fail-open", info["reason"])

    def test_single_vote_never_l0(self):
        # 单票非 L0 直接采用；单票 L0 掉回 default_level（证据不足不可丢）
        self.assertEqual(triage.aggregate_level(self.v(2), 2)[0], 2)
        self.assertEqual(triage.aggregate_level(self.v(0), 2)[0], 2)

    def test_default_zero_is_explicit_discard(self):
        # 2026-10-29 定稿：default_level=0 是「可大量丢」的显式配置；
        # 无票 / 单票 L0 都落 default_level（0），不做 L1 下限。
        self.assertEqual(triage.aggregate_level([], 0)[0], 0)
        self.assertEqual(triage.aggregate_level(self.v(0), 0)[0], 0)

    def test_all_l0_required_for_l0(self):
        self.assertEqual(triage.aggregate_level(self.v(0, 0), 2)[0], 0)
        self.assertEqual(triage.aggregate_level(self.v(0, 0, 0), 2)[0], 0)

    def test_majority_l0_with_dissent_kept(self):
        level, info = triage.aggregate_level(self.v(0, 0, 1), 2)
        self.assertEqual(level, 2)                      # max(default=2, 1)
        self.assertIn("保留", info["reason"])
        # default=0 的源也至少保 L1，不许因为默认值低就丢
        self.assertEqual(triage.aggregate_level(self.v(0, 0, 1), 0)[0], 1)

    def test_majority_nonzero_wins(self):
        self.assertEqual(triage.aggregate_level(self.v(1, 1, 2), 2)[0], 1)
        self.assertEqual(triage.aggregate_level(self.v(2, 2, 1), 2)[0], 2)

    def test_tie_takes_higher(self):
        self.assertEqual(triage.aggregate_level(self.v(1, 2), 2)[0], 2)
        self.assertEqual(triage.aggregate_level(self.v(1, 1, 2, 2), 2)[0], 2)
        # 平票里有 L0：取高 ⇒ 保留为 L1，不丢
        self.assertEqual(triage.aggregate_level(self.v(0, 1), 2)[0], 1)

    def test_invalid_votes_filtered_before_counting(self):
        votes = [{"level": None}, {"level": "L2"}, {"why": "没级别"}]
        level, info = triage.aggregate_level(votes, 3)
        self.assertEqual((level, info["n"]), (2, 1))


class VotesDoneTest(SimpleTestCase):
    """逐票评估的续票停止条件：L2/L3 即停、L1 满 2 票、L0 满 repeat 票。"""

    def test_high_vote_rescues_immediately(self):
        d = triage.votes_done
        self.assertTrue(d([2], 3, True))
        self.assertTrue(d([3], 3, True))
        self.assertTrue(d([0, 2], 3, True))   # 首票 L0，第二票救回 → 不必投第三票
        self.assertTrue(d([1, 3], 3, True))

    def test_l1_needs_second_vote(self):
        d = triage.votes_done
        self.assertFalse(d([1], 3, True))
        self.assertTrue(d([1, 1], 3, True))
        self.assertTrue(d([1, 0], 3, True))   # 复核票杀不掉：[L1,L0] 平票取高仍是 L1

    def test_l0_needs_full_votes(self):
        d = triage.votes_done
        self.assertFalse(d([0], 3, True))
        self.assertFalse(d([0, 0], 3, True))  # 两票全 L0 也继续——第三票可能是救回票
        self.assertTrue(d([0, 0, 0], 3, True))
        self.assertTrue(d([0], 1, True))      # repeat=1 时满票就是一票
        self.assertTrue(d([0, 0], 2, True))   # L0 满票线跟着 repeat 走

    def test_adaptive_off_votes_full(self):
        d = triage.votes_done
        self.assertFalse(d([3], 3, False))
        self.assertTrue(d([3, 2, 1], 3, False))

    def test_stop_then_aggregate_keeps_anti_fomo(self):
        # 停止条件只管省钱，判决仍由聚合器兜底：[L0,L1] 停 → 平票取高 = L1，不丢
        self.assertEqual(triage.aggregate_level([{"level": 0}, {"level": 1}], 2)[0], 1)
        # [L0,L2] 停 → L2 救回
        self.assertEqual(triage.aggregate_level([{"level": 0}, {"level": 2}], 2)[0], 2)


class ParseRowsTest(SimpleTestCase):
    """「模型输出不可信」的第一道防线：围栏 / 夹杂说明 / 坏行都要扛住。"""

    def test_plain_array(self):
        rows = triage.parse_rows(
            '[{"id": 1, "level": "L2", "one_line": "x", "why": "y"}]')
        self.assertEqual(rows, [{"id": 1, "level": 2, "one_line": "x", "why": "y"}])

    def test_markdown_fence_stripped(self):
        rows = triage.parse_rows('```json\n[{"id": 2, "level": 0}]\n```')
        self.assertEqual(rows[0]["id"], 2)

    def test_prose_wrapped_array_found(self):
        rows = triage.parse_rows('好的，结果如下：\n[{"id": 3, "level": "L1"}]\n以上。')
        self.assertEqual(rows[0]["level"], 1)

    def test_single_dict_wrapped(self):
        rows = triage.parse_rows('{"id": 4, "level": 3}')
        self.assertEqual(rows[0]["id"], 4)

    def test_bad_rows_dropped(self):
        # 没级别 / 没 id / 非法 level 的行被丢掉，不能让坏行污染票箱
        rows = triage.parse_rows('[{"id": 5}, {"level": 2}, {"id": 6, "level": 9}, '
                                 '{"id": "7", "level": 1}]')
        self.assertEqual([r["id"] for r in rows], [7])

    def test_garbage_returns_empty(self):
        self.assertEqual(triage.parse_rows("not json at all"), [])
        self.assertEqual(triage.parse_rows(""), [])


class RateStateTest(SimpleTestCase):
    """状态判据是 reason 而不是 level：光看级别分不出判的/默认的/兜底的。"""

    def test_reason_mapping(self):
        cases = {"llm": "llm", "manual": "manual", "fail_open": "fail",
                 "default": "default"}
        for reason, state in cases.items():
            rating = EntryRating(entry_id=1, level=2, reason=reason)
            self.assertEqual(triage.entry_rate_state(rating), state)

    def test_unrated_pending_or_expired(self):
        now = timezone.now()
        self.assertEqual(triage.entry_rate_state(None, now - timedelta(days=1)),
                         "pending")
        old = now - timedelta(days=settings.TRIAGE_LOOKBACK_DAYS + 1)
        self.assertEqual(triage.entry_rate_state(None, old, now=now), "expired")
        self.assertEqual(triage.entry_rate_state(None), "pending")

    def test_count_states_has_all_six_keys(self):
        out = triage.count_states(["llm", "llm", "fail"])
        self.assertEqual(out["llm"], 2)
        self.assertEqual(out["fail"], 1)
        for state in triage.RATE_STATES:
            self.assertIn(state, out)
