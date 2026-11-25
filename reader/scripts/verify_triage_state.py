"""零成本验证「分级来源可视化」（不调 LLM、不落盘、不改库）。

用法：python scripts/verify_triage_state.py

覆盖：
  1. reason → 六种状态的映射（llm / default / fail / manual / pending / expired）
  2. pending 与 expired 的边界正好落在 TRIAGE_LOOKBACK_DAYS
  3. 徽标模板只分三档渲染（彩色=判过 / 红=失败兜底 / 灰=没判定）
  4. 三档视觉契约 + 右键菜单「判过没有」的判定 + 单条判定的护栏
  5. 元数据完整性（模板取不到 key 会静默渲染空）
  6. 「降级」= 压成一句话（与级别无关）：规则层分流实测
  7. 截断告警必须自解释
  8. token 记账（空重试那次也要算）+ S2 单源条目过多时的分批合并
  9. 引用转发（RSSHub twitter 复合正文）的识别与压缩
"""
import datetime as dt
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "apps"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402
django.setup()

from django.conf import settings  # noqa: E402
# 本脚本只验证逻辑，不往真实 data/cache 写文件；缓存行为由单测覆盖。
settings.SUMMARY_CACHE_ENABLED = False
from django.template.loader import render_to_string  # noqa: E402
from django.utils import timezone  # noqa: E402

from core import triage  # noqa: E402

ok = bad = 0
fails = []


def check(name, cond, detail=""):
    global ok, bad
    if cond:
        ok += 1
        print("  PASS  %s" % name)
    else:
        bad += 1
        fails.append(name)
        print("  FAIL  %s   %s" % (name, detail))


class FakeRating:
    def __init__(self, reason, level, votes=None, prev_level=None, manual_note=""):
        self.reason = reason
        self.level = level
        self.votes = votes if votes is not None else []
        self.prev_level = prev_level
        self.manual_note = manual_note


class FakeEntry:
    def __init__(self, i, title, published):
        self.id = i
        self.title = title
        self.published_at = published
        self.created_at = published
        self.url = "https://example.com/%d" % i
        self.feed_title = "示例源"
        self.feed_id = 1
        self.content = "<p>正文</p>"
        self.unread = True
        self.starred = False


NOW = timezone.now()
LOOKBACK = settings.TRIAGE_LOOKBACK_DAYS

print("=" * 68)
print("1. reason → 状态映射（★ 判据是 reason，不是 level）")
print("=" * 68)
cases = [
    ("llm", 0), ("llm", 3), ("default", 2), ("fail_open", 2), ("manual", 1),
]
for reason, level in cases:
    state = triage.entry_rate_state(FakeRating(reason, level), NOW, now=NOW)
    expect = {"llm": "llm", "default": "default", "fail_open": "fail",
              "manual": "manual"}[reason]
    check("reason=%-9s → %s" % (reason, expect), state == expect, state)

# ★ 关键：同样 level=2，三种 reason 必须给出三种不同状态 —— 这正是原来分不出来的点
same_level = {triage.entry_rate_state(FakeRating(r, 2), NOW, now=NOW)
              for r in ("llm", "default", "fail_open")}
check("同为 L2 的 llm/default/fail_open 得到 3 个不同状态",
      len(same_level) == 3, sorted(same_level))

print()
print("=" * 68)
print("2. 未评级：pending 与 expired 的边界（TRIAGE_LOOKBACK_DAYS=%d）" % LOOKBACK)
print("=" * 68)
inside = NOW - dt.timedelta(days=LOOKBACK) + dt.timedelta(hours=1)
outside = NOW - dt.timedelta(days=LOOKBACK) - dt.timedelta(hours=1)
boundary = NOW - dt.timedelta(days=LOOKBACK)
s_in = triage.entry_rate_state(None, inside, now=NOW)
s_out = triage.entry_rate_state(None, outside, now=NOW)
s_bd = triage.entry_rate_state(None, boundary, now=NOW)
print("   窗口内 %s → %s ｜ 恰好边界 %s → %s ｜ 窗口外 %s → %s"
      % (inside.date(), s_in, boundary.date(), s_bd, outside.date(), s_out))
check("窗口内未评级 → pending", s_in == "pending", s_in)
check("超出窗口未评级 → expired（按设计永不判）", s_out == "expired", s_out)
check("恰好在边界（>= cutoff）→ pending", s_bd == "pending", s_bd)
check("无时间戳的未评级条目 → pending（保守，不误报超窗）",
      triage.entry_rate_state(None, None, now=NOW) == "pending")

