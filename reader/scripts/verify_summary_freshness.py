#!/usr/bin/env python3
"""零成本验证 P0 日报新鲜度改造（不调 LLM、不碰真实 Miniflux/DB）。

覆盖：
  1. `_allow_refresh()`：今天/历史日期的 catch_up 语义；
  2. `refresh_stale()`：默认刷全部、有界并发、失败隔离、skipped 统计；
  3. `wait_until_fresh()`：轮询到新鲜、deadline、无 stale 立即返回；
  4. `collect_anomalies()`：宿主停摆归因、skipped/refreshed 可见、不再逐源刷屏；
  5. 窗口后瞬时错误：不进日报、只重试可恢复的错误；
  6. `_header()`：只提示未解决的数据缺口（已解决不写）。

用法：python scripts/verify_summary_freshness.py
"""
import os
import sys
from datetime import timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402
django.setup()

from django.utils import timezone  # noqa: E402

from core import summary  # noqa: E402
from core.datasource.base import CategoryNode, FeedNode  # noqa: E402

ok = bad = 0
fails = []


def check(name, cond, detail=""):
    global ok, bad
    if cond:
        ok += 1
        print(f"  PASS  {name}")
    else:
        bad += 1
        fails.append(name)
        print(f"  FAIL  {name}   {detail}")


def node(fid, last_success, title=None, ok=True, error_msg=""):
    return FeedNode(id=fid, title=title or f"feed-{fid}", site_url="",
                    category_id=1, category_title="测试", last_success=last_success,
                    ok=ok, error_msg=error_msg)


def tree(feeds):
    return [CategoryNode(id=1, title="测试", feeds=feeds)]


class RecordingDS:
    """假数据源：记录被刷新的 feed id；可指定某个 id 抛错。"""

    def __init__(self, fail_ids=()):
        self.called = []
        self.fail_ids = set(fail_ids)

    def refresh_feed(self, fid):
        self.called.append(fid)
        if fid in self.fail_ids:
            raise RuntimeError(f"boom-{fid}")
        return True


class TreeDS:
    """假数据源：`list_tree()` 前 N 次返回 stale，之后返回 fresh。"""

    def __init__(self, feeds, fresh_after=2, end=None):
        self.feeds = feeds
        self.fresh_after = fresh_after
        self.end = end
        self.calls = 0

    def list_tree(self):
        self.calls += 1
        if self.end is not None and self.calls >= self.fresh_after:
            self.feeds = [node(f.id, self.end + timedelta(minutes=1)) for f in self.feeds]
        return tree(self.feeds)

    def _invalidate(self):
        pass


print("=" * 68)
print("1. catch_up 语义：今天必须允许刷新，历史才跳过")
print("=" * 68)
today = summary.due_window_date()
yesterday = today - timedelta(days=1)
check("今天 + catch_up=True → 仍允许刷新",
      summary._allow_refresh(today, True) is True)
check("历史日期 + catch_up=True → 跳过刷新",
      summary._allow_refresh(yesterday, True) is False)
check("任意日期 + catch_up=False → 允许刷新",
      summary._allow_refresh(yesterday, False) is True)

print()
print("=" * 68)
print("2. refresh_stale：默认全部 stale、有界并发、失败隔离")
print("=" * 68)
end = timezone.now().replace(minute=0, second=0, microsecond=0)
old = end - timedelta(hours=9)
feeds = {i: node(i, old) for i in range(1, 13)}

rec = RecordingDS()
summary.new_datasource = lambda: rec
out = summary.refresh_stale(feeds, end, workers=4, max_feeds=0)
check("候选 12 → 尝试 12（不再只刷 10）",
      out["candidates"] == 12 and out["tried"] == 12, out)
check("成功 12、跳过 0", out["ok"] == 12 and out["skipped"] == 0, out)
check("refreshed 记录全部 12 个 id",
      [x["feed_id"] for x in out["refreshed"]] == list(range(1, 13)),
      out["refreshed"])
check("并发下每个 id 恰好刷一次",
      sorted(rec.called) == list(range(1, 13)), rec.called)

