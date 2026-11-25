"""TriageWorker（M3）：定期把新文章分級、写库、执行处置。

流程（设计 §6.1）：
  1. 扫最近条目（含已读）→ 与 EntryRating 做差集 → 待判
  2. 四段合并后全空 → 直接写 default_level，reason=default（零 LLM）
  3. 否则取该 feed 的 active prompt（没有就现建，见 promptbuilder）
     → **按 feed 分批**调 LLM（prompt 是 feed 级的，不能跨 feed 混批）
     → 投票 + anti-FOMO 聚合
     → fail-open：掉到 default_level，reason=fail_open
  4. 写 EntryRating + LlmUsage
  5. 处置写回 Miniflux：L0 → 标已读（★ fail_open 的那批**不标**，故障不能造成不可逆丢失）
     ★★ L3 **不加星**：加星 = 读者的手动确认（列表页 ★ 按钮），分级只负责把候选
        送进 L3（+ 系统通知）。这样 star 的语义是单一的 ——「我读过并认可」，
        而不是「模型猜我会喜欢」；将来 star → LLM wiki 的入料也只信这个。
"""
import logging
import re
import threading
import time
from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from core import costreport as cr
from core import jsontool, llm, promptbuilder, promptspec, settingkit as sk, treekit
from core.datasource import get_datasource
from core.logkit import fields
from core.models import EntryRating
from core.textutil import render_layers
from typing import Optional

log = logging.getLogger(__name__)

MIN_VOTES_FOR_L0 = 2


def votes_done(levels, repeat: int, adaptive: bool) -> bool:
    """逐票评估的续票停止条件（自适应开）：票箱里已有证据够不够下结论。

    - 出现 **L2/L3 任意一票 → 立即停**：有一票说「值得」就不可能再被丢，继续
      投只会加钱，救回不需要凑证据；
    - **当前最高是 L1 → 满 2 票停**：复核票杀不掉内容（全票 L0 才判 L0），
      只可能再抬一级，两票足够；
    - **全是 L0 → 满 repeat 票（默认 3）**才考虑丢：丢弃是唯一不可逆动作。
    停下的票箱照样交给 aggregate_level 聚合，anti-FOMO 不变量（全票 L0、分歧
    保留、平票取高、<2 票不判 L0）只在聚合处生效，这里只管省钱。
    自适应关掉时无条件投满 repeat 票。
    """
    n = len(levels)
    r = max(1, int(repeat))
    if not adaptive:
        return n >= r
    if n == 0:
        return False
    top = max(levels)
    if top >= 2:
        return True
    if top == 1:
        return n >= min(2, r)
    return n >= r


# ------------------------------------------------------------------ 分级状态（给人看）
# ★ 同一个「L2」有四种完全不同的来源，光看级别数字分不出来：
#     llm      模型真判的（花了钱、读了正文）
#     default  源没配分级判据 → 零成本直接落 default_level（没花钱、没读正文）
#     fail     调用失败/无有效票 → fail-open 掉到 default_level（**不是**判定结果）
#     manual   人工改过
#   再叠加「未判」的两种：pending（窗口内，下一轮会判）/ expired（超窗，按设计永不判）。
#
#   实测 2026-10-15：1068 条评级里 default 907、llm 128、fail_open 30、manual 3
#   —— 只显示 "L2" 会把 85% 的**零成本默认**误读成"分级结果"，
#   而 fail-open 的 30 条会和真判的 L2 长得一模一样。这就是「看不出是没跑还是 fail」的根因。
RATE_STATES = ("llm", "default", "fail", "manual", "pending", "expired")

# ★ 视觉只分三档（2026-10-15 收敛：角标/「待判」文字全去掉了 —— 列表是读文章的地方，
#   不是看分级报表的地方，级别的「来源」按需 hover 出来即可）：
#     彩色（级别色）  = 判过的（llm 模型判 / manual 人工调）
#     红虚线          = fail 失败兜底（级别不可信，需要人扫一眼）
#     灰虚线          = 没判定（default 源没配判据 / pending 没跑到 / expired 超窗）
#   hint 只做 tooltip 兜底，真正的悬停文案由 rating_basis() 按条目生成。
RATE_STATE_META = {
    "llm": {"label": "模型判定", "css": "st-llm", "tone": "judged",
            "hint": "模型判定：按该源的分级判据真判过（花了一次调用）"},
    "manual": {"label": "人工调整", "css": "st-manual", "tone": "judged",
               "hint": "人工调整过级别"},
    "fail": {"label": "失败兜底", "css": "st-fail", "tone": "fail",
             "hint": "失败兜底（fail-open）：调用失败或没拿到有效票，沿用默认级别。"
                     "这不是判定结果，级别不可信"},
    "default": {"label": "未判定", "css": "st-default", "tone": "grey",
                "hint": "未判定：该源（及其分类）没配分级判据，"
                        "级别恒等于默认值 —— 没有调用过模型"},
    "pending": {"label": "未判定", "css": "st-pending", "tone": "grey",
                "hint": "未判定：分级 worker 尚未跑到这条（在 {days} 天判级窗口内）"},
    "expired": {"label": "未判定", "css": "st-expired", "tone": "grey",
                "hint": "未判定：超出 {days} 天判级窗口，按设计不再判级"},
}

