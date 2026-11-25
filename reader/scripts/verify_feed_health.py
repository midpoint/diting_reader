"""零成本验证 `scripts/feed_health.py` 的期望值推导（不联网、不读真库、不写基线）。

用法：python scripts/verify_feed_health.py

★ 这个脚本存在的理由：健康检查的阈值原来是**硬编码**的一张 `feed_id → 阈值`
  表。它既泄露订阅列表（开源即公开），又会随 id 漂移而静默失效。改成"按源自己的
  发布节奏推"之后，必须有测试钉死三件事：
    1. 源码里**没有** per-feed 的 id 表（回归防线：不允许再写回来）；
    2. 突发式更新（同一天连发几条）**不会**把阈值压到"周末没发就报警"；
    3. 归档源 / 天生无正文的源，走**本地覆盖**而不是改代码。
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "..", "scripts"))

import feed_health as fh  # noqa: E402

ok = bad = 0
fails = []


def check(name, cond, detail=""):
    global ok, bad
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        bad += 1
        fails.append(name)
        print(f"  ❌ {name}   {detail}")


NOW = datetime.now(timezone.utc)


def ents(ages_days, clens=None):
    """造 entries：ages_days 是「距今天数」列表（升序=最新在前）。"""
    clens = clens or [1000] * len(ages_days)
    rows = [{"published_at": (NOW - timedelta(days=d)).isoformat(), "content": "x" * c}
            for d, c in zip(ages_days, clens)]
    return {"total": len(rows), "entries": rows}


def feed(url="http://example.com/f", err=0, disabled=False):
    return {"id": 1, "title": "t", "feed_url": url,
            "checked_at": NOW.isoformat(), "parsing_error_count": err,
            "disabled": disabled, "category": {"title": "c"}}


def levels(problems):
    return [lv for lv, _ in problems]


def msgs(problems):
    return " | ".join(m for _, m in problems)


SRC = open(os.path.join(BASE, "..", "scripts", "feed_health.py"), encoding="utf-8").read()

print("\n== 1. 源码契约：不许再有 per-feed 硬编码表 ==")
check("EXPECT 表已删除（回归防线）", "EXPECT" not in SRC,
      "又出现 EXPECT 了？硬编码 feed_id→阈值 会泄露订阅列表且随 id 漂移")
check("没有以数字 id 为键的阈值字面量", "  2:  {\"max_age_days\"" not in SRC)
for kw in ("雪球", "豆瓣", "小宇宙", "知乎", "karpathy", "阮一峰", "电鸭", "老李", "老张"):
    check(f"源码不含订阅痕迹「{kw}」", kw not in SRC)
check("覆盖文件默认落在 data/（被 .gitignore 的 data/* 覆盖）",
      fh.OVERRIDE_PATH.endswith(os.path.join("data", "feed_expect.json")))
check("基线按 feed_url 取（不是 feed_id）", "baseline.get(url)" in SRC)
check("基线按 feed_url 写", 'key = s.get("url")' in SRC)
check("有 --expect 参数（本地覆盖入口）", '"--expect"' in SRC)

print("\n== 2. 节奏推导：阈值随源自己走 ==")
# 突发式：同一天连发 4 条（间隔≈0.01 天），再安静 5 天
burst = ents([0, 0.01, 0.02, 0.03, 5, 5.01, 5.02, 5.03])
e = fh.derive_expect(burst)
check(f"突发式源阈值不被压到 3 天下限（得 {e['max_age_days']}）",
      e["max_age_days"] >= 7, e)
check("突发式源阈值不会被抬得离谱（≤30 天）", e["max_age_days"] <= 30, e)

weekly = ents([0, 7, 14, 21, 28, 35])
e = fh.derive_expect(weekly)
check(f"周更源阈值 ≈ 3 周（得 {e['max_age_days']}）", 14 <= e["max_age_days"] <= 30, e)

archived = ents([0, 200, 500, 900, 1400])
e = fh.derive_expect(archived)
check(f"季更/年更源阈值自动放宽（得 {e['max_age_days']}）", e["max_age_days"] >= 600, e)

check("样本不足（2 条）→ 退回 DEFAULTS 30 天",
      fh.derive_expect(ents([0, 3]))["max_age_days"] == fh.DEFAULTS["max_age_days"])
check("空源不炸", fh.derive_expect({"total": 0, "entries": []})["max_age_days"] == 30)

print("\n== 3. 本地覆盖：只认白名单字段，键是 url ==")
ov = fh.derive_expect(weekly, {"max_age_days": 3650, "why": "归档", "乱写": 1})
check("覆盖生效", ov["max_age_days"] == 3650, ov)
check("why / 未知字段被过滤（不会污染判据）",
      "why" not in ov and "乱写" not in ov, ov)

tmp = tempfile.mkdtemp()
check("覆盖文件不存在 → 空 dict（新人 clone 下来就该这样）",
      fh.load_overrides(os.path.join(tmp, "nope.json")) == {})
p_bad = os.path.join(tmp, "bad.json")
open(p_bad, "w").write("{不是 json")
check("坏 JSON 不抛异常（体检不该因覆盖文件挂掉）", fh.load_overrides(p_bad) == {})
p_wrap = os.path.join(tmp, "wrap.json")
json.dump({"feeds": {"u": {"max_age_days": 9}}}, open(p_wrap, "w"))
check('允许 {"feeds": {...}} 包一层', fh.load_overrides(p_wrap) == {"u": {"max_age_days": 9}})

print("\n== 4. 陈旧判据：用推出来的阈值，不是 30 天一刀切 ==")
# 周更源安静 20 天：阈值 ~21 天 → 不报（这就是"周更源静默 3 周才可疑"）
lv, pr, _ = fh.check_feed(feed(), ents([20, 27, 34, 41, 48, 55]), fh.derive_expect(weekly), "", False)
check("周更源安静 20 天不报陈旧（阈值≈21）", "最新条目已" not in msgs(pr), msgs(pr))
# 同一个源安静 60 天 → 报
lv, pr, _ = fh.check_feed(feed(), ents([60, 67, 74, 81, 88, 95]), fh.derive_expect(weekly), "", False)
check("周更源安静 60 天报陈旧", "最新条目已" in msgs(pr), msgs(pr))
# 归档源安静 3 年：走覆盖后不报
lv, pr, _ = fh.check_feed(feed(), ents([1100, 1300, 1700, 2100, 2500]),
                          fh.derive_expect(archived, {"max_age_days": 3650}), "", False)
check("归档源（覆盖 3650 天）安静 3 年不报", "最新条目已" not in msgs(pr), msgs(pr))

print("\n== 5. 正文判据：三条都不依赖 per-feed 数字 ==")
lv, pr, _ = fh.check_feed(feed(), ents([0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [0] * 10),
                          fh.derive_expect(weekly), "", False)
check("整源无正文（≥10 条全 0）→ WARN", "整源无正文" in msgs(pr), msgs(pr))
lv, pr, _ = fh.check_feed(feed(), ents([0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [0] * 10),
                          fh.derive_expect(weekly, {"allow_empty_content": True}), "", False)
check("allow_empty_content 放行天生无正文的源", "整源无正文" not in msgs(pr), msgs(pr))
lv, pr, _ = fh.check_feed(feed(), ents([0, 1, 2], [0, 0, 0]),
                          fh.derive_expect(weekly), "", False)
check("只有 3 条空正文不报（样本不足，可能是标题流）", "整源无正文" not in msgs(pr), msgs(pr))

# 基线骤降：基线 3000 → 现在 1200
lv, pr, _ = fh.check_feed(feed(), ents([0, 1, 2, 3, 4, 5], [1200] * 6),
                          fh.derive_expect(weekly), "", False, prev={"content_med": 3000})
check("正文较基线骤降 → WARN", "骤降" in msgs(pr), msgs(pr))
lv, pr, _ = fh.check_feed(feed(), ents([0, 1, 2, 3, 4, 5], [1800] * 6),
                          fh.derive_expect(weekly), "", False, prev={"content_med": 3000})
check("跌 40% 但没破半 → 不报（避免抖动即报警）", "骤降" not in msgs(pr), msgs(pr))
lv, pr, _ = fh.check_feed(feed(), ents([0, 1, 2, 3, 4, 5], [20] * 6),
                          fh.derive_expect(weekly), "", False, prev={"content_med": 50})
check("基线本来就短（50 字）→ 不报骤降（标题流源不误伤）", "骤降" not in msgs(pr), msgs(pr))
lv, pr, _ = fh.check_feed(feed(), ents([0, 1, 2, 3, 4, 5], [1200] * 6),
                          fh.derive_expect(weekly), "", False, prev=None)
check("首跑没有基线 → 不报骤降（新源不误伤）", "骤降" not in msgs(pr), msgs(pr))

# 哨兵：最新两条都短 vs 只有最新一条短
lv, pr, _ = fh.check_feed(feed(), ents([0, 1, 2, 3, 4, 5], [100, 120] + [3000] * 4),
                          fh.derive_expect(weekly), "", False)
check("最新两条都骤短 → WARN（选择器刚坏）", "最新两条" in msgs(pr), msgs(pr))
lv, pr, _ = fh.check_feed(feed(), ents([0, 1, 2, 3, 4, 5], [100] + [3000] * 5),
                          fh.derive_expect(weekly), "", False)
check("只有最新一条短 → 不报（单条短帖很常见）", "最新两条" not in msgs(pr), msgs(pr))

print("\n== 6. min_content 只剩「本地显式设定」一条路 ==")
lv, pr, _ = fh.check_feed(feed(), ents([0, 1, 2, 3, 4, 5], [50] * 6),
                          fh.derive_expect(weekly), "", False)
check("默认不设绝对下限（短源不再常驻 WARN）", "本地设定下限" not in msgs(pr), msgs(pr))
lv, pr, _ = fh.check_feed(feed(), ents([0, 1, 2, 3, 4, 5], [50] * 6),
                          fh.derive_expect(weekly, {"min_content": 500}), "", False)
check("本地写了 min_content 才按绝对线判", "本地设定下限" in msgs(pr), msgs(pr))

print("\n== 7. 与本次改动无关但会被连带影响的既有判据仍在 ==")
lv, pr, _ = fh.check_feed(feed(err=2), ents([0, 1, 2]), fh.derive_expect(weekly), "", False)
check("解析错误 → ERROR 级", "ERROR" in levels(pr), pr)
lv, pr, _ = fh.check_feed(feed(disabled=True), ents([0]), fh.derive_expect(weekly), "", False)
check("被禁用 → ERROR 级", "ERROR" in levels(pr), pr)
lv, pr, _ = fh.check_feed(feed(), ents([]), fh.derive_expect(weekly), "", False)
check("从未入库 → WARN", any("从未入库" in m for _, m in pr), msgs(pr))

print(f"\n结果：{ok} 通过 / {bad} 失败")
if bad:
    print("失败项：" + "、".join(fails))
sys.exit(1 if bad else 0)