rec2 = RecordingDS()
summary.new_datasource = lambda: rec2
out2 = summary.refresh_stale(feeds, end, workers=4, max_feeds=3)
check("max_feeds=3 时尝试 3、跳过 9",
      out2["tried"] == 3 and out2["skipped"] == 9, out2)

rec3 = RecordingDS(fail_ids={3})
summary.new_datasource = lambda: rec3
out3 = summary.refresh_stale(feeds, end, workers=2)
check("单个源失败不毁掉整轮（11 成功 / 1 失败）",
      out3["ok"] == 11 and out3["fail"] == 1, out3)
check("failed 里带 feed_id 与异常",
      out3["failed"] and out3["failed"][0]["feed_id"] == 3
      and "boom-3" in out3["failed"][0]["error"], out3["failed"])

print()
print("=" * 68)
print("3. wait_until_fresh：轮询到新鲜，不再固定 sleep")
print("=" * 68)
base_feeds = [node(i, old) for i in range(1, 4)]
ds = TreeDS(base_feeds, fresh_after=2, end=end)
summary.get_datasource = lambda: ds
real_sleep = summary.time.sleep
summary.time.sleep = lambda _sec: None          # 不真等，测轮询逻辑
try:
    res = summary.wait_until_fresh(
        end, deadline_min=1, poll_sec=1,
        fallback_nodes={f.id: f for f in base_feeds})
finally:
    summary.time.sleep = real_sleep
check("第 2 次检查拿到 fresh 后立即返回",
      res["stale"] == [] and res["checks"] >= 2, res)
check("返回 waited_sec/deadline_min 供报告记录",
      res["waited_sec"] >= 0 and res["deadline_min"] == 1, res)

class AlwaysStaleDS:
    def list_tree(self):
        return tree([node(i, old) for i in range(1, 4)])

    def _invalidate(self):
        pass

summary.get_datasource = lambda: AlwaysStaleDS()
res2 = summary.wait_until_fresh(end, deadline_min=0, poll_sec=1)
check("deadline=0 仍立即复检一次，并如实返回 still_stale",
      len(res2["stale"]) == 3 and res2["checks"] == 1, res2)

class AllFreshDS:
    def list_tree(self):
        return tree([node(i, end + timedelta(minutes=1)) for i in range(1, 4)])

    def _invalidate(self):
        pass

summary.get_datasource = lambda: AllFreshDS()
res3 = summary.wait_until_fresh(end, deadline_min=1, poll_sec=1)
check("一开始就新鲜 → 只检查 1 次就返回",
      res3["stale"] == [] and res3["checks"] == 1, res3)

print()
print("=" * 68)
print("4. collect_anomalies：宿主停摆归因 + 只报未解决")
print("=" * 68)
stale_nodes = {i: node(i, old) for i in range(1, 37)}
stats = {
    "refresh": {
        "candidates": 46, "tried": 10, "ok": 10, "fail": 0, "skipped": 36,
        "refreshed": [{"feed_id": i, "title": f"feed-{i}"} for i in range(1, 11)],
        "failed": [], "elapsed_sec": 180.0,
    },
    "freshness": {
        "attempted": True, "stale_before": 46, "stale_after": 36,
        "waited_sec": 900.0, "deadline_min": 30,
    },
}
lines = summary.collect_anomalies(stale_nodes, end, stats)
text = "\n".join(lines)
check("识别为宿主停摆（一条，而不是 36 行）",
      "宿主机/采集疑似停摆" in text, text)
check("明确写出「未尝试 36 个」",
      "未尝试 36 个" in text, text)
check("不再展示成功刷新清单（只报未解决）",
      "本次实际刷新" not in text, text)
check("不再写「等待 900s 后仍有…」这类过程信息",
      "等待 900s" not in text and "仍有 36 个源未更新" not in text, text)
check("按缺口分桶（≥6h）而不是逐源 36 行",
      "≥6h（36）" in text, text)
check("不再出现 36 条 `- ⚠️ #id` 式告警",
      sum(1 for x in lines if x.startswith("- ⚠️ #")) == 0, lines)