# tone=judged 的才带级别色（"级别是判出来的"才配得上颜色）
JUDGED_STATES = ("llm", "manual")
# 库里已有评级记录的三态（default 也算有记录，但**不算判过** —— 见 RATE_STATE_META）
RATED_STATES = ("llm", "manual", "fail")

_REASON_TO_STATE = {"fail_open": "fail", "default": "default", "manual": "manual"}


def entry_rate_state(rating, published=None, now=None) -> str:
    """把 (Optional[EntryRating], published_at) 归一成上面六种状态之一。

    ★ 判据是 `reason` 而不是 `level` —— 级别数字恰恰是分不出来的那个东西。
    """
    if rating is not None:
        return _REASON_TO_STATE.get(rating.reason, "llm")
    if published is not None:
        cutoff = (now or timezone.now()) - timedelta(days=sk.effective("triage_lookback_days"))
        if published < cutoff:
            return "expired"
    return "pending"


def rate_meta(state: str) -> dict:
    """状态的展示用元数据（label / css / tone / tooltip 兜底）。"""
    meta = dict(RATE_STATE_META.get(state) or RATE_STATE_META["pending"])
    meta["state"] = state
    # ★ 「判过没有」与「库里有记录没有」是两回事：default 有记录但没判过，
    #   右键菜单据此给「立即判定」而不是「重新判定」。
    meta["rated"] = state in RATED_STATES
    meta["hint"] = meta["hint"].format(days=sk.effective("triage_lookback_days"))
    return meta


def badge_css(state: str, level: int) -> str:
    """级别徽标的 class：灰档不带级别色，判过的/fail 带级别色再叠来源色。"""
    meta = RATE_STATE_META.get(state) or RATE_STATE_META["pending"]
    if meta["tone"] == "grey":
        return meta["css"]
    return f"lv{level} {meta['css']}"


def _brief(text, n: int = 56) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[:n] + "…"


def rating_basis(rating, state: str, level: int) -> str:
    """hover 文案：「为什么是 L{n}」。

    依据来自 EntryRating.votes —— 模型每一票都带 one_line（一句话结论）与
    why（理由），这两样才是级别的**依据**，级别数字本身不是。
    """
    days = sk.effective("triage_lookback_days")
    votes = rating.votes if (rating is not None and isinstance(rating.votes, list)) else []
    levels = [v.get("level") for v in votes if isinstance(v, dict) and isinstance(v.get("level"), int)]

    if state == "llm":
        head = f"模型判定 L{level}"
        if levels:
            top_n = Counter(levels).most_common(1)[0][1]
            head += f"（{top_n}/{len(levels)} 票）"
        for v in votes:
            one, why = _brief(v.get("one_line")), _brief(v.get("why"))
            if one or why:
                return " · ".join(x for x in (head, one, why) if x)
        return head
    if state == "manual":
        parts = [f"人工调整 L{level}"]
        if rating.prev_level is not None:
            parts.append(f"原 L{rating.prev_level}")
        note = _brief(rating.manual_note)
        if note and note != "手工调整":
            parts.append(note)
        for v in votes:                      # 保留改动前的模型理由，便于复核
            if isinstance(v, dict) and isinstance(v.get("level"), int):
                parts.append("曾判：" + (_brief(v.get("why")) or _brief(v.get("one_line"))))
                break
        return " · ".join(parts)
    if state == "fail":
        return (f"失败兜底：这次没拿到有效票，沿用默认级别 L{level}"
                f"（不是判定结果，可右键「重新判定」）")
    if state == "default":
        return f"未判定：该源（及其分类）没配分级判据，级别恒为默认 L{level}（零成本，没调模型）"
    if state == "expired":
        return f"未判定：超出 {days} 天判级窗口，按设计不再判（列表按未评级 L{level} 显示）"
    return f"未判定：分级 worker 还没跑到这条，暂按 L{level} 显示（窗口 {days} 天）"


def count_states(states) -> dict:
    """[{state}] → 各态计数（六种键都在，便于模板直接取）。"""
    out = {s: 0 for s in RATE_STATES}
    for s in states:
        out[s] = out.get(s, 0) + 1
    return out