print()
print("=" * 68)
print("3. 徽标模板：只分三档，其余信息进 hover")
print("=" * 68)
FAKE = {
    "llm": FakeRating("llm", 2, votes=[{"id": 1, "level": 2, "one_line": "点名标的",
                                        "why": "方向肯定"}, {"id": 1, "level": 2,
                                                             "one_line": "点名标的",
                                                             "why": "方向肯定"}]),
    "manual": FakeRating("manual", 3, prev_level=1, votes=[{"id": 2, "level": 1, "why": "仅标题"}],
                         manual_note="手工调整"),
    "fail": FakeRating("fail_open", 2),
    "default": FakeRating("default", 2),
    "pending": None,
    "expired": None,
}
rows = [{"state": s, "level": (2 if s != "manual" else 3),
         "badge_css": triage.badge_css(s, 2 if s != "manual" else 3),
         "basis": triage.rating_basis(FAKE[s], s, 2 if s != "manual" else 3),
         "rated": s in triage.RATED_STATES,
         "state_meta": triage.rate_meta(s)}
        for s in triage.RATE_STATES]
html = {r["state"]: render_to_string("reader/_level_badge.html", {"row": r}).strip() for r in rows}
for r in rows:
    print("   %-8s %s" % (r["state"], html[r["state"]].replace("\n", " ")[:120]))

check("llm 徽标带 st-llm 且保留级别色 lv2", "st-llm" in html["llm"] and "lv2" in html["llm"])
check("manual 徽标带 st-manual 且保留级别色 lv3",
      "st-manual" in html["manual"] and "lv3" in html["manual"])
check("fail 徽标带 st-fail 且保留级别色 lv2",
      "st-fail" in html["fail"] and "lv2" in html["fail"])
check("default / pending / expired 灰档不带级别色（只留灰编号）",
      all("lv0" not in html[s] and "lv1" not in html[s] and "lv2" not in html[s]
          and "lv3" not in html[s] for s in ("default", "pending", "expired")),
      [html[s] for s in ("default", "pending", "expired")])
check("灰档只带自己的 st-* 类",
      all(triage.rate_meta(s)["css"] in html[s] for s in ("default", "pending", "expired")))

check("徽标文本恰好是 L0–L3（级别数字没丢）",
      all((">L%s<" % r["level"]) in html[r["state"]] for r in rows))
check("徽标不再有角标元素 lvm（默/手/⚠ 已取消）",
      not any("lvm" in h for h in html.values()), [h for h in html.values() if "lvm" in h])
check("徽标不再渲染「待判 / 未判」文字（★ 只看可见文字，hover 文案里说「未判定」是对的）",
      not any(("待判" in h.split(">")[-1] or "未判" in h.split(">")[-1]) for h in html.values()),
      [h.split(">")[-1] for h in html.values()])
check("每个徽标都有非空 title（悬停才有依据）",
      all(len(r["basis"]) > 4 for r in rows), [r["basis"] for r in rows])

check("llm 的 hover 含票数 + 一句话 + 理由",
      "2/2 票" in rows[0]["basis"] and "点名标的" in rows[0]["basis"]
      and "方向肯定" in rows[0]["basis"], rows[0]["basis"])
check("manual 的 hover 说明原级别",
      "原 L1" in triage.rating_basis(FAKE["manual"], "manual", 3))
check("fail 的 hover 明确「不是判定结果」",
      "不是判定结果" in triage.rating_basis(FAKE["fail"], "fail", 2))
check("default 的 hover 说明「没配判据、零成本」",
      "没配分级判据" in triage.rating_basis(FAKE["default"], "default", 2))
check("pending 的 hover 说明还没跑到", "还没跑到" in triage.rating_basis(None, "pending", 2))
check("expired 的 hover 说明超窗不判", "超出 %d 天" % LOOKBACK in
      triage.rating_basis(None, "expired", 2))

sigs = {r["state"]: set(html[r["state"]].split('"')[1].split()) for r in rows}
dupes = [(a, b) for a in sigs for b in sigs if a < b and sigs[a] == sigs[b]]
check("六种状态的 class 组合两两不同（视觉上分得开）", not dupes, dupes)

print()
print("=" * 68)
print("4. 三档契约 + 右键菜单 + 单条判定的护栏")
print("=" * 68)
for s in triage.RATE_STATES:
    m = triage.rate_meta(s)
    css = triage.badge_css(s, 2)
    print("   %-8s tone=%-6s css=%-22s rated=%s" % (s, m["tone"], css, m["rated"]))

check("tone 只有三档：judged / fail / grey",
      {triage.rate_meta(s)["tone"] for s in triage.RATE_STATES} == {"judged", "fail", "grey"})
check("judged 两态 = llm / manual",
      [s for s in triage.RATE_STATES if triage.rate_meta(s)["tone"] == "judged"] == ["llm", "manual"])
check("判过的三态 rated=True（右键给「重新判定」）",
      all(triage.rate_meta(s)["rated"] for s in ("llm", "manual", "fail")))
check("default 虽已入库但 rated=False（右键给「立即判定」——它没真判过）",
      triage.rate_meta("default")["rated"] is False)