check("异常段总行数 < 15", len(lines) < 15, len(lines))
check("host_sleep 写回 stats（供报告头使用）",
      bool(stats["freshness"].get("host_sleep")), stats["freshness"])

print()
print("=" * 68)
print("5. 窗口后瞬时错误：不进日报、只重试可恢复的错误")
print("=" * 68)
cases = [
    ("dial tcp: lookup feed.appinn.com on 127.0.0.11:53: no such host", True),
    ("fetcher: Get \"https://x/feed\": EOF", True),
    ("context deadline exceeded (Client.Timeout exceeded while awaiting headers)", True),
    ("fetcher: service unavailable (503 status code)", True),
    ("404 Not Found", False),
    ("403 Forbidden", False),
    ("not a valid RSS feed", False),
]
for msg, want in cases:
    check(f"瞬时判定：{msg[:34]} → {want}",
          summary.is_transient_error(msg) is want, msg)

retry_nodes = {
    1: node(1, end + timedelta(hours=1), ok=False,
            error_msg="dial tcp: lookup x on 127.0.0.11:53: no such host"),
    2: node(2, end + timedelta(hours=1), ok=False, error_msg="404 Not Found"),
    3: node(3, old, ok=False, error_msg="context deadline exceeded"),
}
rec4 = RecordingDS()
summary.new_datasource = lambda: rec4
out4 = summary.refresh_stale(retry_nodes, end, workers=2)
check("窗口后 DNS 错误会被重试；404 不会",
      sorted(rec4.called) == [1, 3], rec4.called)
check("stale/retry 分开计数（1 个窗口缺口 + 1 个窗口后错误）",
      out4["stale_candidates"] == 1 and out4["retry_candidates"] == 1, out4)
check("窗口缺口与重试分别有 tried/ok 计数",
      out4["stale_tried"] == 1 and out4["retry_tried"] == 1
      and out4["retry_ok"] == 1, out4)

post_stats = {
    "refresh": {
        "candidates": 1, "stale_candidates": 0, "retry_candidates": 1,
        "tried": 1, "ok": 1, "fail": 0,
        "stale_tried": 0, "stale_ok": 0, "stale_fail": 0,
        "retry_tried": 1, "retry_ok": 1, "retry_fail": 0,
        "skipped": 0, "stale_skipped": 0, "retry_skipped": 0,
        "refreshed": [{"feed_id": 1, "title": "feed-1", "retry": True}],
        "failed": [], "elapsed_sec": 5.0,
    },
    "freshness": {"attempted": True, "stale_before": 0, "stale_after": 0,
                  "retry_before": 1, "waited_sec": 0.0, "deadline_min": 30},
}
post_nodes = {
    1: node(1, end + timedelta(hours=1), ok=False,
            error_msg="no such host"),
    2: node(2, end + timedelta(hours=1), ok=False, error_msg="404 Not Found"),
}
lines_post = summary.collect_anomalies(post_nodes, end, post_stats)
text_post = "\n".join(lines_post)
check("窗口后错误不出现「解析错误」",
      "解析错误" not in text_post, text_post)
check("窗口后错误不出现「补救刷新」噪音",
      "补救刷新" not in text_post, text_post)
check("窗口后错误不影响异常段（可以为空）", lines_post == [], lines_post)

gap_nodes = {1: node(1, old, ok=False, error_msg="no such host")}
lines_gap = summary.collect_anomalies(
    gap_nodes, end, {"refresh": {}, "freshness": {}})
check("窗口内错误仍然要报（不是把所有 error 都静音）",
      any("解析错误" in x for x in lines_gap), lines_gap)