def pending_count(ttl: int = 60):
    """全库待判条数（带缓存；取不到返回 None 而不是 0 —— 别把故障显示成"已判完"）。"""
    from django.core.cache import cache

    key = "triage:pending_count"
    try:
        got = cache.get(key)
    except Exception:  # noqa: BLE001
        got = None
    if got is not None:
        return got
    try:
        got = sum(len(v) for v in pending_entries().values())
    except Exception as exc:  # noqa: BLE001  Miniflux 挂了不该拖垮列表页
        log.warning("统计待判条数失败：%s", exc)
        return None
    try:
        cache.set(key, got, ttl)
    except Exception as exc:  # noqa: BLE001  缓存写失败只是每次都重算，不影响正确性
        log.debug("待判条数缓存写入失败（每次都会重算）：%s", exc,
                  extra=fields(src="triage"))
    return got


# ------------------------------------------------------------------ 投票聚合
def normalize_level(value):
    if value is None:
        return None
    m = re.search(r"L?([0-3])", str(value).strip().upper().replace(" ", ""))
    return int(m.group(1)) if m else None


def aggregate_level(votes, default_level: int):
    """anti-FOMO 聚合。**这里是权威实现**（早期原型已随开源清理移出仓库；
    它另有跨模型复检 --l0-check/--l3-check 尚未移植，备忘见 scripts/README.md）。

    1. 有效票 < 2 → 唯一票 L0 落 default_level（不直接判 L0；default=0 时按配置落 L0）
    2. 全票 0 才判 L0
    3. 多数 0 但有分歧 → 抬到 max(default_level, 1)（保留）
    4. 否则取多数派，平票取高；无有效票 → fail-open 掉到 default_level
    """
    levels = [lv for lv in (normalize_level(v.get("level")) for v in votes) if lv is not None]
    n = len(levels)
    counts = Counter(levels)
    info = {"n": n, "counts": {str(k): v for k, v in counts.items()}, "reason": ""}

    if n == 0:
        info["reason"] = "无有效票 → fail-open"
        return default_level, info
    if n < MIN_VOTES_FOR_L0:
        info["reason"] = f"有效票仅 {n} 张，不判 L0"
        return (levels[0] if levels[0] != 0 else default_level), info
    if counts.get(0, 0) == n:
        info["reason"] = f"全票 {n}/{n} 判 L0"
        return 0, info

    best = counts.most_common()[0][1]
    winners = [lv for lv, c in counts.items() if c == best]
    level = winners[0] if len(winners) == 1 else max(winners)
    if level == 0:
        info["reason"] = f"多数({best}/{n})说丢但有分歧 → 保留"
        return max(default_level, 1), info
    info["reason"] = f"多数派 {best}/{n}" if len(winners) == 1 else f"平票 {best}/{n} → 取高"
    return level, info


# ------------------------------------------------------------------ 数据准备
def _feed_index():
    """{feed_id: FeedNode} + {feed_id: category_id}（共享实现见 core.treekit）。"""
    return treekit.feed_index()


def pending_entries(feed_ids=None, limit=None):
    """扫最近条目（含已读），与 EntryRating 做差集。返回 {feed_id: [Entry]}。

    两道防 token 跑超的闸：
      1. 时间窗：只判 published_at/created_at 在 TRIAGE_LOOKBACK_DAYS 天内的条目；
      2. feed 粒度：每个 feed 每轮最多 TRIAGE_PER_FEED_LIMIT 条（超出留到下一轮）。
    """
    ds = get_datasource()
    entries = ds.list_entries(status="all", limit=limit or settings.TRIAGE_SCAN_LIMIT)
    if feed_ids:
        entries = [e for e in entries if e.id and e.feed_id in feed_ids]
    rated = set(EntryRating.objects.filter(
        entry_id__in=[e.id for e in entries]).values_list("entry_id", flat=True))

    cutoff = timezone.now() - timedelta(days=sk.effective("triage_lookback_days"))
    out = {}
    for e in entries:
        if e.id in rated:
            continue
        ts = e.published_at or e.created_at
        # 时间窗外的历史文章一律不判（新源一次性导入大量历史时尤其关键）
        if ts is not None and ts < cutoff:
            continue
        # feed 粒度上限：满了就跳过，留到下一轮
        if len(out.get(e.feed_id, [])) >= settings.TRIAGE_PER_FEED_LIMIT:
            continue
        out.setdefault(e.feed_id, []).append(e)
    return out


def _render_items(entries):
    return "\n\n".join(
        f"--- id={e.id} | {(e.published_at or e.created_at or timezone.now()):%Y-%m-%d %H:%M} ---\n"
        f"标题：{(e.title or '').strip()[:120]}\n"
        f"正文：{render_layers(e.content, sk.effective('triage_char_limit'))}"
        for e in entries)