check("pending / expired rated=False",
      not any(triage.rate_meta(s)["rated"] for s in ("pending", "expired")))

row_tpl = open(os.path.join(BASE, "templates/reader/_row.html"), encoding="utf-8").read()
badge_tpl = open(os.path.join(BASE, "templates/reader/_level_badge.html"), encoding="utf-8").read()
rows_tpl = open(os.path.join(BASE, "templates/reader/_rows.html"), encoding="utf-8").read()
app_css = open(os.path.join(BASE, "static/app.css"), encoding="utf-8").read()
app_js = open(os.path.join(BASE, "static/app.js"), encoding="utf-8").read()
views_py = open(os.path.join(BASE, "apps/reader/views.py"), encoding="utf-8").read()
urls_py = open(os.path.join(BASE, "apps/triage/urls.py"), encoding="utf-8").read()

check("_row.html 带 data-rated / data-state（右键菜单据此切换）",
      "data-rated" in row_tpl and "data-state" in row_tpl)
check("_rows.html 不再渲染分级统计条", "rate-strip" not in rows_tpl and "tri_stats" not in rows_tpl)
check("views 不再算 _rate_summary（口径只留徽标一处）", "_rate_summary" not in views_py)
check("app.css 已删 rate-strip 样式", ".rate-strip" not in app_css)
check("app.css 里 fail 是红色（#c0392b）", "#c0392b" in app_css)
check("app.css 里灰档去边框（不再当 chip）",
      ".lv.st-default" in app_css and "border-color: transparent" in app_css)
check("app.js 右键菜单按 data-rated 分支",
      "data-rated" in app_js and "重新判定" in app_js and "立即判定" in app_js)
check("/triage 页面路由已删", 'path("triage",' not in urls_py, urls_py[:200])
check("/triage/rate-now 已注册", "triage/rate-now" in urls_py)
check("侧栏不再有「分级」入口",
      'href="/triage"' not in open(os.path.join(BASE, "templates/base.html"),
                                   encoding="utf-8").read())

# 单条判定的护栏：不存在的 id / 无判据的源都要有明确返回，且**不该调模型**
from core import llm as llm_core  # noqa: E402

orig_chat0 = llm_core.chat
llm_core.chat = lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该调用模型"))
try:
    res_missing = triage.rate_single(999999999)
finally:
    llm_core.chat = orig_chat0
print("   rate_single(不存在) ->", res_missing)
check("不存在的条目 → ok=False 且没调模型", res_missing.get("ok") is False, res_missing)

print()
print("=" * 68)
print("5. 元数据完整性（模板取不到 key 会静默渲染空）")
print("=" * 68)
for s in triage.RATE_STATES:
    m = triage.rate_meta(s)
    check("%-8s label/css/tone/hint 齐全" % s,
          bool(m.get("label")) and bool(m.get("css")) and bool(m.get("tone"))
          and bool(m.get("hint")), m)
    check("%-8s css 形如 st-*" % s, m["css"].startswith("st-"), m["css"])
    check("%-8s hint 已格式化（无残留 {}）" % s,
          "{" not in m["hint"] and "}" not in m["hint"], m["hint"])

print()
print("=" * 68)
print("6. 「降级」= 压成一句话（与级别无关）：规则层分流实测")
print("=" * 68)
from core import summary  # noqa: E402
from core import citeguard  # noqa: E402

long_body = "转自：小明 " + "转贴正文" * 60          # 240+ 字，但仍是转贴
reply_body = "回复@someone: " + "互动的废话" * 40     # 200+ 字
cases = [
    ("标题流", "", "标题流源的一条标题"),
    ("互动回复", reply_body, "回复@abc 你好"),
    ("短转贴", "转自：小明 一句话转发。", "转自：小明"),
    ("长转贴", long_body, "转自：小明"),
    ("正常", "这是一篇结构完整的文章正文，包含事实与数字。", "某篇技术文"),
]
printed = {}
for name, body, title in cases:
    kind = summary.classify_kind(title, body)
    item = {"quote": "[#9 · 05:30](https://e.com/1)", "title": title, "text": body,
            "kind": kind, "summary": "", "truncated": False, "raw_len": len(body)}
    rendered = summary._render_item(item)
    printed[name] = (kind, rendered, len(body))
    print("   %-5s kind=%-10s 原文字数 %-4d 入模：" % (name, kind, len(body))
          + rendered.replace("\n", " / ")[:88])

check("无正文 → title_only", printed["标题流"][0] == "title_only")
check("^回复@ → reply", printed["互动回复"][0] == "reply")
check("短转贴（^转自：且正文短）→ repost", printed["短转贴"][0] == "repost")
check("长转贴 → normal（正文够长就不降级，信息没被砍掉）",
      printed["长转贴"][0] == "normal", printed["长转贴"][0])