print()
print("=" * 68)
print("6. 全部补齐 / 仍未补齐：报告头与异常段说清楚")
print("=" * 68)
fixed_stats = {
    "refresh": {"candidates": 46, "tried": 46, "ok": 46, "fail": 0,
                "skipped": 0, "refreshed": [], "failed": [], "elapsed_sec": 120.0},
    "freshness": {
        "attempted": True, "stale_before": 46, "stale_after": 0,
        "waited_sec": 120.0, "deadline_min": 30,
        "before_stale": [{"feed_id": i, "title": f"feed-{i}",
                          "last_success": old, "gap_h": 9.0, "never": False}
                         for i in range(1, 47)],
    },
}
fresh_nodes = {i: node(i, end + timedelta(minutes=1)) for i in range(1, 47)}
lines2 = summary.collect_anomalies(fresh_nodes, end, fixed_stats)
text2 = "\n".join(lines2)
check("全部补齐 → 异常段为空（不再报已解决的事）",
      lines2 == [], lines2)

# ★ 关键回归：refresh 失败后 Miniflux 的 last_success 会“跳到 end 之后”，
#   不能让“✅ 全部补齐”覆盖失败事实。
failed_stats = {
    "refresh": {
        "stale_candidates": 2, "stale_tried": 2, "stale_ok": 0,
        "stale_fail": 2, "stale_skipped": 0, "retry_candidates": 0,
        "failed": [
            {"feed_id": 1, "title": "feed-1", "error": "500 Server Error",
             "retry": False, "last_success": old},
            {"feed_id": 2, "title": "feed-2", "error": "500 Server Error",
             "retry": False, "last_success": old},
        ],
        "refreshed": [], "elapsed_sec": 5.0,
    },
    "freshness": {
        "attempted": True, "stale_before": 2, "stale_after": 2,
        "waited_sec": 0.0, "deadline_min": 30,
        "after_stale": [
            {"feed_id": 1, "title": "feed-1", "last_success": old,
             "gap_h": 9.0, "never": False},
            {"feed_id": 2, "title": "feed-2", "last_success": old,
             "gap_h": 9.0, "never": False},
        ],
    },
}
heuristic_fresh = {
    1: node(1, end + timedelta(minutes=1), ok=False, error_msg="500 Server Error"),
    2: node(2, end + timedelta(minutes=1), ok=False, error_msg="500 Server Error"),
}
lines_failed = summary.collect_anomalies(heuristic_fresh, end, failed_stats)
text_failed = "\n".join(lines_failed)
check("refresh 失败不被“last_success 跳到 end 后”掩盖",
      "补救刷新未解决：失败 2 个" in text_failed, text_failed)
check("失败源进入未解决分桶（不是 ✅ 补齐）",
      "窗口结束前仍未成功拉取：2 个源" in text_failed, text_failed)
check("失败时不再输出成功刷新清单",
      "本次实际刷新" not in text_failed, text_failed)

merged = summary._merge_stale_details(
    [], failed_stats["refresh"]["failed"], end, heuristic_fresh)
check("_merge_stale_details 把失败项并回 stale",
      len(merged) == 2 and all(x["gap_h"] == 9.0 for x in merged), merged)
recovered = {1: node(1, end + timedelta(minutes=1))}
merged2 = summary._merge_stale_details(
    [], [failed_stats["refresh"]["failed"][0]], end, recovered)
check("后来已恢复的源不并回 stale", merged2 == [], merged2)


class _Row:
    catch_up = False
    finished_at = None
    attempt = 1


def header(freshness):
    st = {"s3": 0, "s2": 0, "s1": 0, "s0": 0, "calls": 0, "tokens": 0,
          "reasoning_tokens": 0, "truncated": [], "freshness": freshness}
    return "\n".join(summary._header(today, *summary.window_for(today), _Row(), st))


h_bad = header(stats["freshness"])
h_ok = header(fixed_stats["freshness"])
check("头部：仍未补齐 → 写「数据不完整」",
      "数据不完整" in h_bad and "仍有 36 个源" in h_bad, h_bad)
check("头部：全部补齐 → 不再写「数据补齐」",
      "数据补齐" not in h_ok and "数据不完整" not in h_ok, h_ok)
check("头部：宿主停摆提示只在有未解决缺口时出现",
      "数据缺口" in h_bad and "疑似宿主机休眠" in h_bad, h_bad)

print()
print("=" * 68)
if bad:
    print(f"FAILED: {bad} 项失败 / {ok + bad} 项")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print(f"ALL PASS: {ok} 项")