def parse_rows(text):
    """从模型输出里捞出 [{id, level, one_line, why}]（JSON 提取见 core.jsontool）。"""
    rows = []
    for data in jsontool.iter_json(text):
        if isinstance(data, dict):
            data = [data]
        for row in data if isinstance(data, list) else []:
            lv = normalize_level(row.get("level"))
            if lv is None or row.get("id") is None:
                continue
            try:
                rows.append({"id": int(row["id"]), "level": lv,
                             "one_line": str(row.get("one_line", ""))[:80],
                             "why": str(row.get("why", ""))[:80]})
            except (TypeError, ValueError):
                continue
        if rows:
            return rows
    return []


# ------------------------------------------------------------------ 处置
def _notify_l3(entry, reason: str) -> int:
    """L3 点名的系统通知（1 = 已提交，0 = 没发）。

    ★ 只通知**模型判出来**的（`reason == "llm"`）：
      · 源配了 `default_level=3` 的零成本路径条条 L3 —— 通知没有信息量，只会刷屏；
      · `fail_open` 是故障兜底值，不是"点名"；
      · 手工改级是你自己在页面上点的，不必再弹一次。
      这三条都在调用点拦掉了，这里只做一道防御性复检。
    """
    if reason != "llm":
        return 0
    try:
        from core import notify

        return 1 if notify.l3(entry) else 0
    except Exception as exc:  # noqa: BLE001  通知失败绝不影响分级
        log.warning("L3 通知失败（不影响分级）：%s", exc)
        return 0


def _apply(entry_ids, level, reason, ds):
    """L0 → 标已读。

    ★★ **fail_open 落 L0 也标已读**（2026-10-29 用户定稿）：fail-open 只掉到
      `default_level`；只有源显式把 default_level 设为 0 时才会落 L0。
      既然默认 L0 代表「这个源基本是垃圾、可以大量丢」，就按配置执行。
      这与「有效票 < 2 且唯一票 L0 → default_level」同一口径，不再对 fail_open
      单独做「只记级、不标已读」的保护。
    ★★ **没有 L3 分支**：2026-10-28 起 L3 不再加星。理由：star 必须是
      人在列表页的手动确认（★ 按钮），分级判 L3 只等于「候选 + 通知」；
      两者语义混同会让「加星率」这个信号失真 —— 自动加的星既不代表
      读者读过，也不能作为将来 star → wiki 的可信入料。
    """
    if not entry_ids:
        return 0
    if level == 0:
        try:
            return ds.mark_read(list(entry_ids))
        except Exception as exc:
            log.warning("mark_read 失败：%s", exc)
            return 0
    return 0


# ------------------------------------------------------------------ 主流程
# ★ 进程内互斥：调度器每 TRIAGE_INTERVAL_MIN 分钟触发一次，页面又有「立即跑一轮」
#   按钮 —— 两轮并发会各自扫到同一批待判条目、重复调用、白花钱。
_RUN_LOCK = threading.Lock()


def is_running() -> bool:
    return _RUN_LOCK.locked()


def run_once(feed_ids=None, dry_run=False, max_calls=None):
    """跑一轮。返回统计 dict（已有别的轮次在跑时返回 {"skipped": …}）。"""
    if not sk.effective("triage_enabled"):
        # scheduler 无条件注册，总开关在这里判：设置页关掉后下一轮 job 醒来
        # 只返回 skipped，不再调 LLM（dry_run 是验证工具，不受开关约束）。
        return {"skipped": "分级已关闭（设置页 → 分级）"}
    if not _RUN_LOCK.acquire(blocking=False):
        log.warning("已有一轮分级在跑，本次跳过（防重复调用）")
        return {"skipped": "已有一轮在跑"}
    t0 = time.monotonic()
    try:
        stats = _run_once(feed_ids=feed_ids, dry_run=dry_run, max_calls=max_calls)
    finally:
        _RUN_LOCK.release()
    stats["ms"] = int((time.monotonic() - t0) * 1000)
    _log_run(stats)
    try:
        from django.core.cache import cache

        cache.delete("triage:pending_count")   # 状态条上的待判数立刻刷新
    except Exception:  # noqa: BLE001
        pass
    return stats