check("普通正文 → normal", printed["正常"][0] == "normal")

check("标题流：正文位置写「（标题流源，无正文）」，一个字正文都不喂",
      "（标题流源，无正文）" in printed["标题流"][1])
check("标题流：原文本来就没正文", printed["标题流"][2] == 0)
check("短转贴：正文被压到 120 字以内",
      len(printed["短转贴"][1].split("（转贴）")[-1]) <= 120, printed["短转贴"][1][:60])
check("互动回复：正文被压到 200 字以内",
      len(printed["互动回复"][1].split("（互动回复）")[-1]) <= 200)
check("normal：正文原样进（≤ condense 上限）",
      printed["正常"][1].endswith("事实与数字。"))
# 级别是 triage 的事：summary 里根本没有改 level 的代码路径
check("summary 模块不写 EntryRating（降级 ≠ 降级别）",
      "EntryRating.objects.update" not in open(os.path.join(BASE, "core/summary.py"),
                                                encoding="utf-8").read())

# L1 只对 normal 生效 → 一句话的三类不再多花一次调用
# ★★ 打桩要打**被调用点的**命名空间，不是「逻辑上的源头」（2026-11-01 实测付过学费）：
#   `summary.py` 顶部是 `from core.citeguard import _call` ⇒ `summary._call` 是**另一份绑定**，
#   patch `citeguard._call` 只盖住 citeguard 内部（`_call_cited`）；`_s3_pass` 走的是
#   `summary._call`，照样真联网 —— 而本节「调用 0 次」的打印是桩自己数的，桩没跑就恒为 0，
#   于是一边花钱一边全绿。两处都要打，且**必须断言桩真被用过**。
orig_call, orig_sum_call = citeguard._call, summary._call
calls = []
_stub = lambda kind, sys_, usr, st, **kw: (calls.append(kw.get("tag")) or "x" * 80, "")
citeguard._call = _stub
summary._call = _stub
try:
    items = [
        {"id": 1, "kind": "normal", "raw_len": 9999, "text": "a" * 9999,
         "feed_id": 1, "category_id": 1, "title": "长文", "summary": ""},
        {"id": 2, "kind": "repost", "raw_len": 9999, "text": "b" * 9999,
         "feed_id": 1, "category_id": 1, "title": "长转贴", "summary": ""},
        {"id": 3, "kind": "title_only", "raw_len": 9999, "text": "",
         "feed_id": 1, "category_id": 1, "title": "标题流", "summary": ""},
    ]
    lstats = {"calls": 0, "budget_stop": False, "s3": 0, "tokens": 0,
              "reasoning_tokens": 0, "errors": [], "truncated": []}
    picked = summary._s3_pass(items, lstats, None, no_cache=True)
finally:
    citeguard._call, summary._call = orig_call, orig_sum_call
print("   L1 命中：%s（调用 %d 次）" % ([it["id"] for it in picked], len(calls)))
check("超长但非 normal 的条目不进 L1（省一次调用）",
      [it["id"] for it in picked] == [1], [it["id"] for it in picked])
# ★ 反证桩本身：桩没被调用 = 这次真的联网了（钱已花、断言却看不见）
check("★ 桩真被用上（恰好 1 次，证明本节零联网）", len(calls) == 1, calls)

print()
print("=" * 68)
print("7. Q3：截断告警必须自解释（monkeypatch，零成本、不写库）")
print("=" * 68)
from core import llm as llm_core  # noqa: E402

fake_usage = {"prompt_tokens": 800, "completion_tokens": 5000, "cached_tokens": 0,
              "cache_miss_tokens": 800, "reasoning_tokens": 4200,
              "finish_reason": "length"}
orig_chat, orig_record = llm_core.chat, llm_core.record
# ★ _call 现在走 costreport.chat_recorded（pool=POOL_SUMMARY）：闸门会读真实账本。
#   打桩成 0，保持本节「零成本、不写库、结果确定」的约定（8.3 前恢复）。
orig_spent = llm_core.spent_today_cny
llm_core.spent_today_cny = lambda *a, **k: 0.0
llm_core.chat = lambda *a, **k: ("半截正文", fake_usage, "", 1)
llm_core.record = lambda *a, **k: None          # ★ 不能让自检写进开销总账
try:
    tstats = {"calls": 0, "tokens": 0, "reasoning_tokens": 0, "errors": [],
              "truncated": [], "budget_stop": False,
              "s3": 1, "s2": 2, "s1": 3, "s0": 1}
    text, err = citeguard._call("summary_feed", "sys", "user", tstats,
                                feed_id=37, max_tokens=5000,
                                tag="S2 按源 feed=37", label="兼职社区")
    tstats.update({"s3": 8, "s2": 2, "s1": 4, "s0": 1, "calls": 16, "tokens": 51627,
                   "reasoning_tokens": 28257})
    start, end = summary.window_for(dt.date(2026, 10, 15))

    class FakeRow:
        catch_up = True
        attempt = 11
        # ★ `_header` 现在用**记录里的实际完成时间**（手工重建头部时不让时间漂移）；
        #   桩必须跟着模型走，否则报 AttributeError。
        finished_at = dt.datetime(2026, 10, 15, 8, 46, tzinfo=dt.timezone(dt.timedelta(hours=8)))

    header = "\n".join(summary._header(dt.date(2026, 10, 15), start, end, FakeRow(), tstats))
