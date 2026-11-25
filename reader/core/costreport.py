"""开销聚合（M5）：预算闸/调用出口 + 把 LlmUsage 总账切成报表需要的各个面。

★ `pool_gate` / `chat_recorded` 是**预算策略层**：triage / promptbuilder /
  summary / translate 的每次 LLM 调用都从这里出去（判定与记账单一出口）。
  其余是报表计算（页面展示用），两层职责刻意分开 —— 动报表不许动闸门。

★ 全部在 Python 侧聚合。理由：
  1. 数据量小（每天几十行），拉全窗口再算毫无压力；
  2. SQLite + USE_TZ 下 `TruncDate(ts, tzinfo=...)` 的时区截断很容易把
     「今天的调用」算成昨天 —— 用 timezone.localtime() 在 Python 里取日期最稳。

口径统一：`cost_cny` / `cost_usd` / `retries` / `ok` 都取自 LlmUsage 落库时的快照，
不在这里重算价格（否则高低峰切换后历史数字会变）。

时间范围（2026-10-20）
---------------------
页面从「近 N 天」升级成 **显式区间**（`Range(start, end)`）：今天 / 昨天 / 近 7 天 /
近 30 天 / 本月 / 上月 / 自定义。单日区间自动切到**按小时**视图。
区间是**闭区间**（含 start 与 end 两个自然日）。
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import NamedTuple, Optional

from django.conf import settings
from django.utils import timezone

from core import llm
from core.models import AppSetting, LlmUsage

KIND_LABELS = dict(LlmUsage.KINDS)
# 堆叠柱的固定顺序（颜色见 app.css 的 --k1..--k6）
# ★ 新 kind 必须同时出现在这里和 app.css 的 `.k-<key>`：只加 kind 不加颜色，
#   图例有名字、柱子里看不见（周报 2026-10-29 就这样隐身过）。
KIND_ORDER = ["triage", "prompt_build", "summary_entry", "summary_feed",
              "summary_category", "summary_global", "summary_weekly",
              "translate_title",
              "wiki_chat", "wiki_extract", "wiki_compile", "wiki_lint",
              "wiki_summary"]
PUBLIC_LABEL = "公共"

# ------------------------------------------------------------------ 时间范围
DEFAULT_PRESET = "7d"
MAX_SPAN_DAYS = 366

# (key, label)。`custom` 不在这里：它由「两个日期输入 + 应用」表达。
PRESETS = [
    ("today", "今天"),
    ("yesterday", "昨天"),
    ("7d", "近 7 天"),
    ("30d", "近 30 天"),
    ("month", "本月"),
    ("last_month", "上月"),
]
_PRESET_KEYS = {k for k, _ in PRESETS}


@dataclass(frozen=True)
class Range:
    """闭区间 [start, end]，两者都是本地时区的自然日。"""

    preset: str
    start: date
    end: date
    is_default: bool = False

    @property
    def single_day(self) -> bool:
        return self.start == self.end

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def label(self) -> str:
        if self.single_day:
            today = timezone.localtime().date()
            suffix = ("（今天）" if self.start == today
                      else "（昨天）" if self.start == today - timedelta(days=1)
                      else "")
            return f"{self.start:%Y-%m-%d}{suffix}"
        return f"{self.start:%m/%d} – {self.end:%m/%d}（{self.days} 天）"

    def query(self, **extra) -> str:
        """拼成 URL 查询串（含 preset / start / end），供模板与跳转复用。"""
        from urllib.parse import urlencode

        q = {"preset": self.preset}
        if self.preset == "custom":
            q.update({"start": self.start.isoformat(), "end": self.end.isoformat()})
        q.update({k: v for k, v in extra.items() if v not in (None, "")})
        return urlencode(q)


def _parse_date(raw) -> Optional[date]:
    try:
        return datetime.strptime((raw or "").strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _preset_range(preset: str, today: date, default: str) -> Range:
    if preset == "today":
        start = end = today
    elif preset == "yesterday":
        start = end = today - timedelta(days=1)
    elif preset == "7d":
        start, end = today - timedelta(days=6), today
    elif preset == "30d":
        start, end = today - timedelta(days=29), today
    elif preset == "month":
        start, end = today.replace(day=1), today
    elif preset == "last_month":
        end = today.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
    else:
        # 兜底：未知 preset 退回默认（避免无限递归，直接用 7d 的算法）
        preset = default if default in _PRESET_KEYS else DEFAULT_PRESET
        return _preset_range(preset, today, default)
    return Range(preset, start, end, preset == default)


def resolve_range(params, default: str = DEFAULT_PRESET) -> Range:
    """从 query dict 解析区间。兼容旧的 `?days=N`。

    优先级：`preset=custom`（或直接给了 start/end）> `preset=...` > `days=N` > 默认。
    """
    today = timezone.localtime().date()
    preset = (params.get("preset") or "").strip()

    has_dates = bool((params.get("start") or "").strip() or
                     (params.get("end") or "").strip())
    if preset == "custom" or (not preset and has_dates):
        start = _parse_date(params.get("start"))
        end = _parse_date(params.get("end"))
        if not start or not end:
            return _preset_range(default, today, default)
        if end < start:
            start, end = end, start
        if (end - start).days > MAX_SPAN_DAYS:
            start = end - timedelta(days=MAX_SPAN_DAYS)
        return Range("custom", start, end, False)

    if preset in _PRESET_KEYS:
        return _preset_range(preset, today, default)

    # ★ 兼容旧链接（`/costs?days=30`、下钻页的「90 天」按钮）
    raw_days = params.get("days")
    if raw_days:
        try:
            n = max(1, min(int(raw_days), MAX_SPAN_DAYS + 1))
        except (TypeError, ValueError):
            n = None
        if n:
            alias = {7: "7d", 30: "30d"}.get(n)
            if alias:
                return _preset_range(alias, today, default)
            return Range("custom", today - timedelta(days=n - 1), today, False)

    return _preset_range(default, today, default)


def last_days(days: int) -> Range:
    """近 N 天（含今天）—— 卡片等固定窗口用。"""
    today = timezone.localtime().date()
    n = max(1, int(days))
    return Range("7d", today - timedelta(days=n - 1), today)


# ------------------------------------------------------------------ 预算分池
# ★ 分级（triage + prompt_build）、总结（summary_*）、标题翻译（translate_title）
#   各一个池子。共用一个池子时的实测后果：补跑 8 天报告花 ¥1.28 → 当日累计超限 →
#   分级当天直接停摆，而总结自己还有额度。节奏不同、优先级也不同，就该分开算。
BUDGET_KEY = "daily_budget_cny"                       # 旧单池键（保留兼容读取）
BUDGET_KEY_TRIAGE = "triage_budget_cny"
BUDGET_KEY_SUMMARY = "summary_budget_cny"
BUDGET_KEY_TRANSLATE = "translate_budget_cny"
BUDGET_KEY_WEEKLY = "weekly_budget_cny"
BUDGET_KEY_WIKI = "wiki_budget_cny"

POOL_TRIAGE = "triage"
POOL_SUMMARY = "summary"
POOL_TRANSLATE = "translate"
# ★ 周报独立成池（2026-10-29）：周日 12:00 时当天日报已花掉一部分总结池，
#   周报（≈¥0.13）挤进去会撞 budget_stop 闸 —— 表现是"周报没生成、也没报错"。
POOL_WEEKLY = "weekly"
# ★ 知识库独立成池（2026-10-31，第 5 池）：wiki 的开销是**事件驱动**的
#   （人聊天/编译时才花），与日报周报的定时批处理节奏不同。混池的后果是
#   两个方向都错：聊两句可能吃掉当天日报的钱，补跑日报也可能把人正在做的
#   编译拦住。默认值 `settings.WIKI_BUDGET_CNY` 是待实测校准的初值。
POOL_WIKI = "wiki"

POOL_KINDS = {
    POOL_TRIAGE: ("triage", "prompt_build"),
    POOL_SUMMARY: ("summary_entry", "summary_feed", "summary_category",
                   "summary_global"),
    # ★ 标题翻译独立成一池：回填可能一次花掉几十条的钱，
    #   绝不能挤掉当天的分级/总结（与两池拆分同一条教训）。
    POOL_TRANSLATE: ("translate_title",),
    POOL_WEEKLY: ("summary_weekly",),
    POOL_WIKI: ("wiki_chat", "wiki_extract", "wiki_compile", "wiki_lint",
                "wiki_summary"),
}
POOL_LABELS = {POOL_TRIAGE: "分级", POOL_SUMMARY: "日报",
               POOL_TRANSLATE: "标题翻译", POOL_WEEKLY: "周报",
               POOL_WIKI: "知识库"}

#: 池 → 超限后果的人话说明（/costs 预算卡直接渲染，模板不再做 if/elif）。
# ★ 2026-10-31 加知识库池时踩到：模板原先是 `{% if pool == ... %}…{% else %}`
#   日报文案{% endif %}`，新池落进 else ⇒ 卡片上写着「超限后本次日报降级为
#   规则版报告」。而知识库超限根本不影响日报 —— 它只是**说错话**，不报错。
#   兜底文案几乎总是骗人的：新增池漏了分支，它安静地把别人的后果安在你头上。
#   所以这段文字也必须收进单一来源，漏了就是 KeyError（当场响）。
POOL_NOTES = {
    POOL_TRIAGE: "超限后本轮停止判级，条目留到下一轮",
    POOL_TRANSLATE: "超限后本轮停止翻译标题，剩余条目下一轮/次日继续",
    POOL_SUMMARY: "超限后本次日报降级为规则版报告",
    POOL_WEEKLY: "超限后本次周报生成失败，可次日手动重跑",
    POOL_WIKI: "超限后对话与编译会被拒绝并明确提示，次日恢复",
}

# ★ 「池 → AppSetting 键」和「池 → settings 默认值」也必须是单一来源：
#   2026-10-29 实测，/costs 的保存视图曾自己手抄一份 pool→key 映射表，
#   加周报池时漏了这一处 → 页面改「周报池日预算」实际写进了
#   `summary_budget_cny`：返回 {"ok": true}、`weekly_budget_cny` 仍无记录、
#   周报池永远停在默认值；再改日报池又把它覆盖回去，用户看到的是
#   「两个都保存不了」。凡是"池"的属性表都放这里，调用点只许查不许抄。
POOL_BUDGET_KEYS = {
    POOL_TRIAGE: BUDGET_KEY_TRIAGE,
    POOL_TRANSLATE: BUDGET_KEY_TRANSLATE,
    POOL_SUMMARY: BUDGET_KEY_SUMMARY,
    POOL_WEEKLY: BUDGET_KEY_WEEKLY,
    POOL_WIKI: BUDGET_KEY_WIKI,
}

#: 池 → settings 默认值（与 POOL_BUDGET_KEYS 同序同源，只在这里写一次）
POOL_DEFAULTS = {
    POOL_TRIAGE: settings.TRIAGE_BUDGET_CNY,
    POOL_TRANSLATE: settings.TRANSLATE_BUDGET_CNY,
    POOL_SUMMARY: settings.SUMMARY_BUDGET_CNY,
    POOL_WEEKLY: settings.WEEKLY_BUDGET_CNY,
    POOL_WIKI: settings.WIKI_BUDGET_CNY,
}

# 参与"总览合计 / 预算状态"的池子（新增池子时改这里，别在调用点抄列表）。
# ★ 顺序 = /costs 预算卡的显示顺序：分级 → 标题翻译 → 日报 → 周报 → 知识库。
#   日报放翻译右边（用户 2026-10-29 指定）：翻译是附属动作，不该插在两个
#   主内容池中间。知识库同为「主内容」池，追加在末尾而不是插进日报周报之间。
ALL_POOLS = (POOL_TRIAGE, POOL_TRANSLATE, POOL_SUMMARY, POOL_WEEKLY, POOL_WIKI)


def _setting_float(key: str, fallback: float) -> float:
    row = AppSetting.objects.filter(key=key).first()
    if row and (row.value or "").strip():
        try:
            return float(row.value.strip())
        except ValueError:
            pass
    return float(fallback)


def budget_cny() -> float:
    """各池之和（页面总览口径；不参与任何停机判定）。"""
    return sum(pool_budget_cny(p) for p in ALL_POOLS)


def pool_budget_cny(pool: str) -> float:
    """单个池子的上限：AppSetting 优先，回退 settings 的同名项。

    ★ 键映射查 `POOL_BUDGET_KEYS`，默认值查 `POOL_DEFAULTS` —— 新增池子
      只改那两张表；未知池回退旧单池键（`daily_budget_cny`）只为兼容老数据，
      正常调用点不该走到这里。
    """
    key = POOL_BUDGET_KEYS.get(pool)
    if key is not None:
        return _setting_float(key, POOL_DEFAULTS[pool])
    return _setting_float(BUDGET_KEY, settings.DAILY_BUDGET_CNY)


# ------------------------------------------------------------------ 预算闸与调用出口
# ★ 「每次 LLM 调用必记账 + 预算双闸」是本项目的核心原则，此前靠 triage /
#   promptbuilder / summary / translate 各自手抄 chat+record 配对与闸判定，
#   参数已经漂移过（漏 latency_ms、把业务校验重试记成传输重试）。原则本身
#   不能依赖每个调用点自觉 —— 判定与记账都收在这里，业务方只许调 chat_recorded。
class ChatResult(NamedTuple):
    """chat_recorded 的返回。

    budget_stop=True 表示被预算闸拦下：**没调模型、没记账**，调用方应停止本轮；
    usage_row 是本次落账的 LlmUsage 行（闸下时为 None）。

    ★ `tool_calls`（2026-11-05 第四步）：模型想调工具时带出，末尾带默认值，
      现有 5 参数构造 / `.text/.usage/.err/.budget_stop` 用法全部不变。
      `llm.chat` 已把它塞进 norm（usage），这里再单独立一个字段是为了
      `citeguard._call` 能显式读到它 —— 别丢在 usage 里靠调用方猜。
    """
    text: str
    usage: dict
    err: str
    budget_stop: bool
    usage_row: object = None
    tool_calls: tuple = ()


def _require_pool(pool: str) -> tuple:
    """取池的 kind 元组；未知池直接抛错。

    ★ 2026-10-31 修正：原实现是 `POOL_KINDS.get(pool, ())` —— 拼错池名（或传了
      已删的池）时 kinds 为空，`spent_today_cny(())` 恒为 0，于是**预算闸永远不超**：
      不报错、不拦、照常花钱，报表上还显示「已用 0%」。这是最坏的一种失败
      （静默 + 恰恰在最该刹车的地方失效）。拼错池名是开发期 bug，应当在第一次
      调用就炸出来，而不是等到月底看账单。
    """
    kinds = POOL_KINDS.get(pool)
    if kinds is None:
        raise ValueError(
            f"未知预算池：{pool!r}（可用：{', '.join(sorted(POOL_KINDS))}）")
    return kinds


def pool_gate(pool: str) -> tuple:
    """预算闸判定（全项目唯一实现）。返回 (超限?, 人话原因)。

    ★ 停机判定不要再手写 `llm.spent_today_cny(...) >= cr.pool_budget_cny(...)`——
      那样每个调用点各留一份，新池子/新判定口径一改就会漏。
    ★ 未知池抛 ValueError（见 `_require_pool`），不回落到「不超」。
    ★ 返回的 `why` 是**给人看的**：wiki 池是事件驱动（人点了按钮才花钱），
      超限时调用方必须把它回显到界面，不能像定时任务那样静默跳过 ——
      「点了没反应」比「明确告诉今天用尽了」糟得多。
    """
    kinds = _require_pool(pool)
    limit = pool_budget_cny(pool)
    spent = llm.spent_today_cny(kinds)
    if spent >= limit:
        return True, f"{POOL_LABELS.get(pool, pool)}池今日预算（¥{limit:g}）已用尽"
    return False, ""


def chat_recorded(kind, messages, *, pool=None, temperature=0.1, max_tokens=4096,
                  retries=3, timeout=90, provider=None, model=None,
                  feed_id=None, category_id=None, entry_id=None,
                  summary_run_id=None, tools=None) -> ChatResult:
    """LLM 业务调用的**唯一出口**：预算闸 → llm.chat → llm.record，一次完成。

    ★ pool 非空时先判闸（超限即返回，不花钱不落账）；pool=None 表示该调用
      不判闸 —— 只允许调用方已在更粗粒度上判过的场景（如 translate 逐批判）。
    ★ latency 与传输重试次数（tries-1）由这里统一测量落账，调用方不要再自己传；
      `retries` 形参是传输层重试**上限**，业务校验重试（如 prompt 结构校验）
      属于调用方自己的多次 chat_recorded 调用，各记各的账。
    """
    if pool is not None:
        over, why = pool_gate(pool)
        if over:
            return ChatResult("", {}, why, True, None)
    import time   # 函数内导入：模块层的 time 是 datetime.time（_at 用它取零点）

    cfg = llm.provider_cfg(provider)
    t0 = time.monotonic()
    text, usage, err, tries = llm.chat(
        messages, provider=provider, model=model, temperature=temperature,
        max_tokens=max_tokens, retries=retries, timeout=timeout, tools=tools)
    row = llm.record(kind, provider=cfg["provider"], model=(model or cfg["model"]),
                     usage=usage, retries=max(0, tries - 1),
                     latency_ms=int((time.monotonic() - t0) * 1000),
                     ok=not err, error=err, feed_id=feed_id,
                     category_id=category_id, entry_id=entry_id,
                     summary_run_id=summary_run_id)
    # ★ tool_calls 在 llm.chat 的 norm（usage）里；单独立字段带出，别丢。
    tool_calls = tuple((usage or {}).get("tool_calls") or ())
    return ChatResult(text, usage or {}, err, False, row, tool_calls)


def chat_recorded_stream(kind, messages, *, pool=None, temperature=0.1,
                         max_tokens=4096, timeout=180, provider=None, model=None,
                         on_delta=None, tools=None) -> ChatResult:
    """流式版 `chat_recorded`：**预算闸 → llm.chat_stream → llm.record**，一次完成。

    ★ 加法式（2026-11-19）：不碰 `chat_recorded()`（ dozens 测试桩钉死）。
    ★ 判闸与记账口径与 `chat_recorded()` 逐字相同 —— 流式不是「绕过预算的后门」：
      闸下直接返回 budget_stop（没调模型、没记账），调用方据此停；流式成功/失败
      都在流末按真实 usage 记一行账。usage 靠 llm.chat_stream 的 include_usage 拿。
    """
    if pool is not None:
        over, why = pool_gate(pool)
        if over:
            return ChatResult("", {}, why, True, None)
    import time
    cfg = llm.provider_cfg(provider)
    t0 = time.monotonic()
    text, usage, err, tries = llm.chat_stream(
        messages, provider=provider, model=model, temperature=temperature,
        max_tokens=max_tokens, timeout=timeout, on_delta=on_delta, tools=tools)
    row = llm.record(kind, provider=cfg["provider"], model=(model or cfg["model"]),
                     usage=usage, retries=max(0, tries - 1),
                     latency_ms=int((time.monotonic() - t0) * 1000),
                     ok=not err, error=err)
    tool_calls = tuple((usage or {}).get("tool_calls") or ())
    return ChatResult(text, usage or {}, err, False, row, tool_calls)


# ------------------------------------------------------------------ 取数
def _blank() -> dict:
    return {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0,
            "tokens": 0, "cost_cny": 0.0, "cost_usd": 0.0,
            "calls": 0, "retries": 0, "failures": 0}


def _acc(bucket: dict, row) -> dict:
    bucket["prompt_tokens"] += row.prompt_tokens or 0
    bucket["completion_tokens"] += row.completion_tokens or 0
    bucket["cached_tokens"] += row.cached_tokens or 0
    bucket["tokens"] = bucket["prompt_tokens"] + bucket["completion_tokens"]
    bucket["cost_cny"] += float(row.cost_cny or 0)
    bucket["cost_usd"] += float(row.cost_usd or 0)
    bucket["calls"] += 1
    bucket["retries"] += row.retries or 0
    if not row.ok:
        bucket["failures"] += 1
    return bucket


def _fmt(bucket: dict) -> dict:
    bucket["cost_cny"] = round(bucket["cost_cny"], 4)
    bucket["cost_usd"] = round(bucket["cost_usd"], 4)
    return bucket


def day_start(offset_days: int = 0):
    """本地时区「今天 00:00」再往回 offset 天（返回 aware datetime）。"""
    now = timezone.localtime()
    return (now.replace(hour=0, minute=0, second=0, microsecond=0)
            - timedelta(days=offset_days))


def _at(day: date):
    return timezone.make_aware(datetime.combine(day, time.min))


def _rows_range(rng: Range, feed_id: Optional[int] = None):
    """区间内的行（含两端自然日），按时间升序。`feed_id` 用于下钻页只取单源。"""
    qs = LlmUsage.objects.filter(
        ts__gte=_at(rng.start),
        ts__lt=_at(rng.end + timedelta(days=1)),
    )
    if feed_id is not None:
        qs = qs.filter(feed_id=int(feed_id))
    return list(qs.order_by("ts"))


def _rows(days: int):
    """兼容旧调用：近 days 天（含今天）。"""
    return _rows_range(last_days(days))


# ------------------------------------------------------------------ 卡片
def summary(days: int = 1) -> dict:
    """近 days 天（含今天）的汇总。days=1 即今日。"""
    return summary_range(last_days(days))


def summary_range(rng: Range) -> dict:
    b = _blank()
    for r in _rows_range(rng):
        _acc(b, r)
    b["days"] = rng.days
    return _fmt(b)


# ------------------------------------------------------------------ 柱图（按日 / 按小时）
def _new_cell(label: str, **extra) -> dict:
    return {"label": label, "total_cny": 0.0, "total_tokens": 0, "calls": 0,
            "kinds": {k: _blank() for k in KIND_ORDER}, **extra}


def _hit(cell: dict, r) -> None:
    kind = r.kind if r.kind in cell["kinds"] else None
    if kind is not None:
        _acc(cell["kinds"][kind], r)
    # ★ 新 kind 忘了加 KIND_ORDER 时，宁可只计入总额（图例里看不到），
    #   也不能默认落到 triage —— 那会把翻译开销伪装成分级，报表直接骗人。
    cell["total_cny"] += float(r.cost_cny or 0)
    cell["total_tokens"] += (r.prompt_tokens or 0) + (r.completion_tokens or 0)
    cell["calls"] += 1


def _finalize(cells: list) -> float:
    """补齐 segments / pct / 横轴标签显隐 / hover 文案，返回峰值（模板不做算术）。"""
    peak = max([c["total_cny"] for c in cells] or [0.0])
    # ★ 横轴标签抽稀：格子多的时候只标 4 个左右。24 小时 → 00/06/12/18；
    #   30 天 → 每 8 天一个。格子少（≤12）时全标。
    # ★★ **最右侧一定标**：否则右边界没有刻度，读图时说不清最后一格是哪天。
    n = len(cells)
    step = 1 if n <= 12 else max(1, round(n / 4))
    for i, c in enumerate(cells):
        c["show_label"] = (i % step == 0) or (i == n - 1)
        c["total_cny"] = round(c["total_cny"], 4)
        for k in KIND_ORDER:
            _fmt(c["kinds"][k])
        c["segments"] = [dict(c["kinds"][k], key=k, label=KIND_LABELS.get(k, k))
                         for k in KIND_ORDER]
        for seg in c["segments"]:
            seg["pct"] = round(seg["cost_cny"] / peak * 100, 2) if peak > 0 else 0.0
        c["total_pct"] = round(c["total_cny"] / peak * 100, 2) if peak > 0 else 0.0
        # hover 第一行：日期/小时 · 汇总 · 调用数。0 元也照常显示（任意位置可 hover）
        tip_label = c.get("tip_label") or c["label"]
        money = "0" if not c["total_cny"] else f"{c['total_cny']:.4f}"
        c["tip_title"] = f"{tip_label} · ¥{money} · {c['calls']} 次"
    # 最右侧的标签如果和倒数第二格撞在一起（相邻两格都标），去掉倒数第二个，避免文字重叠。
    # ★ 只在抽稀生效（step>1）时做：≤12 格是全标的，不能在这里误删。
    if step > 1 and n >= 2 and cells[-1]["show_label"] and cells[-2]["show_label"]:
        cells[-2]["show_label"] = False
    return round(peak, 4)


def _kinds_meta() -> list:
    return [{"key": k, "label": KIND_LABELS.get(k, k)} for k in KIND_ORDER]


def by_day_range(rng: Range, feed_id: Optional[int] = None) -> dict:
    """按日堆叠柱。日期是**连续**的（没有调用的日子也占一格），否则柱图会错位。

    `feed_id`：下钻页传它，只统计该源（否则画的是全部源的总览）。
    """
    cells = []
    d = rng.start
    while d <= rng.end:
        cells.append(_new_cell(f"{d.month}/{d.day}", date=d))
        d += timedelta(days=1)
    grid = {c["date"]: c for c in cells}
    for r in _rows_range(rng, feed_id=feed_id):
        cell = grid.get(timezone.localtime(r.ts).date())
        if cell is not None:
            _hit(cell, r)
    peak = _finalize(cells)
    return {"rows": cells, "peak_cny": peak, "days": rng.days,
            "mode": "day", "kinds": _kinds_meta()}


def by_hour(day: date, feed_id: Optional[int] = None) -> dict:
    """单日按小时堆叠柱（24 格，含空小时）。

    ★ 为什么单日不画一张「一根柱子」：那根柱子只能看出总额，看不出钱花在哪个时段；
      而 24 格能直接对上分级每 5 分钟一轮、日报 07:30 编排这些节奏。
      也不是饼图：饼图只表达「构成」，而构成在每根柱子的分段里已经有了。
    ★ `feed_id`：下钻页必须传，否则这里会把**全部源**的调用都算进来
      （2026-10-20 实测踩到：feed 详情页点「今天」看到的是总览数据）。
    """
    cells = [_new_cell(f"{h:02d}", hour=h, tip_label=f"{h:02d}:00") for h in range(24)]
    for r in _rows_range(Range("today", day, day), feed_id=feed_id):
        _hit(cells[timezone.localtime(r.ts).hour], r)
    peak = _finalize(cells)
    return {"rows": cells, "peak_cny": peak, "days": 1,
            "mode": "hour", "date": day, "kinds": _kinds_meta()}


def by_day(days: int = 7) -> dict:
    """兼容旧调用。"""
    return by_day_range(last_days(days))


# ------------------------------------------------------------------ 图 2：Top feed
def top_feeds_range(rng: Range, limit: int = 10, titles: dict = None) -> list:
    titles = titles or {}
    acc = {}
    for r in _rows_range(rng):
        if r.feed_id is None:
            continue
        b = acc.setdefault(r.feed_id, _blank())
        _acc(b, r)
    out = []
    for fid, b in acc.items():
        _fmt(b)
        b["feed_id"] = fid
        # ★ 树里没有这个 feed，但账本里还有它的历史调用 —— 明确标「已删除」，
        #   否则只显示 feed:55 会让人以为是个没名字的源。
        if fid in titles:
            b["title"] = titles[fid]
        elif titles:
            b["title"] = f"feed:{fid}（已删除）"
        else:
            b["title"] = f"feed:{fid}"
        out.append(b)
    out.sort(key=lambda x: (-x["cost_cny"], -x["tokens"]))
    out = out[:limit]
    peak = max([x["cost_cny"] for x in out] or [0.0])
    for x in out:
        x["pct"] = round(x["cost_cny"] / peak * 100, 2) if peak > 0 else 0.0
    return out


def top_feeds(days: int = 7, limit: int = 10, titles: dict = None) -> list:
    return top_feeds_range(last_days(days), limit=limit, titles=titles)


# ------------------------------------------------------------------ 图 3：分类分布
def by_category_range(rng: Range, cat_titles: dict = None,
                      feed_to_cat: dict = None) -> list:
    """分类 = feed 的归属分类；feed_id 为空的调用归「公共」。

    ★ LlmUsage.category_id 只在落库时写了才用；历史行可能只有 feed_id，
      所以这里用 feed_to_cat 兜底映射，两路合并。
    """
    cat_titles = cat_titles or {}
    feed_to_cat = feed_to_cat or {}
    acc = {}
    for r in _rows_range(rng):
        cid = r.category_id
        if cid is None and r.feed_id is not None:
            cid = feed_to_cat.get(r.feed_id)
        key = cid if cid is not None else "public"
        b = acc.setdefault(key, _blank())
        _acc(b, r)
    out = []
    for key, b in acc.items():
        _fmt(b)
        b["key"] = key
        b["label"] = PUBLIC_LABEL if key == "public" else (cat_titles.get(key) or f"分类{key}")
        out.append(b)
    out.sort(key=lambda x: -x["cost_cny"])
    total = sum(x["cost_cny"] for x in out) or 1.0
    for x in out:
        x["pct"] = round(x["cost_cny"] / total * 100, 1)
    return out


def by_category(days: int = 7, cat_titles: dict = None,
                feed_to_cat: dict = None) -> list:
    return by_category_range(last_days(days), cat_titles=cat_titles,
                             feed_to_cat=feed_to_cat)


# ------------------------------------------------------------------ 明细表
def feed_table_range(rng: Range, sort: str = "cost", direction: str = "desc",
                     titles: dict = None) -> list:
    """feed × 周期明细行（含按 feed 汇总的所有 kind）。"""
    rows = top_feeds_range(rng, limit=10 ** 6, titles=titles)
    key = {"cost": "cost_cny", "tokens": "tokens", "calls": "calls",
           "retries": "retries", "failures": "failures"}.get(sort, "cost_cny")
    rows.sort(key=lambda x: x.get(key, 0), reverse=(direction != "asc"))
    return rows


def feed_table(days: int = 7, sort: str = "cost", direction: str = "desc",
               titles: dict = None) -> list:
    return feed_table_range(last_days(days), sort=sort, direction=direction,
                            titles=titles)


# ------------------------------------------------------------------ 下钻：单 feed
def feed_detail_range(feed_id: int, rng: Range) -> dict:
    """单 feed 的按日序列 + 逐次调用明细。"""
    series = []
    d = rng.start
    while d <= rng.end:
        series.append({"date": d, "label": f"{d.month}/{d.day}", "cost_cny": 0.0,
                       "tokens": 0, "calls": 0})
        d += timedelta(days=1)
    grid = {c["date"]: c for c in series}

    detail, total = [], _blank()
    for r in _rows_range(rng, feed_id=feed_id):
        _acc(total, r)
        cell = grid.get(timezone.localtime(r.ts).date())
        if cell is not None:
            cell["cost_cny"] += float(r.cost_cny or 0)
            cell["tokens"] += (r.prompt_tokens or 0) + (r.completion_tokens or 0)
            cell["calls"] += 1
        detail.append({
            "ts": timezone.localtime(r.ts), "kind": KIND_LABELS.get(r.kind, r.kind),
            "model": r.model, "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens, "cached_tokens": r.cached_tokens,
            "cost_cny": float(r.cost_cny or 0), "retries": r.retries,
            "latency_ms": r.latency_ms, "ok": r.ok, "error": r.error,
            "entry_id": r.entry_id,
        })
    detail.sort(key=lambda x: x["ts"], reverse=True)
    _fmt(total)

    peak = max([c["cost_cny"] for c in series] or [0.0])
    w, h, pad = 720, 140, 10
    n = len(series)
    for i, c in enumerate(series):
        c["cost_cny"] = round(c["cost_cny"], 4)
        c["pct"] = round(c["cost_cny"] / peak * 100, 2) if peak > 0 else 0.0
        # 折线坐标（模板不做算术，这里一次性算好）
        c["x"] = round(pad + (w - 2 * pad) * (i / max(n - 1, 1)), 1)
        c["y"] = round(h - pad - (h - 2 * pad) * (c["cost_cny"] / peak if peak else 0), 1)
    return {"series": series, "detail": detail, "total": total,
            "peak_cny": round(peak, 4), "days": rng.days, "feed_id": int(feed_id),
            "polyline": " ".join(f"{c['x']},{c['y']}" for c in series),
            "svg_w": w, "svg_h": h}


def feed_detail(feed_id: int, days: int = 30) -> dict:
    return feed_detail_range(feed_id, last_days(days))


# ------------------------------------------------------------------ 告警
def pool_spent_cny(pool: str, days: int = 1) -> float:
    """某个池子近 days 天的花费（与预算闸同一条 SQL 聚合，口径只此一份）。

    ★ 未知池抛错（同 `pool_gate`）：这里返回 0 会让报表显示「已用 0%」，
      而闸门那边可能正在放行或根本没拦 —— 两边都不能假装池子存在。
    """
    return round(llm.spent_today_cny(_require_pool(pool), days=days), 4)


def pool_status(pool: str, days: int = 1) -> dict:
    limit = pool_budget_cny(pool)
    spent = pool_spent_cny(pool, days)
    return {"pool": pool, "label": POOL_LABELS.get(pool, pool),
            "limit_cny": limit, "spent_cny": spent,
            "ratio": round(spent / limit * 100, 1) if limit > 0 else 0.0,
            "over": limit > 0 and spent >= limit,
            "note": POOL_NOTES[pool]}


def budget_status(days: int = 1) -> dict:
    """各池状态 + 合计（合计只用于页面总览，不参与任何停机判定）。"""
    pools = [pool_status(p, days) for p in ALL_POOLS]
    limit = round(sum(p["limit_cny"] for p in pools), 4)
    spent = round(sum(p["spent_cny"] for p in pools), 4)
    return {"pools": pools, "limit_cny": limit, "spent_cny": spent,
            "ratio": round(spent / limit * 100, 1) if limit > 0 else 0.0,
            "over": any(p["over"] for p in pools)}