def _log_run(stats):
    """一轮分级的收尾日志。

    ★ 分级每 5 分钟一轮，"什么都没有"是常态 ⇒ 无事发生记 DEBUG，
      真有判定动作才记 INFO。否则日志会被 288 行/天的"无事发生"淹没。
    """
    f = fields(src="triage", pending=stats.get("pending"), rated=stats.get("rated"),
               llm=stats.get("llm_calls"), l0=stats.get("l0_marked"),
               l3n=stats.get("l3_notified"),
               fail=stats.get("fail_open"), ms=stats.get("ms"))
    if not stats.get("pending"):
        log.debug("分级一轮：无待判条目（耗时 %sms）", stats.get("ms"), extra=f)
        return
    log.info("分级一轮：%s 个源 / 待判 %s 条 → 判定 %s（零成本默认 %s · 失败兜底 %s）"
             "｜LLM %s 次｜标已读 %s · L3 通知 %s｜%sms",
             stats.get("scanned_feeds"), stats.get("pending"), stats.get("rated"),
             stats.get("defaulted"), stats.get("fail_open"), stats.get("llm_calls"),
             stats.get("l0_marked"), stats.get("l3_notified"), stats.get("ms"), extra=f)
    for err in stats.get("errors") or []:
        log.warning("分级一轮遗留：%s", err, extra=fields(src="triage"))


def _run_once(feed_ids=None, dry_run=False, max_calls=None):
    """跑一轮。返回统计 dict。"""
    ds = get_datasource()
    nodes, cats = _feed_index()
    pending = pending_entries(feed_ids=feed_ids)

    stats = {"scanned_feeds": len(pending), "pending": sum(len(v) for v in pending.values()),
             "rated": 0, "defaulted": 0, "fail_open": 0, "llm_calls": 0,
             "l0_marked": 0, "l3_notified": 0,
             "budget_stop": False, "errors": []}
    if not pending:
        return stats

    max_calls = max_calls or sk.effective("triage_max_calls")
    cfg = llm.provider_cfg()
    # ★ 分级只认自己那一个池子（POOL_TRIAGE = triage + prompt_build 的钱）。
    #   判定与记账走 cr.chat_recorded / cr.pool_gate，不再手写 spent_today 比较。

    for feed_id, entries in sorted(pending.items(), key=lambda kv: -len(kv[1])):
        if stats["llm_calls"] >= max_calls:
            stats["errors"].append("达到单次调用上限，剩余留到下一轮")
            break

        node = nodes.get(feed_id)
        title = node.title if node else f"feed:{feed_id}"
        category_id = cats.get(feed_id)
        spec = promptspec.build_spec(feed_id, category_id=category_id, feed_title=title)
        default_level = spec["default_level"]

        # ---- 2) 四段全空 → 零成本路径
        if not spec["has_any"]:
            if dry_run:
                stats["defaulted"] += len(entries)
                continue
            for e in entries:
                EntryRating.objects.update_or_create(
                    entry_id=e.id,
                    defaults={"feed_id": feed_id, "category_id": category_id,
                              "level": default_level, "reason": "default",
                              "votes": [], "model": ""},
                )
                stats["rated"] += 1
                stats["defaulted"] += 1
            if default_level == 0:
                stats["l0_marked"] += _apply([e.id for e in entries], 0, "default", ds)
            # default_level == 3：不处置（不加星，见 _apply docstring；也不逐条通知）
            continue

        # ---- 3) 有判据 → 建/取 prompt → 按 feed 分批调 LLM
        prompt_row, spec = promptbuilder.build(feed_id, category_id=category_id,
                                               feed_title=title)
        if prompt_row is None:
            continue
        base_prompt = prompt_row.prompt_text

        for start in range(0, len(entries), sk.effective("triage_batch_size")):
            if stats["llm_calls"] >= max_calls:
                break

            batch = entries[start:start + sk.effective("triage_batch_size")]
            user = base_prompt + "\n\n===== 待判定的条目 =====\n" + _render_items(batch)
            messages = [{"role": "system", "content": promptspec.BASE_SYSTEM},
                        {"role": "user", "content": user}]

            res = cr.chat_recorded(
                "triage", messages, pool=cr.POOL_TRIAGE,
                temperature=0.1, max_tokens=4096, retries=3, timeout=90,
                feed_id=feed_id, category_id=category_id)
            if res.budget_stop:
                stats["budget_stop"] = True
                stats["errors"].append(res.err + "，剩余留到下一轮")
                log.warning("分级池预算已用尽，本轮停止")
                break
            stats["llm_calls"] += 1
            text, err = res.text, res.err

            rows = parse_rows(text) if not err else []
            got = {r["id"]: r for r in rows}

            for e in batch:
                if stats["budget_stop"]:
                    break       # 补票中途撞闸：本条用已有票聚合，剩余留到下一轮
                votes = []
                failed = bool(err)
                if e.id in got:
                    votes.append(got[e.id])
                    # ★ 逐票评估的续票（自适应）：L2/L3 即停、L1 满 2 票、L0 满票
                    for _ in range(min(max(0, sk.effective("triage_repeat") - 1), 2)):
                        if votes_done([v["level"] for v in votes],
                                      sk.effective("triage_repeat"),
                                      sk.effective("triage_adaptive")):
                            break
                        if stats["llm_calls"] >= max_calls:
                            break
                        single = [{"role": "system", "content": promptspec.BASE_SYSTEM},
                                  {"role": "user",
                                   "content": base_prompt + "\n\n===== 待判定的条目 =====\n"
                                   + _render_items([e])}]
                        r2 = cr.chat_recorded(
                            "triage", single, pool=cr.POOL_TRIAGE,
                            temperature=0.1, max_tokens=2048, retries=2, timeout=90,
                            feed_id=feed_id, category_id=category_id)
                        if r2.budget_stop:
                            # 补票也要过闸：钱不够就用已有票聚合（<2 票永不判 L0）
                            stats["budget_stop"] = True
                            break
                        stats["llm_calls"] += 1
                        hit = [r for r in parse_rows(r2.text) if r["id"] == e.id]
                        if hit:
                            votes.append(hit[0])
                        else:
                            failed = True

                if not votes:
                    level, reason, info = default_level, "fail_open", {"reason": err or "无有效票"}
                    stats["fail_open"] += 1
                    votes = [{"level": default_level, "why": f"fail-open（{err or '无有效票'}）",
                              "_fail_open": True}]
                else:
                    level, info = aggregate_level(votes, default_level)
                    reason = "llm"
                    if failed:
                        stats["fail_open"] += 1
                        log.warning("entry %s 有票但调用曾失败（%d/%d 票）",
                                    e.id, len(votes), sk.effective("triage_repeat"))

                if dry_run:
                    stats["rated"] += 1
                    continue

                EntryRating.objects.update_or_create(
                    entry_id=e.id,
                    defaults={
                        "feed_id": feed_id, "category_id": category_id,
                        "level": level, "reason": reason, "votes": votes,
                        "model": cfg["model"], "prompt_version": prompt_row.version,
                        "default_level": default_level,
                        "confidence": (info.get("counts", {}).get(str(level), 0)
                                       / max(len(votes), 1)) if votes else None,
                    },
                )
                stats["rated"] += 1
                if level == 0:
                    stats["l0_marked"] += _apply([e.id], level, reason, ds)
                elif level == 3:
                    # L3 处置 = 只通知，不加星（星是读者的手动确认）
                    stats["l3_notified"] += _notify_l3(e, reason)

            if stats["budget_stop"]:
                break
        if stats["budget_stop"]:
            break

    return stats