finally:
    llm_core.chat, llm_core.record = orig_chat, orig_record

print("   truncated[0] = %r" % (tstats["truncated"][0] if tstats["truncated"] else None))
check("有正文但 finish_reason=length → 记进 truncated",
      len(tstats["truncated"]) == 1, tstats["truncated"])
check("截断串带「层 + 源名 + 预算」",
      tstats["truncated"] and "S2 按源 feed=37" in tstats["truncated"][0]
      and "兼职社区" in tstats["truncated"][0]
      and "预算 5000 tokens" in tstats["truncated"][0], tstats["truncated"])
check("截断串不再是裸的 (5000) 格式",
      tstats["truncated"] and not tstats["truncated"][0].endswith("(5000)"))

print("   报告头一行：%s" % [l for l in header.splitlines() if "输出上限" in l])
check("报告头含「输出上限」（e2e 依赖这个词）", "输出上限" in header)
check("报告头解释了原因（硬截断 / 共享预算）",
      "硬截断" in header and "共享" in header)
check("报告头带上了源名与预算",
      "兼职社区" in header and "预算 5000 tokens" in header)
check("空输出（推理吃光预算）走的是翻倍重试，不记 truncated",
      tstats["calls"] >= 1 and text == "半截正文", text)

print()
print("=" * 68)
print("8. Q4：token 记账 + S2 单源分批（monkeypatch，零成本、不写库）")
print("=" * 68)

# ---- 8.1 ★ 修复：推理吃光预算的「空重试」那次调用也要计入 stats["tokens"] ----
#   症状（run 18 实测）：报告头 67091 vs LlmUsage 实际 83730，差 16639 全是空重试
#   （feed29 4023+4998、feed37 2618+5000）。原因是累加写在 continue 之后。
EMPTY_USAGE = {"prompt_tokens": 800, "completion_tokens": 5000, "cached_tokens": 0,
               "reasoning_tokens": 5000, "finish_reason": "length"}
CUT_USAGE = {"prompt_tokens": 900, "completion_tokens": 9999, "cached_tokens": 0,
             "reasoning_tokens": 9000, "finish_reason": "length"}
queue = [("", EMPTY_USAGE, "", 1), ("半截正文", CUT_USAGE, "", 1)]
llm_core.chat, llm_core.record = (lambda *a, **k: queue.pop(0)), (lambda *a, **k: None)
try:
    qstats = {"calls": 0, "tokens": 0, "reasoning_tokens": 0, "errors": [],
              "truncated": [], "budget_stop": False, "s3": 0, "s2": 0, "s1": 0, "s0": 0}
    qtext, qerr = citeguard._call("summary_feed", "sys", "user", qstats,
                                  feed_id=29, max_tokens=5000,
                                  tag="S2 按源 feed=29", label="Twitter @Alex Chen")
finally:
    llm_core.chat, llm_core.record = orig_chat, orig_record

print("   调用 %d 次｜tokens=%d｜truncated=%s"
      % (qstats["calls"], qstats["tokens"], qstats["truncated"]))
check("第一次正文为空 → 真的翻倍重试（发起 2 次调用）", qstats["calls"] == 2, qstats["calls"])
check("★ 空重试那次也计入 tokens（修复前漏算 800+5000）",
      qstats["tokens"] == (800 + 5000) + (900 + 9999), qstats["tokens"])
check("截断串报的是翻倍后的预算（10000，不是配置值 5000）",
      bool(qstats["truncated"]) and "预算 10000 tokens" in qstats["truncated"][0],
      qstats["truncated"])
check("截断串与今天报告头逐字同形",
      qstats["truncated"] and qstats["truncated"][0]
      == "S2 按源 feed=29 · Twitter @Alex Chen（预算 10000 tokens）",
      qstats["truncated"][0] if qstats["truncated"] else None)


# ---- 8.2 S2 单源条目过多 → 分批汇总 + 合并 ----
def _mk_l2_items(n):
    # ★ quote 必须带 URL：引用守卫（`_call_cited`）按 `](https://…)` 计数，
    #   没有 URL 的 `[#29 · 10:00]` 会被判成"整段失引"，L2 直接被丢弃。
    return [{"id": i, "quote": "[Alex Chen · 10:00](https://x.com/s/%d)" % i,
             "title": "动态 %d" % i,
             "truncated": False, "summary": "", "kind": "normal",
             "text": "正文 " + "x" * 200, "raw_len": 210,
             "feed_id": 29, "feed_title": "Twitter @Alex Chen", "category_id": 8}
            for i in range(1, n + 1)]