# ------------------------------------------------------------------ 单条判定（右键菜单）
def rate_single(entry_id, force=False):
    """立即判定 / 重新判定**单条**。返回 {ok, level, state, detail, basis, error}。

    force=True：先删掉已有评级（重新判定）—— 连 manual 也覆盖，因为这是人的显式动作。
    ★ 与 run_once 共用 _RUN_LOCK：两轮并发会各自调模型，白花钱。
    """
    if not _RUN_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "分级正在跑，等这一轮结束再试"}
    try:
        return _rate_single(entry_id, force=force)
    finally:
        _RUN_LOCK.release()
        try:
            from django.core.cache import cache

            cache.delete("triage:pending_count")
        except Exception:  # noqa: BLE001
            pass


def _rate_single(entry_id, force=False):
    ds = get_datasource()
    try:
        entry = ds.get_entry(entry_id)
    except Exception as exc:  # noqa: BLE001  Miniflux 对不存在的 id 抛 404
        return {"ok": False, "error": f"取条目失败：{exc}"}
    if entry is None:
        return {"ok": False, "error": "条目不存在"}
    feed_id = entry.feed_id
    nodes, cats = _feed_index()
    node = nodes.get(feed_id)
    title = node.title if node else f"feed:{feed_id}"
    category_id = cats.get(feed_id)
    spec = promptspec.build_spec(feed_id, category_id=category_id, feed_title=title)
    default_level = spec["default_level"]

    if force:
        EntryRating.objects.filter(entry_id=entry_id).delete()

    # ---- 零成本路径：该源没有判据，结果就是 default_level
    if not spec["has_any"]:
        EntryRating.objects.update_or_create(
            entry_id=entry_id,
            defaults={"feed_id": feed_id, "category_id": category_id,
                      "level": default_level, "reason": "default",
                      "default_level": default_level, "votes": [], "model": ""})
        _apply([entry_id], default_level, "default", ds)
        return {"ok": True, "level": default_level, "state": "default",
                "detail": f"该源没配分级判据 → 默认 L{default_level}"}

    prompt_row, spec = promptbuilder.build(feed_id, category_id=category_id,
                                           feed_title=title)
    if prompt_row is None:
        return {"ok": False, "error": "建 prompt 失败"}

    cfg = llm.provider_cfg()
    over, why = cr.pool_gate(cr.POOL_TRIAGE)
    if over:
        return {"ok": False, "error": why}

    # ---- 与批量路径同口径：逐票评估（L2/L3 即停 / L1 满 2 票 / L0 满票）
    base = prompt_row.prompt_text
    votes, err = [], ""
    for i in range(min(max(1, sk.effective("triage_repeat")), 3)):
        user = base + "\n\n===== 待判定的条目 =====\n" + _render_items([entry])
        res = cr.chat_recorded(
            "triage",
            [{"role": "system", "content": promptspec.BASE_SYSTEM},
             {"role": "user", "content": user}],
            pool=cr.POOL_TRIAGE,
            temperature=0.1, max_tokens=4096 if i == 0 else 2048,
            retries=2, timeout=90,
            feed_id=feed_id, category_id=category_id)
        if res.budget_stop or res.err:
            err = res.err
            break
        hit = [r for r in parse_rows(res.text) if r["id"] == entry_id]
        if not hit:
            err = "模型没返回这一条"
            break
        votes.append(hit[0])
        if votes_done([v["level"] for v in votes],
                      sk.effective("triage_repeat"),
                      sk.effective("triage_adaptive")):
            break

    if not votes:
        EntryRating.objects.update_or_create(
            entry_id=entry_id,
            defaults={"feed_id": feed_id, "category_id": category_id,
                      "level": default_level, "reason": "fail_open",
                      "default_level": default_level, "model": cfg["model"],
                      "votes": [{"level": default_level, "_fail_open": True,
                                 "why": f"fail-open（{err or '无有效票'}）"}]})
        return {"ok": False, "level": default_level, "state": "fail",
                "error": f"判定失败：{err or '无有效票'}（已按失败兜底记 L{default_level}）"}

    level, info = aggregate_level(votes, default_level)
    EntryRating.objects.update_or_create(
        entry_id=entry_id,
        defaults={"feed_id": feed_id, "category_id": category_id,
                  "level": level, "reason": "llm", "votes": votes,
                  "model": cfg["model"], "prompt_version": prompt_row.version,
                  "default_level": default_level,
                  "confidence": (info.get("counts", {}).get(str(level), 0)
                                 / max(len(votes), 1))})
    _apply([entry_id], level, "llm", ds)
    rating = EntryRating.objects.filter(entry_id=entry_id).first()
    return {"ok": True, "level": level, "state": "llm",
            "detail": f"L{level}（{info.get('reason') or '模型判定'}）",
            "basis": rating_basis(rating, "llm", level)}

# ------------------------------------------------------------------ 链路总览（`/triage/status` JSON）
def overview(calls: int = 20, fails: int = 60) -> dict:
    """分级链路的一屏总览：待判 / 失败兜底 / 覆盖 / 预算 / 最近调用。

    ★ 这是「心里有数」的落点：一个 L2 到底是判出来的、默认来的、还是失败兜底，
      在条目级别靠徽标看，在链路级别靠这里看。
    """
    from django.db.models import Count

    from core import costreport as cr
    from core.models import LlmUsage

    now = timezone.now()
    tree = []
    tree_err = ""
    try:
        tree = get_datasource().list_tree()
    except Exception as exc:  # noqa: BLE001  链路挂了更要能看到页面
        tree_err = f"{type(exc).__name__}: {exc}"
        log.warning("状态页取树失败：%s", exc)

    nodes, cats = treekit.feed_index(tree)
    cat_titles = {cat.id: cat.title for cat in tree}
    # ---- 待判（真扫一次 Miniflux；失败明确写出来，不装成 0）
    pending_rows, pend_total, pend_err = [], None, tree_err
    try:
        pend = pending_entries()
        pend_total = sum(len(v) for v in pend.values())
        for fid, es in sorted(pend.items(), key=lambda kv: -len(kv[1]))[:30]:
            newest = max((e.published_at or e.created_at for e in es
                          if (e.published_at or e.created_at)), default=None)
            pending_rows.append({
                "feed_id": fid,
                "title": (nodes[fid].title if fid in nodes else f"#{fid}"),
                "category": cat_titles.get(cats.get(fid), "未分类"),
                "n": len(es), "newest": newest,
            })
    except Exception as exc:  # noqa: BLE001
        pend_err = f"{type(exc).__name__}: {exc}"
        log.warning("状态页统计待判失败：%s", exc)

    # ---- 历史评级分布（reason 才是真信号）
    by_reason = {r["reason"]: r["c"] for r in
                 EntryRating.objects.values("reason").annotate(c=Count("id"))}
    by_level = {r["level"]: r["c"] for r in
                EntryRating.objects.values("level").annotate(c=Count("id"))}

    fail_qs = EntryRating.objects.filter(reason="fail_open").order_by("-rated_at")
    fail_total = fail_qs.count()
    fail_rows = [{
        "entry_id": r.entry_id, "feed_id": r.feed_id,
        "title": (nodes[r.feed_id].title if r.feed_id in nodes else f"#{r.feed_id}"),
        "level": r.level, "rated_at": r.rated_at,
        "why": (r.votes or [{}])[0].get("why", "") if isinstance(r.votes, list) else "",
    } for r in fail_qs[:fails]]

    # ---- 覆盖：多少源真的有分级判据（有判据才可能花 LLM，其余走零成本默认）
    with_criteria, without = [], []
    for cat in tree:
        for f in cat.feeds:
            spec = promptspec.build_spec(f.id, category_id=cat.id, feed_title=f.title)
            item = {"feed_id": f.id, "title": f.title, "category": cat.title,
                    "default_level": spec["default_level"]}
            (with_criteria if spec["has_any"] else without).append(item)

    # ---- 最近调用（★ 0 token 的"成功"调用要显眼：10-15 实测出现过 900 秒、
    #      0 token、却被记成 ok 的调用，分级链路其实什么都没拿到）
    recent = [{
        "ts": u.ts, "feed_id": u.feed_id,
        "title": (nodes[u.feed_id].title if u.feed_id in nodes else
                  (f"#{u.feed_id}" if u.feed_id else "—")),
        "ok": u.ok, "latency_ms": u.latency_ms, "prompt_tokens": u.prompt_tokens,
        "completion_tokens": u.completion_tokens, "error": u.error,
        "suspicious": (u.prompt_tokens or 0) + (u.completion_tokens or 0) == 0,
    } for u in LlmUsage.objects.filter(kind="triage").order_by("-ts")[:calls]]

    state_counts = {
        "llm": by_reason.get("llm", 0),
        "default": by_reason.get("default", 0),
        "fail": by_reason.get("fail_open", 0),
        "manual": by_reason.get("manual", 0),
        "pending": pend_total,      # None = 取不到（Miniflux 挂了），别显示成 0
        "expired": None,            # 全库无意义：超窗未判只在列表窗口内才看得见
    }
    legend = []
    for state in RATE_STATES:
        meta = rate_meta(state)
        legend.append({"state": state, "label": meta["label"], "css": meta["css"],
                       "hint": meta["hint"], "count": state_counts.get(state)})

    return {
        "now": now,
        "tree_err": pend_err,
        "pending_total": pend_total,
        "pending_rows": pending_rows,
        "fail_total": fail_total,
        "fail_rows": fail_rows,
        "by_reason": by_reason,
        "by_level": by_level,
        "rated_total": sum(by_reason.values()),
        "state_counts": state_counts,
        "legend": legend,
        "with_criteria": with_criteria,
        "without_criteria": without,
        "feed_total": len(with_criteria) + len(without),
        "recent": recent,
        "budget": {"limit": cr.pool_budget_cny(cr.POOL_TRIAGE),
                   "spent": cr.pool_spent_cny(cr.POOL_TRIAGE)},
        "cfg": {
            "enabled": sk.effective("triage_enabled"),
            "interval": settings.TRIAGE_INTERVAL_MIN,
            "lookback": sk.effective("triage_lookback_days"),
            "per_feed": settings.TRIAGE_PER_FEED_LIMIT,
            "batch": sk.effective("triage_batch_size"),
            "repeat": sk.effective("triage_repeat"),
            "adaptive": sk.effective("triage_adaptive"),
            "unrated_level": settings.UNRATED_LEVEL,
            "provider": settings.LLM_PROVIDER,
            "model": llm.provider_cfg()["model"],
            "max_calls": sk.effective("triage_max_calls"),
        },
    }


def unrate_failed(ids=None, feed_id=None, limit=None) -> int:
    """删掉 fail_open 的评级 → 下一轮 TriageWorker 会重新判（返回删除条数）。

    ★ 只删 `reason="fail_open"`：
      · `default` 重判也是同一个默认值（白花钱）；
      · `llm` 已经判过，重判要钱且没必要；
      · `manual` 是人的意志，不能被工具覆盖。
    fail-open 落 L0 的那批本来就没标已读（`_apply` 的硬规则），所以这里不需要回滚 Miniflux。
    """
    qs = EntryRating.objects.filter(reason="fail_open")
    if feed_id:
        qs = qs.filter(feed_id=int(feed_id))
    if ids:
        qs = EntryRating.objects.filter(reason="fail_open",
                                        entry_id__in=[int(i) for i in ids])
    if limit:
        keep = list(qs.order_by("-rated_at").values_list("id", flat=True)[:int(limit)])
        qs = EntryRating.objects.filter(id__in=keep)
    n, _ = qs.delete()
    if n:
        from django.core.cache import cache

        try:
            cache.delete("triage:pending_count")
        except Exception:  # noqa: BLE001
            pass
        log.info("清掉 %s 条 fail_open 评级，等下一轮重判", n)
    return n