_seen_budgets, _seen_prompts = [], []


def _spy_chat(msgs, **k):
    _seen_budgets.append(k.get("max_tokens"))
    _seen_prompts.append(msgs[1]["content"])
    return ("- 要点 [Alex Chen · 10:00](https://x.com/s/1)",
            {"prompt_tokens": 100, "completion_tokens": 50,
             "reasoning_tokens": 30, "finish_reason": "stop"}, "", 1)


def _run_l2(n, max_calls=None):
    _seen_budgets.clear()
    _seen_prompts.clear()
    st = {"calls": 0, "tokens": 0, "reasoning_tokens": 0, "errors": [],
          "truncated": [], "budget_stop": False, "s3": 0, "s2": 0, "s1": 0, "s0": 0}
    old_calls = settings.SUMMARY_MAX_CALLS
    if max_calls:
        settings.SUMMARY_MAX_CALLS = max_calls
    # ★ 读取点已走 sk.effective()（15s 缓存）：patch settings 后必须清缓存，
    #   否则本用例读到的是上一轮 _run_l2 缓存的旧上限，掐断断言全空。
    from django.core.cache import cache
    cache.delete("settingkit:summary_max_calls")
    llm_core.chat, llm_core.record = _spy_chat, (lambda *a, **k: None)
    try:
        res = summary._s2_pass({29: _mk_l2_items(n)}, st, None, "win", no_cache=True)
    finally:
        llm_core.chat, llm_core.record = orig_chat, orig_record
        settings.SUMMARY_MAX_CALLS = old_calls
        cache.delete("settingkit:summary_max_calls")
    return st, res


BATCH = settings.SUMMARY_S2_BATCH
print("   SUMMARY_S2_BATCH = %s（L2_MIN = %d）" % (BATCH, settings.SUMMARY_S2_MIN))
check("分批阈值 ≥ 2（否则等于没分）", BATCH >= 2, BATCH)
check("分批阈值 > L2_MIN（低门槛源不该被拆）", BATCH > settings.SUMMARY_S2_MIN, BATCH)

st, res = _run_l2(settings.SUMMARY_S2_MIN - 1)
check("低于 L2_MIN 的源根本不调 L2", st["calls"] == 0, st["calls"])

st, res = _run_l2(BATCH)
check("刚好等于阈值 → 一次调用（不分批）", st["calls"] == 1, st["calls"])
check("单批不走合并提示词", not any("汇总成**一整段**" in p for p in _seen_prompts))

st, res = _run_l2(BATCH + 1)
check("阈值 +1 → 2 批 + 1 次合并 = 3 次调用", st["calls"] == 3, st["calls"])

st, res = _run_l2(34)                      # feed=29 今天的真实条数
_merge_prompts = [p for p in _seen_prompts if "分 2 批" in p]
_nums = [int(m.group(1)) for p in _seen_prompts
         for m in [re.search(r"的 (\d+) 条内容", p)] if m]
print("   34 条 → 调用 %d 次｜各批条数 %s（合计 %d）｜合并 %d 次"
      % (st["calls"], _nums, sum(_nums), len(_merge_prompts)))
check("34 条（今天的真实值）→ 3 次调用", st["calls"] == 3, st["calls"])
check("每批 ≤ BATCH，且各批合计 = 全量（分批不丢条目）",
      len(_nums) == 2 and max(_nums) <= BATCH and sum(_nums) == 34, _nums)
check("合并只发一次，且明说「分 2 批」+ 要求合成一整段", len(_merge_prompts) == 1)
check("★ 产出契约不变：一个源仍然只有一段 text",
      len(res) == 1 and isinstance(res.get(29, {}).get("text"), str), list(res))
check("items 仍是全量（L3 的覆盖集不受分批影响）", len(res[29]["items"]) == 34)
check("l2 计数按「源」算，不按批算", st["s2"] == 1, st["s2"])
check("每次调用（含合并）都用 L2 预算",
      bool(_seen_budgets) and all(b == settings.SUMMARY_S2_MAX_TOKENS
                                  for b in _seen_budgets), _seen_budgets)

st, res = _run_l2(61, max_calls=3)
check("调用上限掐断时仍有产出（退化为已完成的批次）", bool(res))
check("★ 覆盖不全必须留痕（否则读者以为这个源就这么多）",
      bool(st["errors"]) and "只完成 2 批" in st["errors"][0], st["errors"])

llm_core.spent_today_cny = orig_spent   # 8.3 起不再有 llm 桩，恢复真实账本读取

# ---- 8.3 预算默认值（源码扫描，防被改回去）----
SETTINGS_SRC = open(os.path.join(BASE, "config", "settings.py"), encoding="utf-8").read()
for key, val in [("SUMMARY_S2_MAX_TOKENS", "12000"), ("SUMMARY_S1_MAX_TOKENS", "9000"),
                 ("SUMMARY_S2_BATCH", "20")]:
    check("settings 默认值 %s = %s" % (key, val),
          '"%s", "%s"' % (key, val) in SETTINGS_SRC, val)
check("预算上调带了依据注释（不许静默改）",
      "S2 5000 / S1 6000 会撞上限" in SETTINGS_SRC)

# ---- 9. 引用转发（RSSHub twitter 的复合正文）的识别与压缩 ----
print()
print("=" * 68)
print("9. 引用转发：本人文字 vs 被引用原文（feed 29 的 79% 噪声）")
print("=" * 68)
from types import SimpleNamespace  # noqa: E402

from core.textutil import split_rsshub_quote, to_plain  # noqa: E402

# 9.1 切分判据
QUOTE_HTML = ('Makes sense<br/><hr style="border:0">'
              '<div class="rsshub-quote">Sam Wang:&ensp;' + "引文" * 200 + "</div>")
head, tail, is_q = split_rsshub_quote(QUOTE_HTML)
check("★ 原始 RSSHub 结构（带 class）→ 认出引用转发", is_q)
check("本人部分不含引文", "Makes sense" in head and "引文" not in head, head[:40])
check("引文部分以被引用者名开头",
      to_plain(tail).startswith("Sam Wang"), to_plain(tail)[:24])

# ★★ Miniflux 入库会 sanitize，class 被剥掉，库里实际长这样：
REAL = "Yes<hr/>Sam Lee:\u2002And this is why they must lose."
check("★★ class 被剥掉后仍认得出（靠「<hr/> + 名字:␣」签名）",
      split_rsshub_quote(REAL)[2], REAL[:40])
check("★ 普通网页的 <hr/> 不算引用转发（不误伤中文源）",
      not split_rsshub_quote("<p>第一段</p><hr/><p>第二段</p>")[2])
check("无 <hr/> → 不是引用转发", not split_rsshub_quote("一条普通推文，没有分隔")[2])

# 9.2 kind 判定
check("is_quote=True → kind=quote",
      summary.classify_kind("t", "body", is_quote=True) == "quote")
check("★ RT @user: … → repost（补上英文转推的盲区）",
      summary.classify_kind("RT @SPCX100T: I built a bot",
                            "RT @SPCX100T: I built a bot") == "repost")
check("中文转贴判据未被破坏",
      summary.classify_kind("转自：小明 一句话", "转自：小明 一句话") == "repost")
check("普通英文推文 → normal", summary.classify_kind("Cool", "Cool") == "normal")
check("★ is_quote 压过正文长度（引用转发永不进 S3 单篇压缩）",
      summary.classify_kind("x", "字" * 5000, is_quote=True) == "quote")


# 9.3 build_items 端到端（不碰库、不花钱）
def _mk(content, title="t", eid=1):
    return SimpleNamespace(id=eid, feed_id=29, feed_title="Twitter @Alex Chen",
                           content=content, title=title, url="https://x.com/a",
                           published_at=timezone.now(), created_at=None)


CAP = settings.SUMMARY_QUOTE_CHARS
LONG_QUOTE = "我的点评" + "<hr/>" + "SomeOne:\u2002" + "别人的长文" * 500
item = summary.build_items([_mk(LONG_QUOTE)], {}, {})[0]
raw_plain_len = len(to_plain(LONG_QUOTE))
print("   引用转发原文 %d 字 → 入模 %d 字（引文上限 %d）"
      % (raw_plain_len, len(item["text"]), CAP))
check("kind=quote", item["kind"] == "quote", item["kind"])
check("★ 本人文字完整保留", item["text"].startswith("我的点评"), item["text"][:20])
check("★ 引文被截到 SUMMARY_QUOTE_CHARS",
      len(item["text"]) <= len("我的点评（引文）SomeOne:\u2002") + CAP,
      len(item["text"]))
check("★ 素材被压到原文的 1/10 以下",
      len(item["text"]) * 10 < raw_plain_len, "%d vs %d" % (len(item["text"]), raw_plain_len))
check("渲染带「（引用转发）」标记", "（引用转发）" in summary._render_item(item))

plain = summary.build_items([_mk("一篇普通文章正文。" * 50, title="普通文")], {}, {})[0]
check("★ 不含 rsshub-quote 的源行为不变（仍走 condense，kind=normal）",
      plain["kind"] == "normal" and plain["is_quote"] is False, plain["kind"])

SETTINGS_SRC2 = open(os.path.join(BASE, "config", "settings.py"), encoding="utf-8").read()
check("settings 默认值 SUMMARY_QUOTE_CHARS = 120",
      '"SUMMARY_QUOTE_CHARS", "120"' in SETTINGS_SRC2)
check("引文上限带了依据注释（不许静默改）", "被引用方写了" in SETTINGS_SRC2)

# ---------------------------------------------------------------- 10. L3 不加星
# ★ 2026-10-28 定稿：L3 = 候选（+通知），星标只来自列表页 ★ 按钮的人工点击。
#   这四条是防回归的钉子 —— 任何一处想加回「判 L3 自动 star」都会先红。
import inspect  # noqa: E402
from core.datasource import base as ds_base  # noqa: E402

APPLY_SRC = inspect.getsource(triage._apply)
check("★ _apply 里没有 toggle_star（L3 处置只剩 L0 标已读）",
      "toggle_star" not in APPLY_SRC)
check("★ _apply 的 docstring 写明了为什么 L3 不加星（防止后人当 bug 修回去）",
      "手动确认" in APPLY_SRC or "人工" in APPLY_SRC or "手动" in APPLY_SRC)

VIEWS_TRIAGE_SRC = open(os.path.join(BASE, "apps", "triage", "views.py"),
                        encoding="utf-8").read()
check("★ set_level（手工调级）也不加星（加星只走 hx/star 一个人口）",
      "toggle_star" not in VIEWS_TRIAGE_SRC)

MODELS_SRC = open(os.path.join(BASE, "core", "models.py"), encoding="utf-8").read()
check("★ EntryRating 不再有 star_applied / manual_star 死字段（0010 迁移已删）",
      "star_applied" not in MODELS_SRC and "manual_star" not in MODELS_SRC)


class _SpyDS:
    """行为级验证：L3 一律零动作，L0 只走 mark_read。"""

    def __init__(self):
        self.marked, self.starred = [], []

    def mark_read(self, ids):
        self.marked += list(ids)
        return len(ids)

    def toggle_star(self, eid, want):
        self.starred.append((eid, want))
        return True


_spy = _SpyDS()
check("★ _apply(L3) 零动作、零副作用",
      triage._apply([1, 2], 3, "llm", _spy) == 0
      and not _spy.starred and not _spy.marked)
check("★ _apply(L0, fail_open) 也标已读（default_level=0 的显式选择）",
      triage._apply([1], 0, "fail_open", _spy) == 1 and _spy.marked == [1])
_spy.marked = []
check("★ _apply(L0, llm) 正常标已读",
      triage._apply([7, 8], 0, "llm", _spy) == 2 and _spy.marked == [7, 8])
check("★ 全程没有任何 toggle_star 调用", not _spy.starred)

_rv_src = open(os.path.join(BASE, "apps", "reader", "views.py"), encoding="utf-8").read()
# ★ 只看 LEVEL_FILTERS 里的**筛选文案**：全文搜关键词会被无关注释误伤
#   （2026-10-29 就踩过：加星文档里写「Miniflux 已加星 78 篇」把这条判红）。
_m = re.search(r"LEVEL_FILTERS\s*=\s*\[(.*?)\]", _rv_src, re.S)
_labels = " ".join(re.findall(r'"([^"]*)"', _m.group(1))) if _m else _rv_src
check("★ 列表页 L3 筛选文案不再写「已加星」", "已加星" not in _labels, _labels)
check("★ 数据源层 toggle_star 仍在（hx/star 手动入口要靠它）",
      "def toggle_star" in inspect.getsource(ds_base))

# 2026-11-23：上游提前断开 chunked 响应抛 IncompleteRead（HTTPException，
# 不是 URLError）。漏捕获 → 客户端 3 次重试不执行、chat_recorded 不记账、
# scheduler._safe 打断整轮 triage。两条出口都要接住。
_llm_src = open(os.path.join(BASE, "core", "llm.py"), encoding="utf-8").read()


def _fn_slice(src, name):
    m = re.search(r"^def %s\(" % re.escape(name), src, re.M)
    if not m:
        return ""
    nxt = re.search(r"^def ", src[m.end():], re.M)
    return src[m.start():m.end() + (nxt.start() if nxt else len(src) - m.end())]


check("★ chunked 断流走客户端重试，不打断整轮 triage",
      "import http.client" in _llm_src
      and "http.client.HTTPException" in _fn_slice(_llm_src, "chat")
      and "http.client.HTTPException" in _fn_slice(_llm_src, "chat_stream"),
      "IncompleteRead 是 HTTPException 不是 URLError：漏掉它重试与记账全失效")

print()
print("=" * 68)
print("结果：%d 通过 / %d 失败" % (ok, bad))
if fails:
    print("失败项：")
    for f in fails:
        print("  - %s" % f)
print("=" * 68)
sys.exit(1 if bad else 0)
