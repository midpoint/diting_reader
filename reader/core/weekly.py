"""周报（周期总结 M9）：**过去 7 天日报的合集** → 一次 W0 调用 → 落盘 → 推送。

为什么是"从日报生成"而不是重采一遍
----------------------------------
  · 省钱：一次 W0（≈¥0.13/周）而不是把 7 天的 S3/S2/S1/S0 重跑一遍（¥1.4/天量级）；
  · 连贯：周报直接继承日报已经做过的压缩与点名，不会再冒出一套口径；
  · 解决跨期：日报天然只看 24 小时，"这一周的线索怎么演进"只有周报能说。

★ 素材是**日报文件**（`reportstore`），不是 Miniflux 重建的条目 —— 所以周报
  与那 7 份日报永远同源，不会"周报说有、日报说没有"。

★ 本模块不判"今天该不该跑"（那是 `guard_tick` 与 scheduler 的事），只回答
  "给我一个 week_end，我能不能出一份周报"。

★ 生成失败的处置（2026-10-29 定的）：
  · 缺的是**历史**某天的日报 ⇒ 立即 fail + 通知（历史日报不会自己出现，交给手动）；
  · 缺的是**当天**日报 ⇒ 允许在重试窗口内再来（只是看文件在不在，零开销）——
    07:30 失败时 guard 一直在补跑，12:00 立刻判死很可能误伤"马上就出来"。
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import date as _date, datetime, timedelta
from functools import lru_cache

from django.conf import settings
from django.utils import timezone

from core import (
    clicktrack,
    costreport as cr,
    marks as mk,
    notify,
    promptspec,
    reportstore,
    reporttext as rt,
    settingkit as sk,
    sourceblock,
)
from core import citeguard
from core.logkit import fields
from core.models import StarEvent, SummaryRun

log = logging.getLogger(__name__)

#: 周报 = 最近 N 份日报（含 week_end 当天那份）
WINDOW_DAYS = 7

#: 周报在 `SummaryRun.kind` 里的值（与日报共用一个 id 池，见 models.SummaryRun）
KIND = SummaryRun.KIND_WEEKLY

_RUN_LOCK = threading.Lock()
#: 本进程内活着的周报线程（week_end -> Thread），见 summary._ACTIVE_RUNS 同款教训
_ACTIVE = {}


def rows():
    """周报运行行（新→旧）。★ 必须走 `all_objects` —— 默认 manager 只放行日报。"""
    return SummaryRun.all_objects.filter(kind=KIND).order_by("-date")


def row_of(week_end):
    """取某一期的运行行（可能不存在）。"""
    return SummaryRun.all_objects.filter(kind=KIND, date=week_end).first()


W0_SYSTEM = (
    "你是个人情报助理。基于**过去 7 天的日报合集**与**本周 star 清单**"
    "生成一份 Markdown 周报，只输出周报正文。"
)

W0_USER = """基于下面【过去 {n} 天的日报合集】生成一份 Markdown 周报。

【硬性要求】
0. **不复述、只判断**。不要按天罗列"10-27 发生了…、10-28 发生了…"，
   要写**跨天**才看得出的东西：同一条线索在一周里的推进、反复出现的信号、
   两个源互相矛盾的说法、一件小事如何变成了大事。
1. **结构**：直接从「## 二、本周主线」开始，接着「## 三、跨周趋势与矛盾」、
   「## 四、下周关注与阅读优先级」、「## 五、star、划线、批注里的线索」。
   ★ 不要输出一级标题（`# …`），不要输出「本周概览 / 数量统计」段 —— 那些由系统生成。
   ★★ **不许解读任何统计数字**：条目数、占比、集中度、源产出一律不许复述或评论。
      统计是系统给的运维口径，读者不关心。
2. **引用**：每条信息后必须**原样抄回**素材里的 `[源名 · 时分](URL)`。漏引用视为失败。
   引用**紧跟**它所支撑的那句话，不要堆在段末；不要改成 `[#id]` 或纯文字。
3. **观点必须归到人**：素材里每个源名就是发言者。写"X 认为…；Y 则表示…"，
   不要揉成"有观点认为…"。
4. **数字必须与素材完全一致**：不得四舍五入、不得改写、不得补出素材里没有的数字。
5. **不许给自己找借口**：不要写「素材未提供引用链接」这类话 —— 每条素材都带引用。
6. **区分证据等级**：你的推断标「（推测）」；来自素材的标「实测」。不要把推断写成事实。
7. **「## 二、本周主线」**：≤5 条，每条带引用。只写这一周里**真正推进了**的事
   （昨天开了什么会、今天涨了多少这种单日新闻型的不写 —— 它已经在那天的日报里了）。
8. **「## 三、跨周趋势与矛盾」**：写单篇/单日看不出来的东西。**没有就直说
   「本周无跨周趋势」**，不要凑数、不要硬凑鸡汤式建议。
9. **「## 四、下周关注与阅读优先级」**：分「必读 / 选读」两档点名具体文章，
   每档 1–3 条、带引用，并各用一句话说明为什么。
10. **篇幅上限 2000 字**。宁缺毋滥 —— 写不完就先写重要的，绝不为了凑数拉长。
11. **「## 五、star、划线、批注里的线索」**：这是本周唯一需要你**主动挖掘**的地方。
    三样东西都是我的注意力，但**含义不同**：star = 整篇在意；划线 = 这一段在意；
    ✎ 旁注 / 批注 = 我自己敲的字（层级见第 12 条）。
    ★ 本节第一行的「本周共加星 N 篇 / 划线与批注 N 条」由系统插入，**你不要写**，
      也不要数数 —— 直接从线索正文开始。
    请从【本周你加了星的文章】+【本周你在日报、文章、对话里划的线与批注】里
    找出**只有把多条放在一起才看得出的东西**：
    · 反复出现的主题（≥2 条在讲同一件事、同一个人、同一条产业链）
    · **互相矛盾**的说法（同一件事上两个相反判断 —— 这最值钱）
    · ★★ **跨类型对照（本节重点，也最容易出彩）**：同一件事上，我 star 了一篇文章、
      划了另一篇里的句子、又在对话里追问过 —— 三者可能互相补强，也可能互相打脸。
      素材里每条痕迹的 `[日报]` / `[文章《…》]` / `[对话：关于《…》]` 标签就是用来
      做这个对照的：同一天里出现**指向同一主题的多种标签**，说明我在那件事上
      花了好几倍的力气，值得单独说。
    · 和你本周主线**呼应或打脸**的地方
    · 我明显在**追着一条线**往下看（连着星、或连着划同一主题）
    · ★「— 你在本周回看的旧材料 —」段要**单独考虑**：那是我本周回头翻的旧内容，
      说明我在串一条跨周的线。可以写成"上周读过的、这周又回去划"。
    · 硬性：**2–4 条**，每条至少涉及 **2 条**痕迹（star 或划线都可以），
      **点名**是哪篇/谁说的，带引用。
    · 禁止：写"本周星了 N 篇""投资 10 篇 AI 7 篇"这类数量复述；禁止逐篇摘要。
    · **没料就只写一句「本周的星标与划线没有明显聚类」**，不许凑数、不许编关联。
12. **「划线」和「批注」不是一回事，绝对不能混**（这一条最容易出错，也最容易
    让我以为你在替我想事）：
    · `划线：…` 是**我标出的别人的原文** —— 只说明"我在意这一段"，可能是重要事实，
      也可能是我认可或存疑的**他人**判断。**它不是我的观点**：不许写成
      "我认为…""我判断…"，只能写"我标出了…""这一段我特别在意"。
      ★ `[对话：关于《…》]` 里的划线，划的是 **AI 的回复** —— 同样是"别人说的话"，
        不是我说的，只能当"我在意这个回答里的这句话"用。
    · `✎ 我在划线旁写的话` 和 `批注：…` 是**我自己敲的字**，**这才是我的判断**，
      权重最高（高于 star，也高于划线本身）。
    · 只有划线、没有旁注的地方，只能当**关注信号**用，**不许替它编一个观点**。
    · 用法：「本周主线」优先写我标过/批注过的线；「下周关注」优先推我踩过的线。
      本周一条都没有就按日报正常写，不要硬扯。

【本周窗口】{window}
【覆盖】{covered}
【读者画像】（判断"对我重不重要"的依据；**不是分级判据**）
{profile}
【本周你在日报、文章、对话里划的线与批注】（★ 三层语义，见第 12 条：划线≠我的判断）
{annotations}
【本周你加了星的文章】（手动确认 = **整篇**在意；`[分类]` 是我的 RSS 分类，
可用于找共同主题；引用原样可用）
{stars}

【过去 {n} 天的日报】
{body}"""


# ==================================================================== 窗口与素材
def week_days(week_end: _date, days: int = WINDOW_DAYS) -> list:
    """周报覆盖的报告日（升序）：`week_end-(n-1) .. week_end`。

    ★ 入口归一成 date：`week_end` 若是 datetime，减完 timedelta 还是 datetime，
      下游拼 `daily-2026-10-26T12:38:58+08:00.md` 就永远找不到日报（见 `_as_date`）。
    """
    week_end = _as_date(week_end)
    return [week_end - timedelta(days=i) for i in range(days - 1, -1, -1)]


def window_span(week_end: _date, days: int = WINDOW_DAYS):
    """本期周报**真实覆盖**的 `[start, end)` —— 窗口口径的唯一来源。

    日报 D 的窗口是 `[D-1 N:00, D N:00)`（`summary.window_for`），所以 days 份日报
    （`week_end-days+1 .. week_end`）的合集是：

        `[week_end-days N:00, week_end N:00)`

    ★★ 起点减的是 **days**，不是 `days-1`。`days-1` 算出来的是「最后一份日报的
      起始日」（D-1），拿它当周报起点等于把 7 天说成 6 天：
      week_end=11/01 时印成「10-26 06:00 → 11-01 06:00」，而实际 10/26 那份日报
      的窗口就是从 **10-25** 06:00 开始的（2026-11-01 用户指出）。
    ★ 小时数取 `sk.effective("summary_window_hour")`，与日报**同源**：原来直读
      `settings.SUMMARY_WINDOW_HOUR`，而那是设置页可即时改的旋钮 —— 改了之后
      日报跟着动、周报的标签和 star 窗还停在旧值，同一件事两个来源必漂。
    """
    hour = int(sk.effective("summary_window_hour"))
    week_end = _as_date(week_end)

    def at(d):
        return timezone.make_aware(
            datetime.combine(d, datetime.min.time()).replace(hour=hour)
        )

    return at(week_end - timedelta(days=days)), at(week_end)


def window_label(week_end: _date, days: int = WINDOW_DAYS) -> str:
    """窗口标签（自解释，不靠人回忆口径）。只排版，不自己算日期。"""
    start, end = window_span(week_end, days)
    return f"{start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M}"


#: 日报里的"系统生成 / 与周报无关"的部分：头部 `>` 元信息、一级标题、数量统计段
_RE_HEAD_META = re.compile(r"^>\s")
_RE_H1 = re.compile(r"^#\s+\S")
_RE_STATS = re.compile(r"^#{1,3}\s*[一二三四五六七八九十0-9、. 　]*数量统计")


def clean_daily(md: str) -> str:
    """一份日报 → 周报素材：去掉头部元信息、一级标题、系统统计段。

    ★ 统计段为什么要删：① 它是系统渲染的运维口径，喂给模型它就会去解读
      （日报实测：每期札记都在写"TOP3 源占比过大"）；② 7 份统计段塞进素材
      是一大坨纯数字，白占上下文。
    ★ 不删「异常告警」：周报的异常段要看它们（且日报异常是原样保留的状态码）。
    """
    out, skipping = [], False
    for ln in (md or "").split("\n"):
        if _RE_STATS.match(ln):
            skipping = True
            continue
        if skipping:
            if re.match(r"^##\s+\S", ln):
                skipping = False
            else:
                continue
        if _RE_HEAD_META.match(ln) or _RE_H1.match(ln):
            continue
        out.append(ln)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def collect(week_end: _date, days: int = WINDOW_DAYS) -> dict:
    """取 7 份日报。返回 `{parts, missing, degraded, covered}`。

    ★ `missing` 按"缺的是不是今天"分类由调用方决定怎么处理 —— 本函数只报事实。
    ★ `degraded`：LLM 没产出、落成规则版的那些天（素材是空洞的，不是数据错误）。
    """
    week_end = _as_date(week_end)
    parts, missing, degraded = [], [], []
    for d in week_days(week_end, days):
        md = reportstore.read(d)
        if not md:
            missing.append(d)
            continue
        text = clean_daily(md)
        if not text:
            degraded.append(d)
            continue
        row = SummaryRun.objects.filter(date=d).first()
        # 规则版（s0=0）的正文是条目清单，不是概述 —— 标出来，别让它悄悄拉低周报
        if row and row.state == "failed":
            degraded.append(d)
        parts.append((d, text))
    return {
        "parts": parts,
        "missing": missing,
        "degraded": degraded,
        "covered": len(parts),
        "need": days,
    }


def build_material(parts) -> str:
    """7 份日报 → 一段素材（每天一段，标清是哪天的）。"""
    return "\n\n".join(f"===== 日报 {d.isoformat()} =====\n{text}" for d, text in parts)


# ==================================================================== star
def _star_window(week_end: _date, days: int = WINDOW_DAYS):
    """star 的归属窗 = **本期日报的覆盖窗**（`window_span`，同一个区间）。

    ★ 必须与日报合集同窗：一篇文章在 star 段里算"本周"，在日报里也必须算本周，
      否则同一篇内容在两处归属到不同周。
    ★ 相邻两期首尾相接、不空也不重叠 —— 下一期 `week_end' = D+days`，起点
      `(D+days)-days = D` 的 N:00，正好等于本期终点。

    ★★ 2026-11-01 修：原实现取 `[D-(days-1) N:00, D+1 N:00)`，整体**后移一天**。
      它当时怕的"空出 `[D, D+1)` 整天"其实不存在 —— 那是把下一期起点也当成
      `D+1` 才得出的结论（下一期起点若按同一公式算就是 `D+days-(days-1) = D+1`）。
      真代价是两头都错：本期第一天 `[D-7, D-6)` 的星漏掉（那段属于日报 10/26），
      却把下一期第一天 `[D, D+1)` 的星提前算进来（那段属于日报 11/02），
      同一个星在两期各算一次。
    """
    return window_span(week_end, days)


def _feed_categories(ds) -> dict:
    """`feed_id → 分类名`（取 Miniflux 的分类树）。

    ★ 为什么要它：star 段原来是一张平铺列表，短动态和真文章混在一起，
      一周二三十篇就读不出结构。分类是**现成的**（用户自己分好的），不需要模型
      猜、不额外花钱。
    ★ 取不到就返回空 dict —— star 段退化成"未分类"一组，不影响主流程。
    """
    try:
        out = {}
        for c in ds.list_tree():
            cat = (getattr(c, "title", "") or "").strip() or "未分类"
            for f in getattr(c, "feeds", None) or []:
                out[int(f.id)] = cat
        return out
    except Exception as exc:  # noqa: BLE001  分类是展示增强，失败不该拖垮 star 段
        log.warning("取源分类失败（star 段退化为不分组）：%s", exc)
        return {}


def star_entries(week_end: _date, days: int = WINDOW_DAYS) -> list:
    """本周加星的文章（按 StarEvent 时间窗；无记录者按 published_at 补记后纳入）。

    ★ 为什么要"对账"：Miniflux 不记加星时间（`entries` 只有 `starred` 布尔），
      时间只能本地记（`hx/star` 写 `source="reader"`）。但**在 Miniflux 原生 UI
      点星**的那部分我们没有事件 —— 不补记，周报的 star 段会系统性漏掉。
      补记口径 = `published_at`（用户拍板：没有时间戳就按发布时间来）。
    ★ 补记只能覆盖"发布时间落在窗口内"的条目。发布时间在窗口外的老文章
      （比如本周给一篇两周前的文章加星）**补记不了** —— 那类只能靠
      `hx/star` 的真实时间戳，所以那条写入路径不能省（见 reader.views.hx_star）。
    ★ 口径：只算**当前仍带星**的（要拿标题/URL 必须回 Miniflux 匹配）。
      本周加星后来又取消的，事件还在表里但**不进本周 star 段** —— 语义上"取消"
      就是撤回，合理；副作用是同一期重跑数字可能变小（实测 32 → 27 有 4 篇被取消）。
    """
    start, end = _star_window(week_end, days)
    _reconcile_stars(start, end)
    rows = list(
        StarEvent.objects.filter(ts__gte=start, ts__lt=end, starred=True).order_by(
            "-ts"
        )
    )
    if not rows:
        return []
    from core.datasource import get_datasource

    ds = get_datasource()
    by_id = {int(r.entry_id): r for r in rows}
    cats = _feed_categories(ds)
    got = []
    try:
        for e in ds.list_entries(starred=True, status="all", limit=500):
            if int(e.id) in by_id:
                got.append((by_id[int(e.id)], e))
    except Exception as exc:  # noqa: BLE001  star 段是附加值，取不到就退化成只列 id
        log.warning("拉 starred 条目失败（周报 star 段降级）：%s", exc)
        return [
            {
                "entry_id": r.entry_id,
                "feed_id": r.feed_id,
                "title": "",
                "published_at": r.ts,
                "url": "",
                "source": r.source,
                "feed_name": "",
                "category": cats.get(int(r.feed_id or 0), ""),
            }
            for r in rows
        ]
    got.sort(
        key=lambda kv: _aware(kv[1].published_at or kv[1].created_at) or kv[0].ts,
        reverse=True,
    )
    out = []
    for ev, e in got:
        ts = _aware(e.published_at) or ev.ts
        out.append(
            {
                "entry_id": int(e.id),
                "feed_id": int(e.feed_id),
                "title": (e.title or "").strip(),
                "published_at": ts,
                "url": (e.url or "").strip(),
                "source": ev.source,
                "feed_name": (getattr(e, "feed_title", "") or "").strip(),
                "category": cats.get(int(e.feed_id or 0), ""),
            }
        )
    return out


def _aware(ts):
    """naive → 本地时区 aware（Miniflux 给的是 aware，但桩/边界数据可能是 naive）。

    ★ 不在这里做兼容，后面 `start <= ts < end` 与排序都会抛
      "can't compare offset-naive and offset-aware"。
    """
    if ts is None:
        return None
    if timezone.is_naive(ts):
        return timezone.make_aware(ts)
    return ts


def _reconcile_stars(start, end) -> int:
    """把「已 star 但没有本地事件」的条目补一条 `inferred`（时间 = published_at）。

    只补**落在窗口内**的：窗口外的老条目补了也没人看，还会把表撑大。
    返回补记条数。
    """
    from core.datasource import get_datasource

    known = set(StarEvent.objects.values_list("entry_id", flat=True))
    ds = get_datasource()
    try:
        all_starred = ds.list_entries(starred=True, status="all", limit=500)
    except Exception as exc:  # noqa: BLE001
        log.warning("star 对账失败（沿用已有事件）：%s", exc)
        return 0
    created = 0
    start, end = _aware(start), _aware(end)
    for e in all_starred:
        if int(e.id) in known:
            continue
        ts = _aware(e.published_at or e.created_at)
        if ts is None or not (start <= ts < end):
            continue
        StarEvent.objects.create(
            entry_id=int(e.id),
            feed_id=int(e.feed_id or 0),
            starred=True,
            ts=ts,
            source="inferred",
        )
        created += 1
    if created:
        log.info(
            "star 对账：补记 %s 条（Miniflux UI 点的星，按发布时间归周）",
            created,
            extra=fields(src="weekly", stars=created),
        )
    return created


def star_material(entries: list) -> str:
    """star 段素材：**先给标准引用形态**，再给分类和标题。

    ★ 形态必须是 `[源名 · 时:分](深链)`——与正文引用同一种写法。第一版把分类
      写在最前面（`- [投资] 标题 [10-29 04:54](link)`），模型就照着写成了
      `[投资 · 10-29 04:54]`：读者看不出**是谁说的**，正好违反"观点必须点名"。
      素材长什么样，引用就长什么样，这里不能自创格式。
    ★ 分类放在引用之后当**聚类抓手**：只说"这周星了 27 篇"模型挖不出东西，
      但看到「[投资] 六条全是某源的期权仓位碎碎念」就能说出点具体的。
    """
    if not entries:
        return "（本周没有加星）"
    lines = []
    for e in entries:
        name = (e.get("feed_name") or "").strip() or (e.get("category") or "未分类")
        ts = (
            f"{e['published_at'].astimezone():%m-%d %H:%M}" if e["published_at"] else ""
        )
        label = f"[{name} · {ts}]" if ts else f"[{name}]"
        link = (
            rt.deep_link(e["feed_id"], e["entry_id"], "weekly")
            if e["feed_id"] and e["entry_id"]
            else ""
        )
        cite = f"{label}({link})" if link else label
        lines.append(
            f"- {cite} [{e.get('category') or '未分类'}] "
            f"{e['title'] or '（无标题）'}"
        )
    return "\n".join(lines)


def _group_stars(entries: list) -> list:
    """按分类分组：`[(分类, [条目…])]`，类按条数降序、类内保持传入顺序（时间倒序）。"""
    by = {}
    for e in entries:
        cat = (e.get("category") or "").strip() or "未分类"
        by.setdefault(cat, []).append(e)
    return sorted(by.items(), key=lambda kv: (-len(kv[1]), kv[0]))


def starred_url(src: str = "weekly") -> str:
    """阅读器的「全部星标」页（`sel=starred`）。"""
    base = (getattr(settings, "NOTIFY_READER_BASE", "") or "").rstrip("/")
    return f"{base}/?sel=starred&src={src}" if base else ""


def star_brief(entries: list, marks: dict = None) -> str:
    """第五节的一行摘要 —— **系统生成**，插在「五、star、划线、批注里的线索」标题下。

    ★ 为什么不再单列一章清单（2026-10-29 调整）：一周 27–60 篇全列出来，
      读者既不会扫也扫不完；清单本身也不产生信息。
      真正要读的是**线索**，要翻细节去阅读器的星标页 —— 所以这里只留"数量 +
      分类分布 + 一个链接"，明细交给页面。
    ★ 分类计数仍然有用：它是"这周我在关注什么方向"的**分布**（投资 10 / AI 7…），
      而不是标题列表。分组顺序与条数都来自 `_group_stars`，类是降序的。
    ★ 痕迹条数并进同一行（2026-11-01）：star 和划线本来就是同一件事的两个粒度
      （整篇在意 / 这一段在意），摘要拆成两行反而让人以为它们无关。
      没有 star 但**有**痕迹时也要插这一行 —— 原来 `entries` 为空就返回 ""，
      会让「本周一条没星、却划了 20 条」的报告第五节光秃秃的开头。
    """
    parts = []
    n_marks = int((marks or {}).get("n") or 0)
    if entries:
        groups = _group_stars(entries)
        brief = " · ".join(f"{cat} {len(es)}" for cat, es in groups)
        parts.append(f"本周共加星 **{len(entries)}** 篇：{brief}")
    if n_marks:
        detail = f"划线 {(marks or {}).get('n_hl', 0)}"
        if (marks or {}).get("n_an"):
            detail += f" · 批注 {marks['n_an']}"
        if (marks or {}).get("n_review"):
            detail += f" · 本周回看 {marks['n_review']}"
        parts.append(f"划线与批注 **{n_marks}** 条（{detail}，来自日报/文章/对话）")
    if not parts:
        return ""
    url = starred_url()
    # ★ 标签短（`全部星标`）不用「→」：整行很长，中英混排换行会把箭头/末字甩到
    #   下一行单独成行（实测「查看全部星标 →」裂成「查看全部星」+「标 →」）。
    #   也刻意不写「（星标 = 你的手动确认）」—— 星是自己点的，属于自明信息。
    tail = f"｜[全部星标]({url})" if url else ""
    return "｜".join(parts) + tail


#: 模型写的第五节标题。★ 只认关键词、不认编号 —— 章节号是模型自己写的
#   （历史实现里换过一次编号，硬编码 `## 五、` 会静默失配）。
_RE_STAR_HEAD = re.compile(r"^#{2,3}[^\n]*(?:star|星标|划线|批注)[^\n]*$", re.M | re.I)


def insert_star_brief(body: str, brief: str) -> str:
    """把摘要行插到「star 里的线索」标题下、正文前。

    ★ 必须**插进模型那一段**而不是另起一章：摘要属于这一节的开头（"这周星了
      多少、都是什么方向"是读线索的前提）。标题找不到时**追加到末尾**而不是
      丢弃 —— 摘要是系统真值，静默消失比位置难看严重得多。
    """
    body = body or ""
    if not brief:
        return body
    m = _RE_STAR_HEAD.search(body)
    # ★ 前后都必须留**空行**：只隔一个 \n 属于 CommonMark 的软换行，摘要会和
    #   第一条线索被渲染进同一段（实测就是这样粘在一起的）。
    if not m:
        return body.rstrip() + "\n\n" + brief + "\n"
    return body[: m.end()] + "\n\n" + brief + "\n\n" + body[m.end() :].lstrip("\n")


#: `2026-10-23：<正文>` —— `anomalies_of()` 给每条都加了日期前缀
_RE_ANOM_PREFIX = re.compile(r"^(?P<day>\d{4}-\d{2}-\d{2})：(?P<body>.*)$", re.S)
#: 源级异常正文：`- ⚠️ #9 某源：解析错误（…）` / `      - ✗ #27 源名：HTTPError: 500 …`
#  ★ 只认「装饰符 + #id + 源名 + 冒号」这一种形态。刻意不匹配
#    `- 本次实际刷新：#3、#4…`（数字后是顿号）与 `- ≥6h（2）：#27…`（# 前有文字），
#    否则过程日志会被当成异常条目。
_RE_ANOM_SRC = re.compile(
    r"^[\s\-•]*[^\w\s#]{0,3}\s*#(?P<fid>\d+)\s+(?P<name>[^：:\n]{1,40}?)[：:]\s*(?P<detail>.+)$",
    re.S,
)
#: 宿主机/采集停摆：机器睡了，不是源坏了 —— 单独说，不与"源异常"混算
_RE_ANOM_HOST = re.compile(r"宿主机|停摆|Mac 休眠")

_KIND_RULES = (
    ("404 资源不存在", ("404", "not found")),
    ("站点 500", ("500", "internal server error")),
    ("403 拒绝", ("403", "forbidden")),
    (
        "网络不可达",
        (
            "network error",
            "no such host",
            "timeout",
            "connection refused",
            "dial tcp",
            "tls handshake",
        ),
    ),
)


def _anom_kind(detail: str) -> str:
    low = (detail or "").lower()
    for label, keys in _KIND_RULES:
        if any(k in low for k in keys):
            return label
    return "其他错误"


def anomaly_digest(items: list) -> dict:
    """把 7 天异常行压成**统计规律**（不列明细）。

    ★ 为什么不再列明细（2026-10-29 用户要求）：异常当天已经在日报里逐条处理过了，
      周报再抄一遍既抄不完也没人看。周报该回答的是两个问题：
      **"是不是一次事故"** 和 **"哪个源一直没好"**。
    ★ 为什么必须先剔过程日志：实测 61 行里 **39 行**是「补救刷新：候选 73 个…」
      「✅ 等待 0s 后已全部补齐」这类**过程记录**，根本不是异常。旧实现按 `#id`
      硬抓，一块里 7 个源会被算成 1 类、只显示第一个源名 —— 数字和事实都错。
    ★ 同一源同一天既有 `⚠️ 解析错误` 又有 `✗ 刷新失败` 时**只算一次**（保留先出现的
      解析错误，它信息更具体）。不合并的话"5 个源坏了"会算成 10 次，还会伪造出
      一个"5 个源同时 500"的假爆发。
    """
    raw = len(items)
    seen, per_feed = {}, {}
    by_kind, by_day = {}, {}
    host_days, matched = [], 0
    for x in items:
        m = _RE_ANOM_PREFIX.match(x or "")
        if not m:
            continue
        day, body = m.group("day"), m.group("body")
        s = _RE_ANOM_SRC.match(body)
        if not s:
            if _RE_ANOM_HOST.search(body) and day not in host_days:
                host_days.append(day)
            continue
        matched += 1
        fid = s.group("fid")
        if (day, fid) in seen:
            continue  # 同一天同一源的第二条（刷新失败）不重复计
        kind = _anom_kind(s.group("detail"))
        seen[(day, fid)] = kind
        name = s.group("name").strip()
        rec = per_feed.setdefault(fid, {"name": name, "days": []})
        if len(name) > len(rec["name"]):
            rec["name"] = name
        rec["days"].append(day)
        by_kind[kind] = by_kind.get(kind, 0) + 1
        by_day.setdefault(day, []).append((fid, kind))

    persist = []
    for fid, v in per_feed.items():
        if len(v["days"]) < 2:
            continue
        days = sorted(v["days"])
        kinds = {}
        for d0 in days:
            k = seen[(d0, fid)]
            kinds[k] = kinds.get(k, 0) + 1
        persist.append(
            {
                "feed_id": fid,
                "name": v["name"],
                "days": days,
                "n": len(days),
                "kind": max(kinds.items(), key=lambda kv: kv[1])[0],
            }
        )
    persist.sort(key=lambda r: (-r["n"], int(r["feed_id"])))

    bursts = []
    for day, evs in by_day.items():
        kinds = {}
        for fid, kind in evs:
            kinds.setdefault(kind, set()).add(fid)
        kind, fids = max(kinds.items(), key=lambda kv: len(kv[1]))
        if len(fids) >= 5 and len(fids) >= 0.6 * len(evs):
            bursts.append({"day": day, "n": len(fids), "kind": kind, "total": len(evs)})
    # ★ 停摆那天的大量报错是同一个根因，由「宿主机停摆」那行交代，不再单列爆发
    bursts = [b for b in bursts if b["day"] not in host_days]
    bursts.sort(key=lambda b: (-b["n"], b["day"]))

    return {
        "raw": raw,
        "events": len(seen),
        "feeds": len(per_feed),
        "logs": raw - matched - len(host_days),
        "by_kind": by_kind,
        "persist": persist,
        "bursts": bursts,
        "host_days": sorted(host_days),
    }


def anomaly_section(items: list) -> str:
    """「本周异常」段 —— **只写统计规律**（不列明细，明细看当天日报）。"""
    if not items:
        return ""
    d = anomaly_digest(items)
    L = [
        "## 六、本周异常（系统生成）",
        "",
        f"- 源级异常 **{d['events']}** 次，涉及 **{d['feeds']}** 个源"
        f"（同一源同一天只算一次）"
        + (
            f"；另有 {d['logs']} 条过程日志（补救刷新统计 / 等待结果）不计入"
            if d["logs"] > 0
            else ""
        ),
    ]
    for b in d["bursts"][:2]:
        same = (
            f"，其中 {b['n']} 个同为「{b['kind']}」"
            if b["n"] != b["total"]
            else f"，全部为「{b['kind']}」"
        )
        L.append(
            f"- **一次性爆发**：{b['day'][5:]} 一天就有 {b['total']} 个源报错"
            f"{same} → 更像单次网络/宿主机事故，不是这些站同时坏"
        )
    if d["persist"]:
        seg = "；".join(
            f"#{p['feed_id']} {p['name']}（{p['n']} 天 {p['days'][0][5:]}~"
            f"{p['days'][-1][5:]}，{p['kind']}）"
            for p in d["persist"][:4]
        )
        L.append(f"- **持续性（要动手）**：{seg}")
    else:
        L.append("- **持续性**：没有源连续两天以上报错")
    if d["by_kind"]:
        seg = " · ".join(
            f"{k} {v}" for k, v in sorted(d["by_kind"].items(), key=lambda kv: -kv[1])
        )
        L.append(f"- 错误类型：{seg}")
    if d["host_days"]:
        L.append(
            f"- 宿主机/采集疑似停摆 **{len(d['host_days'])}** 天（"
            + "、".join(x[5:] for x in d["host_days"])
            + "）"
        )
    L.append("")
    return "\n".join(L)


# ==================================================================== 我留下的痕迹
#: 单条摘录 / 备注 / 批注进素材的字数上限（去掉噪音长段落，不是截断正文）
_MARK_CLIP = 200

#: 扫 entry / chat 痕迹的上限（按划线时间倒序取）。
#: ★ 为什么可以只取最近这些条：**内容归属本期**的痕迹，其划线动作必然发生在
#:   内容产生之后 ⇒ 倒序取最近的不会漏掉本期；只会漏掉「很久以前划的旧痕迹」，
#:   而那类既不属于本期内容、也不再是本周回看，本来就不该进素材。
_MARK_SCAN = 500

#: 痕迹条数超过它就先分片提炼再喂 W0。
#  ★★ 阈值不能拍脑袋定小（2026-11-01 实测教训）：第一版设 40，本周 42 条就触发了
#     分片 —— 而 42 条 ≈ 2500 字 ≈ 2000 tokens，相对 7 份日报的 5 万 tokens
#     完全是零头。为了省这点上下文花了 ¥0.19、跑了 4 分钟，还因为推理吃光分片
#     预算把整期周报拖成「达到单次调用上限」。现在按 200 条（≈1.2 万字）才分片：
#     日常几十条直接并进 W0，一次调用搞定。
MARK_CHUNK_THRESHOLD = 200
#: 每片的条数（分片一旦触发，片内别太大：单片摘要本身要保持可读）。
MARK_CHUNK_SIZE = 60


def _clip(text: str, limit: int = _MARK_CLIP) -> str:
    """压成一行并限长（素材里每条痕迹不该超过几行）。"""
    s = " ".join((text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


@lru_cache(maxsize=512)
def _entry_meta(entry_id: int):
    """entry → {"published_at", "feed_id", "title"}；取不到返回 None。

    ★ 必须缓存：同一篇文章常有多条划线（实测 8 条全在同一篇），不缓存就是
      8 次 Miniflux 请求。`published_at` 不会变，缓存没有失效风险。
    ★ 取不到返回 None 而不是抛：元数据缺失只让这一条「日期不明」，
      不能连带整期周报的痕迹全丢。
    """
    try:
        from core.datasource import get_datasource

        e = get_datasource().get_entry(int(entry_id))
        if e is None:
            return None
        return {
            "published_at": _aware(getattr(e, "published_at", None)),
            "feed_id": getattr(e, "feed_id", None),
            "title": (getattr(e, "title", "") or "").strip(),
        }
    except Exception as exc:  # noqa: BLE001  痕迹是加分项，元数据取不到要降级不要炸
        log.warning("取 entry 元数据失败 %s: %s", entry_id, exc)
        return None


def _daily_of(ts, hour: int):
    """时间戳 → 它落在哪份日报。日报 D 覆盖 `[D-1 N:00, D N:00)`。

    ★ 这就是「内容归属日」的换算规则，全模块只有这一份 —— `window_for` 的反函数。
      10-28 07:00 属于 `daily-10-29`（而不是 10-28），因为 10-28 那份日报在
      06:00 就截稿了。
    """
    if ts is None:
        return None
    t = timezone.localtime(_aware(ts))
    return t.date() + timedelta(days=1) if t.hour >= hour else t.date()


def _mark_line(it: dict, hint: str = "") -> list:
    """一条痕迹 → 素材行（来源标签 + 层级 + 深链）。

    `hint` 拼进来源标签里（回看段用它标「发布于 X，本周回看」）。
    """
    tag = "[日报]"
    if it["src"] == "entry":
        # ★ 标题必须截短：同一篇文章常有 8 条划线，标题原样重复 8 次会把
        #   素材撑成一大坨（实测那篇投资总结标题 60+ 字）。20 字足够识别。
        title = (it.get("title") or "").strip()
        if len(title) > 20:
            title = title[:19] + "…"
        tag = f"[文章《{title}》]" if title else "[文章]"
    elif it["src"] == "chat":
        # ★ 必须说清「这场对话是关于哪篇内容的」：不然模型看到一条
        #   `[对话] 划线：…` 无从判断它和同一天那篇被划的文章是不是同一件事，
        #   「跨类型对照」这个最重要的用法就直接失效了。
        ctx = (it.get("ctx") or "").strip()
        if len(ctx) > 20:
            ctx = ctx[:19] + "…"
        tag = f"[对话：关于《{ctx}》]" if ctx else "[对话]"
    if hint:
        tag = tag[:-1] + f" · {hint}]"
    # ★ 引用标签必须**带名字**，不能写「来源」：素材长什么样、模型就抄成什么样
    #   （与 star_material 的第一版同一个坑 —— 那里写了个「[投资]」，
    #   模型就照着写成「[投资 · 10-29 04:54]」，读者看不出是谁说的）。
    #   实测：给「[来源](url)」时，周报里就出现一堆 `[来源](...)` 的裸引用。
    if it.get("link"):
        who = {"report": "日报", "entry": "文章", "chat": "对话"}.get(it["src"], "来源")
        day = f"{it['content_day']:%m-%d}" if it.get("content_day") else ""
        tail = f" [{who}{' · ' + day if day else ''}]({it['link']})"
    else:
        tail = ""
    if it["kind"] == "an":
        return [f"  {tag} 批注：{it['body']}{tail}"]
    out = [f"  {tag} 划线：{it['body']}{tail}"]
    if it.get("note"):
        out.append(f"        ✎ 我在划线旁写的话：{it['note']}")
    return out


def week_marks(week_end, days: int = WINDOW_DAYS) -> dict:
    """本周「我留下的痕迹」：日报 + 文章 + 对话 的划线/批注。

    返回 `{"text", "n", "n_hl", "n_an", "n_review", "err"}`。
    `err` 非空 ⇒ 至少一类读取失败，报告头部必须留痕（否则「本周没划线」和
    「读失败」在成品里长得一模一样）。

    ★★ 两个时间，各管一件事（2026-11-01 用户拍板）：
      · `content_day` = 痕迹**指向的内容**属于哪份日报 —— 定「在不在本期」；
      · `marked_at`   = 我**动手划线**的时刻 —— 定「是不是本周回看」。
      用动作时间定窗口会同时制造两个 bug（真实数据都发生了）：
      ① 10-31 16:26 一次性补划 10-26~10-30 的旧日报，按动作时间会算成
         「10-31 一天的活动」，而它们谈的是整整五天的事；
      ② 11-01 09:36 划的那 8 条文章（内容属于本周）会等到**下一期**才进周报。

    ★ chat 的 `content_day` 取**被划线那条消息**的 created_at，不是划线的
      created_at —— 我划的是消息里的话，那话是消息产生时写的
      （chat/48 消息 12:28、划线 17:47，取错就跨窗口）。

    ★ 三层语义不在这一层判（禁令在 W0_USER 第 12 条），但这一层必须把
      「划线 / ✎ 划线旁的话 / 批注」分开写：一旦揉成一段「批注」，
      模型必然替我编一个观点出来。
    """
    week_end = _as_date(week_end)
    hour = int(sk.effective("summary_window_hour"))
    wdays = week_days(week_end, days)
    wset = set(wdays)
    ws, we = window_span(week_end, days)
    err = ""
    items = []

    # ---- 1) 日报：来源键本身就是日期，content_day 零歧义
    for d in wdays:
        try:
            got = mk.list_marks("report", f"daily-{d.isoformat()}")
        except Exception as exc:  # noqa: BLE001  划线是加分项，读不到要直说
            err = f"{type(exc).__name__}: {exc}"[:200]
            break
        sid = f"daily-{d.isoformat()}"
        for h in got.get("highlights") or []:
            q = _clip(h.get("quoted_text"))
            if not q:
                continue
            items.append(
                {
                    "kind": "hl",
                    "body": q,
                    "note": _clip(h.get("note"), 300),
                    "content_day": d,
                    "marked_at": _aware(h.get("created_at")),
                    "src": "report",
                    "link": sourceblock.source_url(
                        "report", sid, mark_id=h.get("id"), src="weekly"
                    ),
                }
            )
        for a in got.get("annotations") or []:
            b = _clip(a.get("body"), 400)
            if not b:
                continue
            items.append(
                {
                    "kind": "an",
                    "body": b,
                    "note": "",
                    "content_day": d,
                    "marked_at": _aware(a.get("created_at")),
                    "src": "report",
                    "link": sourceblock.source_url(
                        "report", sid, mark_id=a.get("id"), src="weekly"
                    ),
                }
            )

    # ---- 2) 文章 / 对话：先按划线时间倒序取最近一批，再按内容归属日分桶
    try:
        from core.models import Annotation, ChatMessage, Highlight

        # ★ 软删 = 不想要：删掉的痕迹不该再被周报当素材收进来。
        hl_rows = list(
            Highlight.objects.live().filter(source_type__in=["entry", "chat"]).order_by(
                "-created_at"
            )[:_MARK_SCAN]
        )
        an_rows = list(
            Annotation.objects.live().filter(source_type__in=["entry", "chat"]).order_by(
                "-created_at"
            )[:_MARK_SCAN]
        )
    except Exception as exc:  # noqa: BLE001
        err = err or f"{type(exc).__name__}: {exc}"[:200]
        hl_rows, an_rows = [], []

    # 对话消息一次性取回（避免每条痕迹一次查询）
    msg_ids = [
        int(r.source_id)
        for r in (hl_rows + an_rows)
        if r.source_type == "chat" and (r.source_id or "").isdigit()
    ]
    msgs = (
        {m.id: m for m in ChatMessage.objects.filter(id__in=msg_ids)} if msg_ids else {}
    )
    convs = {}
    if msgs:
        from core.models import Conversation

        convs = {
            c.id: c
            for c in Conversation.objects.filter(
                id__in={m.conversation_id for m in msgs.values() if m.conversation_id}
            )
        }

    for r, is_hl in [(x, True) for x in hl_rows] + [(x, False) for x in an_rows]:
        st = r.source_type
        sid = (r.source_id or "").strip()
        body = _clip(r.quoted_text) if is_hl else _clip(r.body, 400)
        if not body:
            continue
        content_ts, link, title, feed_id, ctx = None, None, "", None, ""
        if st == "entry":
            meta = _entry_meta(int(sid)) if sid.isdigit() else None
            if meta:
                content_ts, feed_id = meta["published_at"], meta["feed_id"]
                title = meta["title"]
            if meta and meta["feed_id"]:
                link = rt.deep_link(meta["feed_id"], int(sid), "weekly", mark_id=r.id)
        elif st == "chat":
            msg = msgs.get(int(sid)) if sid.isdigit() else None
            if msg is None:
                continue  # 消息被删了，这条痕迹指向的东西已不存在
            content_ts = _aware(msg.created_at)
            # ai=1 让浮窗自动弹出；mark_id 让落点精确到消息里那条划线
            link = sourceblock.source_url(
                "chat", sid, ai=True, mark_id=r.id, src="weekly"
            )
            # 这场对话是围绕哪篇内容展开的 —— 跨类型对照要靠它
            conv = convs.get(msg.conversation_id)
            if conv is not None:
                csid = (conv.source_id or "").strip()
                if conv.source_type == "entry" and csid.isdigit():
                    ctx = (_entry_meta(int(csid)) or {}).get("title", "") or ""
                elif conv.source_type == "report" and csid:
                    ctx = csid[len("daily-") :] if csid.startswith("daily-") else csid
        items.append(
            {
                "kind": "hl" if is_hl else "an",
                "body": body,
                "note": _clip(r.note, 300) if is_hl else "",
                "content_day": _daily_of(content_ts, hour),
                "marked_at": _aware(r.created_at),
                "src": st,
                "title": title,
                "link": link,
                "ctx": ctx,
            }
        )

    # ---- 3) 分桶
    in_week, review, unknown = [], [], []
    for it in items:
        cd = it["content_day"]
        if cd is None:
            unknown.append(it)
        elif cd in wset:
            in_week.append(it)
        elif it["marked_at"] is not None and ws <= it["marked_at"] < we:
            # 内容不在本期、但我本周回头划了 ⇒ 跨周线索（用户认可先这样跑着看）
            review.append(it)
        # 其余：内容不在本期、也不是本周划的 —— 与本期无关，丢弃

    order = {"report": 0, "entry": 1, "chat": 2}
    ts_key = lambda x: x["marked_at"] or ws  # noqa: E731
    in_week.sort(key=lambda x: (x["content_day"], order.get(x["src"], 9), ts_key(x)))
    review.sort(key=lambda x: (x["content_day"] or _date.min, ts_key(x)))
    unknown.sort(key=lambda x: (order.get(x["src"], 9), ts_key(x)))

    lines, cur = [], None
    for it in in_week:
        if it["content_day"] != cur:
            cur = it["content_day"]
            lines.append(f"— {cur:%m-%d} —")
        lines.extend(_mark_line(it))
    if review:
        lines.append("")
        lines.append("— 你在本周回看的旧材料（内容不在本期，但你本周回头划了）—")
        for it in review:
            day = f"{it['content_day']:%m-%d}" if it["content_day"] else "日期不明"
            lines.extend(_mark_line(it, hint=f"发布于 {day}，本周回看"))
    if unknown:
        lines.append("")
        lines.append("— 日期不明（取不到发布时间，未计入本期）—")
        for it in unknown:
            lines.extend(_mark_line(it))

    # ★ 统计只数**进了素材的**：`items` 里还有一批「内容不在本期、也不是本周划的」
    #   被丢掉（chat 的长期会话最容易命中）。拿 items 全量数会让
    #   「概览里写 45 条、素材里只有 42 条」—— 读者一对就发现数对不上。
    used = in_week + review + unknown
    n_hl = sum(1 for it in used if it["kind"] == "hl")
    n_an = sum(1 for it in used if it["kind"] == "an")
    n = len(used)
    if err:
        return {
            "text": "（本期读取划线/批注失败，未使用）",
            "n": 0,
            "n_hl": 0,
            "n_an": 0,
            "n_review": 0,
            "err": err,
            "used": [],
        }
    if not lines:
        return {
            "text": "（本周没有划线，也没有批注）",
            "n": 0,
            "n_hl": 0,
            "n_an": 0,
            "n_review": 0,
            "err": "",
            "used": [],
        }
    return {
        "text": "\n".join(lines),
        "n": n,
        "n_hl": n_hl,
        "n_an": n_an,
        "n_review": len(review),
        "err": "",
        "used": used,
    }


#: 痕迹分片提炼的提示词。目标只有一个：**压长度、保引用、保矛盾**。
_DIGEST_SYSTEM = (
    "你是个人情报助理。把一批「我划的线与批注」压缩成线索摘要，"
    "供周报主笔写作使用。只输出摘要正文。"
)

_DIGEST_USER = """下面是本周我留下的一批痕迹（`划线` = 我标出的别人的原文；
`✎ 我在划线旁写的话` 与 `批注` = 我自己敲的字）。

【硬性要求】
1. **原样保留引用**（形如 `[日报 · 10-28](URL)` / `[文章 · 10-29](URL)`），
   每条信息后都要带；漏引用视为失败。
2. 优先写**反复出现的主题、互相矛盾的说法、指向同一件事的多条痕迹** ——
   这批就是为此才被单独拎出来的。
3. ★ 划线只说明"我在意这一段"，**不是我的判断**；只有 ✎ 旁注和批注才是我的观点。
   不要替我把划线解读成观点。
4. **不要罗列、不要逐条复述**。最多 8 条，宁缺毋滥。
5. 确实没有可归纳的就写「本批无明显聚类」，不许硬凑。

【痕迹】
{body}"""


def digest_marks(items: list, stats: dict, *, run_id=None, week_end=None) -> str:
    """痕迹太多时先分片提炼 —— 每片一次调用，拼成一段摘要喂 W0。

    ★ 什么时候才分片：`week_marks` 的条目上限是每类 500 条、每条最坏 200~400 字，
      全塞进 W0 会挤掉 7 份日报的位置（那才是周报的主素材）。条数超阈值就先压一层。
    ★★ 三条不能省的约束：
      ① **同一个池** `POOL_WEEKLY` —— 独立池会在周日静默 refuse（踩过的坑）；
      ② **必传 `run_id`** —— 否则 `LlmUsage` 的 run 关联为空，页面成本三列恒 0；
      ③ 某片失败**用原文顶上** —— 降级但不丢，同时靠 stats["errors"] 留痕。

    ★★ 两个实测教训（2026-11-01 真跑踩出来的，别再踩）：
      · **max_tokens 必须给够**：推理模型的推理链计入 completion_tokens，给 1600
        会「推理吃光预算、正文空串」，然后触发翻倍重试 —— 每次都是钱和时间
        （实测单片跑了 192 秒）。4096 起。
      · **不要用 `_call_cited`**：这里产的是**中间摘要**，模型常常不肯逐条抄链接，
        于是"无引用"再重试一次，白花一次调用。引用靠 prompt 要求即可，不重试。
    """
    from core import citeguard

    chunks = [
        items[i : i + MARK_CHUNK_SIZE] for i in range(0, len(items), MARK_CHUNK_SIZE)
    ]
    base = int(getattr(settings, "WEEKLY_MAX_CALLS", 2))
    # ★ 上限要跟着片数走：写死 2 会让第 3 片起全部返回「达到单次调用上限」，
    #   而且失败得很安静 —— 摘要只剩前两片，报告看起来正常。
    #   每片最多 2 次（原始 + 推理预算翻倍重试），再给 W0 留 base 次。
    cap = base + 2 * len(chunks)
    out = []
    for idx, chunk in enumerate(chunks, 1):
        body = "\n".join(line for it in chunk for line in _mark_line(it))
        got, err = citeguard._call(
            "summary_weekly",
            _DIGEST_SYSTEM,
            _DIGEST_USER.format(body=body),
            stats,
            tag=f"痕迹提炼 {idx}/{len(chunks)}",
            label=(week_end.isoformat() if week_end else ""),
            pool=cr.POOL_WEEKLY,
            run_id=run_id,
            max_tokens=4096,
            temperature=0.2,
            max_calls=cap,
        )
        if err or not got:
            out.append(f"（第 {idx} 片提炼失败，下面是原文）\n{body}")
            continue
        out.append(got.strip())
    return "\n\n".join(out)


# ==================================================================== 系统概览
def overview_section(
    week_end: _date, col: dict, anomalies: list, marks: dict = None
) -> str:
    """「一、本周概览」—— 系统生成。

    ★ 周报**不重算条目统计**（每天日报里已有一份，周报再列一遍是重复且要新采集）。
      这里只给周报层面的事实：窗口、覆盖、缺口、异常、点击、痕迹。
    ★ 点击数按来源分组给出分项（`clicktrack.group_counts`），分项之和恒等于
      总数；未知 src 归「其他」原样显示，不静默丢。
    ★ 加星数量**不放这里**：它移到「五、star、划线、批注里的线索」首行（与分类
      分布放一起，读者看那一节时才知道"总量有多大"）。同一件事写两遍就是噪音。
    """
    L = [
        "## 一、本周概览（系统生成）",
        "",
        f"- 窗口 {window_label(week_end)}（过去 {col['need']} 天日报的合集）",
        f"- 日报覆盖 **{col['covered']}/{col['need']}** 天",
    ]
    if col["missing"]:
        L.append("  - ⚠️ 缺：" + "、".join(d.isoformat() for d in col["missing"]))
    if col["degraded"]:
        L.append(
            "  - 其中 "
            + "、".join(d.isoformat() for d in col["degraded"])
            + " 为规则版（LLM 未产出，素材是条目清单）"
        )
    L.append(f"- 本周异常 **{len(anomalies)}** 条（聚合自 7 天的日报）")
    try:
        rows = clicktrack.load(days=8)
        if rows:
            # ★ 按来源摊开分项（分项之和恒等于总数，未知 src 归「其他」不吞）：
            #   原来那句「含日报/通知/L3」只是罗列了可能来源，没回答各点了几次。
            parts = " · ".join(
                f"{name} {n}" for name, n in clicktrack.group_counts(rows)
            )
            L.append(f"- 本周深链点击 **{len(rows)}** 次（按来源：{parts}）")
    except Exception:  # noqa: BLE001 点击是附加信息，读不到就不写
        pass
    if marks and marks.get("n"):
        parts = [f"划线 {marks.get('n_hl', 0)}"]
        if marks.get("n_an"):
            parts.append(f"批注 {marks['n_an']}")
        line = (
            f"- 本周留下 **{marks['n']}** 条痕迹（{' · '.join(parts)}，"
            f"来自日报/文章/对话）"
        )
        if marks.get("n_review"):
            line += f"，其中 **{marks['n_review']}** 条是本周回看的旧材料"
        L.append(line)
    L.append("")
    return "\n".join(L)


def anomalies_of(days: list) -> list:
    """聚合 7 天日报的异常（DB 里已有，不需要再问模型）。"""
    out = []
    for d in days:
        row = SummaryRun.objects.filter(date=d).only("anomalies_json").first()
        if not row or not row.anomalies_json:
            continue
        for a in row.anomalies_json:
            if isinstance(a, str):
                out.append(f"{d.isoformat()}：{a}")
    return out


# ==================================================================== 生成
def _stats_new() -> dict:
    """周报自己的 stats（口径比日报少，只留生成侧要用的字段）。"""
    return {
        "calls": 0,
        "tokens": 0,
        "reasoning_tokens": 0,
        "errors": [],
        "truncated": [],
        "budget_stop": False,
        "cite_missing": [],
        # 痕迹（划线/批注）三源统计：总数 / 划线 / 批注 / 本周回看
        "notes": 0,
        "n_hl": 0,
        "n_an": 0,
        "n_review": 0,
        "notes_err": "",
    }


def retag_src(body: str, src: str) -> str:
    """把从日报搬来的 `src=daily` 改写成 `src=weekly`。

    ★ 不改的话，周报里的点击全被 `clicktrack` 记成日报来源 —— **周报打开率
      永远统计不到**，而打开率正是要监控的东西（L3 打开率不该过低同理）。
    """
    return re.sub(r"([?&]src=)daily\b", rf"\g<1>{src}", body or "")


#: 深链锚点被模型抄坏：`…#row=4907`（把 `-` 写成 `=`）。实测 54 个链接里出现 1 次。
_RE_BAD_ANCHOR = re.compile(r"#row=(\d+)")


def fix_anchors(body: str) -> str:
    """修正常见的锚点笔误 —— 锚点坏了不会报错，只是点进去不滚到那一行。

    ★ 为什么值得单独修：`open=<id>` 还在，条目照样能打开，所以**没有任何检查
      会亮红**；坏掉的只有"滚到那一行"这个体验，而且只对这一个链接坏。
    """
    return _RE_BAD_ANCHOR.sub(r"#row-\1", body or "")


def run(week_end: _date = None, *, force: bool = False) -> dict:
    """生成一份周报并落盘。返回 `{ok, path?, reason?, ...}`。

    ★ 幂等：文件已存在且非 force ⇒ 直接返回 skipped（生成一次 ≈¥0.13，别乱花）。
    ★ 缺日报：缺历史 ⇒ 立即 fail；缺当天 ⇒ 返回 `wait`（调用方决定要不要再试）。
    """
    from core import summary

    # ★ 入口归一化：week_end 可能是调用方传进来的 datetime（guard 以前就传过），
    #   不归一就会拼出带时分秒的文件名，日报全变成"不存在"（见 _as_date）。
    week_end = _as_date(week_end or _due_week_end())
    days = int(WINDOW_DAYS)
    path = weekly_path(week_end)
    if path.is_file() and path.stat().st_size > 0 and not force:
        return {
            "ok": True,
            "skipped": True,
            "week_end": week_end,
            "path": str(path),
            "reason": "本周周报已存在（force=1 可重跑）",
        }

    col = collect(week_end, days)
    if col["missing"]:
        hist = [d for d in col["missing"] if d != week_end]
        if hist:
            return _fail(
                week_end, col, "缺历史日报：" + "、".join(d.isoformat() for d in hist)
            )
        return {
            "ok": False,
            "wait": True,
            "week_end": week_end,
            "reason": f"当天日报（{week_end.isoformat()}）还没出来，稍后重试",
        }
    if not col["parts"]:
        return _fail(week_end, col, "7 天日报一份都没有")

    row = _claim(week_end, force)
    if row is None:
        return {"ok": False, "week_end": week_end, "reason": "本周周报正在生成中"}
    stats = _stats_new()
    try:
        material = build_material(col["parts"])
        stars = star_entries(week_end, days)
        # ★ 三类痕迹（日报 / 文章 / 对话）一次性取回，见 week_marks 的口径注释
        marks = week_marks(week_end, days)
        # ★ 划线/批注读不到时必须能看出来：报告头部要留痕（见 _header），
        #   否则「这周没划线」和「读取失败」在成品里长得一模一样。
        stats["notes"], stats["notes_err"] = marks["n"], marks["err"]
        stats["n_hl"], stats["n_an"] = marks["n_hl"], marks["n_an"]
        stats["n_review"] = marks["n_review"]
        anom = anomalies_of([d for d, _ in col["parts"]])
        covered = (
            f"过去 {days} 天的日报全在（{col['covered']}/{col['need']} 天）"
            if col["covered"] == col["need"]
            else f"覆盖 {col['covered']}/{col['need']} 天"
        )
        star_txt = star_material(stars)
        # ★ 痕迹条数过多时先分片提炼，避免把 7 份日报的位置挤掉。
        #   注意 `marks["text"]` 仍保留原文：数字核对的基准必须是**给模型的全部输入**，
        #   用摘要当基准会把摘要里合法的数字误报成"编造"。
        marks_txt = marks["text"]
        max_calls = int(getattr(settings, "WEEKLY_MAX_CALLS", 2))
        if len(marks["used"]) > MARK_CHUNK_THRESHOLD:
            n_chunks = (len(marks["used"]) + MARK_CHUNK_SIZE - 1) // MARK_CHUNK_SIZE
            marks_txt = digest_marks(
                marks["used"], stats, run_id=row.id, week_end=week_end
            )
            # ★★ 上限必须加上分片数：分片已经花掉 stats["calls"]，W0 还传原值
            #   (2) 的话，`stats["calls"] >= cap` 立刻成立 ⇒ 周报直接返回
            #   「达到单次调用上限」，而且**没有任何异常**，只是报告没了。
            max_calls += 2 * n_chunks
            log.info(
                "痕迹 %d 条超过阈值 %d，已分 %d 片提炼",
                len(marks["used"]),
                MARK_CHUNK_THRESHOLD,
                n_chunks,
                extra=fields(src="weekly", week=str(week_end)),
            )
        text, err = citeguard._call_cited(
            "summary_weekly",
            W0_SYSTEM,
            W0_USER.format(
                n=days,
                window=window_label(week_end, days),
                covered=covered,
                profile=promptspec.profile_notes_block(),
                annotations=marks_txt,
                stars=star_txt,
                body=material,
            ),
            material,
            stats,
            tag="W0 周报",
            label=week_end.isoformat(),
            max_tokens=int(getattr(settings, "WEEKLY_MAX_TOKENS", 8192)),
            temperature=0.3,
            reject_uncited=False,
            pool=cr.POOL_WEEKLY,
            # ★★ 必须传 run_id：LlmUsage 没有 date 字段，不传就成 NULL，
            #    页面「¥/调用/tokens」三列永远是 0，成本只能靠 ts 猜区间。
            run_id=row.id,
            max_calls=max_calls,
        )
        if err and not text:
            return _fail(week_end, col, err, row=row)
        body = summary._strip_autogen(text or "")
        body = rt.space_cites(body)
        # ★ 审计只针对**模型输出**：系统段（概览/star/异常）是系统渲染的真值，
        #   它的数字与"要不要引用"不由模型负责（与日报同一条纪律）。
        p_hit, p_total, n_links = rt._point_coverage(body)
        stats["pt_hit"], stats["pt_total"], stats["cites"] = p_hit, p_total, n_links
        # ★ 数字核对的基准必须是**给模型的全部输入**，不只是 7 份日报：
        #   模型引用 star 标题里的数字（实测 1083）是完全合法的，拿日报当唯一
        #   基准会把它报成"疑似编造" —— 假警报比漏报更伤信任。
        stats["num_unknown"] = rt.number_audit(
            body, material + "\n" + star_txt + "\n" + marks["text"]
        )
        # ★ 时序契约：改 src 必须在审计之后（审计按链接目标匹配）
        body = retag_src(body, "weekly")
        body = fix_anchors(body)
        # ★★ 加星摘要也必须在审计之后插：它是**系统真值**（数量 + 分类分布，
        #   模型没参与计算）。插早了会被 `_point_coverage` 当成"没带引用的要点"
        #   而误报；也会被 `number_audit` 拿去找素材里根本没有的数字。
        body = insert_star_brief(body, star_brief(stars, marks))

        md = "\n".join(_header(week_end, col, stats))
        md += "\n" + overview_section(week_end, col, anom, marks)
        md += "\n" + body.strip() + "\n"
        an_md = anomaly_section(anom)
        if an_md:
            md += "\n" + an_md
        written = write_weekly(week_end, md)

        row.state = "done"
        row.source_count = col["covered"]
        row.missing_json = [d.isoformat() for d in col["missing"]]
        # ★ 异常总数进库：页面「规模」列要显示，不能让 UI 去解析自己生成的 md
        row.anomalies_total = len(anom)
        row.report_path = str(written)
        row.error = ""
        row.finished_at = timezone.now()
        row.save()
        _ACTIVE.pop(week_end, None)
        log.info(
            "周报生成完成 %s（%s 份日报，%s tokens）",
            week_end,
            col["covered"],
            stats["tokens"],
            extra=fields(src="weekly", week=str(week_end), tokens=stats["tokens"]),
        )
        # ★ 完成通知，落点是**本地周报页**
        try:
            notify_weekly(week_end, stars=len(stars))
        except Exception as exc:  # noqa: BLE001  通知失败不该让"生成"看起来失败
            log.warning("周报完成通知失败（文件已落盘）：%s", exc)
        return {
            "ok": True,
            "week_end": week_end,
            "path": str(written),
            "days": col["covered"],
            "tokens": stats["tokens"],
            "stars": len(stars),
            "anomalies": len(anom),
        }
    except Exception as exc:  # noqa: BLE001  job 里抛异常会被 APScheduler 吞掉
        log.error("周报生成异常 %s：%s", week_end, exc, exc_info=True)
        return _fail(week_end, col, f"生成异常：{exc}", row=row)


def _fail(week_end, col, reason, row=None) -> dict:
    """判失败并通知。

    ★★ **已有成品就不许改成 failed**：周报文件还在盘上，把状态写成 failed 只会让
      页面/通知与事实相反 —— 用户看到「周报未生成」，其实它就在那儿。
      2026-11-01 真实事故：12:24 生成成功后，12:38 的 guard 拿错了 week_end
      （datetime）判「缺历史日报」，`_claim(force=True)` 把 done 覆盖成 failed 并发了
      「周报未生成」通知。判据用「done + 文件在」，不看 notified/attempt 这些过程字段。
    """
    week_end = _as_date(week_end)
    if row is None:
        cur = SummaryRun.all_objects.filter(kind=KIND, date=week_end).first()
        if cur is not None and cur.state == "done" and weekly_path(week_end).is_file():
            _ACTIVE.pop(week_end, None)
            log.warning(
                "周报已有成品，忽略本次失败判定 %s：%s",
                week_end,
                reason,
                extra=fields(src="weekly", week=str(week_end)),
            )
            return {
                "ok": True,
                "skipped": True,
                "week_end": week_end,
                "reason": f"已有成品，忽略失败判定（{reason}）",
            }
    row = row or _claim(week_end, True)
    if row is not None:
        row.state = "failed"
        row.error = str(reason)[:500]
        row.source_count = col.get("covered", 0)
        row.missing_json = [d.isoformat() for d in col.get("missing", [])]
        row.finished_at = timezone.now()
        row.save()
    _ACTIVE.pop(week_end, None)
    log.warning(
        "周报未生成 %s：%s",
        week_end,
        reason,
        extra=fields(src="weekly", week=str(week_end)),
    )
    notify_weekly_failed(week_end, reason)
    return {"ok": False, "week_end": week_end, "reason": reason}


def _claim(week_end: _date, force: bool):
    """抢占本周的生成权（避免 guard 与 cron 同一毫秒各跑一次）。"""
    with _RUN_LOCK:
        t = _ACTIVE.get(week_end)
        if t is not None and t.is_alive():
            return None
        # ★ all_objects：默认 manager 只放行日报，用它建不出周报行
        row, _ = SummaryRun.all_objects.get_or_create(kind=KIND, date=week_end)
        if row.state == "summarizing" and not force:
            return None
        row.state = "summarizing"  # ★ 不是 "running"：要落进 summary.IN_FLIGHT
        row.attempt = int(row.attempt or 0) + 1
        row.started_at = timezone.now()
        row.notified = False
        row.save()
        _ACTIVE[week_end] = threading.current_thread()
        return row


# ==================================================================== 落盘
def weekly_path(week_end: _date):
    """周报文件路径。★ 归一成 date —— datetime 会拼出带冒号的非法文件名，
    既写不进也读不到（见 `_as_date` 的事故说明）。"""
    from pathlib import Path

    return Path(settings.REPORT_DIR) / f"weekly-{_as_date(week_end).isoformat()}.md"


def write_weekly(week_end: _date, md: str):
    """原子落盘（与 reportstore.write 同一姿势）。"""
    import os
    from pathlib import Path

    p = weekly_path(week_end)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".md.tmp")
    tmp.write_text(md, encoding="utf-8")
    os.replace(tmp, p)
    return p


def read_weekly(week_end: _date):
    p = weekly_path(week_end)
    try:
        if p.is_file() and p.stat().st_size > 0:
            return p.read_text(encoding="utf-8")
    except OSError:
        pass
    return None


def weekly_dates(limit: int = 12) -> list:
    """周报的 week_end（新→旧），**以库为准**。

    ★ 2026-10-29 改了口径：原来只扫 `weekly-*.md` 文件，导致**生成失败的周报
      在页面上完全不存在**（没有文件就没有行），也就没有任何手动重跑的入口。
      现在 SummaryRun 就是事实源，失败行照样列出来、照样能点「重跑」。
      文件在不在只影响「日期能不能点开看正文」。
    """
    return list(rows().values_list("date", flat=True)[:limit])


def _header(week_end, col, stats) -> list:
    lines = [
        f"# 谛听周报 · {week_end.isoformat()}",
        "",
        f"> 窗口 {window_label(week_end)}（本地时区，按 pubDate）",
        f"> 素材：过去 {col['need']} 天日报，实收 {col['covered']} 份"
        + (
            "；⚠️ 规则版 " + "、".join(d.isoformat() for d in col["degraded"])
            if col["degraded"]
            else ""
        ),
        f"> 生成方式：W0 1 次｜调用 {stats['calls']} 次｜{stats['tokens']} tokens"
        + (
            f"（含推理 {stats['reasoning_tokens']}）"
            if stats.get("reasoning_tokens")
            else ""
        ),
    ]
    if stats.get("notes_err"):
        # ★ 失败必须可见：否则「本周没划线」和「读取失败」在成品里同一副样子
        lines.append(
            "> ⚠️ 划线/批注：读取失败，**本期没用上你的划线**（"
            + stats["notes_err"]
            + "）"
        )
    if stats.get("truncated"):
        lines.append(
            "> ⚠️ **以下调用触到输出上限、内容不完整**："
            + "、".join(stats["truncated"])
        )
    if stats.get("pt_total"):
        ok, tot = stats["pt_hit"], stats["pt_total"]
        mark = "" if ok >= tot else f" ⚠️ **{tot - ok} 个要点未附引用**"
        lines.append(
            f"> 引用：{ok}/{tot} 个要点可点回原文（正文 "
            f"{stats.get('cites', 0)} 处链接）{mark}"
        )
    if stats.get("num_unknown"):
        lines.append(
            "> ⚠️ 数字核对：以下数字在素材里找不到（疑为改写或编造）："
            + "、".join(stats["num_unknown"])
        )
    lines.append("")
    return lines


# ==================================================================== 什么时候跑
def _as_date(v) -> _date:
    """把 date / datetime 归一成 **date**（幂等）。

    ★★ 为什么必须有这一个函数：`week_end` 会一路传给 `weekly_path()`
      （拼 `weekly-YYYY-MM-DD.md`）和 `collect()`（拼 `daily-YYYY-MM-DD.md`）。
      一旦 datetime 混进来，`isoformat()` 会带上 `T12:38:58.388120+08:00`，
      拼出来的文件名永远对不上 —— **7 天日报全部"不存在"**，周报被判
      「缺历史日报」，还会把已经 done 的记录覆盖成 failed。
      2026-11-01 真实事故：12:00 的 cron 无参调用（拿到 date）生成成功，
      12:21 的 guard 传 `timezone.localtime()`（datetime）→ 判失败并覆盖。

    ★ 放在**入口**归一化，而不是各处 `.date()` 一下：调用方有好几个
      （cron / guard / 手动重跑 / 测试），漏一个就静默错一次。
    """
    if isinstance(v, datetime):
        return timezone.localtime(v).date() if timezone.is_aware(v) else v.date()
    return v


def _due_week_end(now=None) -> _date:
    """「本期周报」的 week_end = 今天（周报在跑的那天就是它所属的那周的最后一天）。

    ★ 返回类型是 **date** 不是 datetime —— `now` 常是 `timezone.localtime()`
      的 datetime，必须先归一再往下传（见 `_as_date` 的事故说明）。
    """
    return _as_date(now) if now is not None else timezone.localdate()


def due_now(now=None) -> bool:
    """今天该不该出周报：配置里的星期 + 时刻已过。"""
    now = now or timezone.localtime()
    if int(getattr(settings, "WEEKLY_WEEKDAY", 6)) != now.weekday():
        return False
    return (now.hour, now.minute) >= (
        int(getattr(settings, "WEEKLY_HOUR", 12)),
        int(getattr(settings, "WEEKLY_MINUTE", 0)),
    )


def retry_deadline(week_end: _date):
    """当天日报还没出来时，最多等到几点（之后判 fail，不再每 30 分钟刷一遍）。"""
    h = int(getattr(settings, "WEEKLY_HOUR", 12))
    m = int(getattr(settings, "WEEKLY_MINUTE", 0))
    start = timezone.make_aware(
        datetime.combine(week_end, datetime.min.time()).replace(hour=h, minute=m)
    )
    return start + timedelta(hours=int(getattr(settings, "WEEKLY_RETRY_HOURS", 2)))


def guard_tick() -> dict:
    """兜底检查（每 SUMMARY_GUARD_MIN 一轮）：该跑就跑，缺当天日报就在窗口内等。

    ★ 只在本进程内串行：`_RUN_LOCK` 与 `run()` 共用，cron 与 guard 撞车时后者让路。
    ★ 缺历史日报 ⇒ 立即 fail 通知（历史日报不会自己出现，等也没用）。
    """
    if not getattr(settings, "WEEKLY_ENABLED", True):
        return {"skipped": "周报未启用"}
    if not sk_enabled():
        return {"skipped": "设置项关闭"}
    now = timezone.localtime()
    if not due_now(now):
        return {"skipped": "未到周报时间"}
    week_end = _due_week_end(now)
    if weekly_path(week_end).is_file():
        return {"skipped": "本周周报已存在"}
    prev = row_of(week_end)
    if prev and prev.state == "failed" and prev.notified:
        # ★ 已经判死并通知过一次 ⇒ 别再每 30 分钟刷一遍（等人手动）
        return {"skipped": "本周已判失败并通知过（等手动重跑）"}
    res = run(week_end)
    if res.get("wait"):
        if timezone.localtime() < retry_deadline(week_end):
            return {"skipped": "等当天日报（窗口内静默重试）", "week_end": week_end}
        # 窗口过了，当天日报还没出来 ⇒ 判死，别再每 30 分钟刷一遍
        return _fail(
            week_end,
            collect(week_end),
            f"当天日报（{week_end.isoformat()}）到 "
            f"{int(getattr(settings, 'WEEKLY_RETRY_HOURS', 2))} 小时重试窗口结束仍缺",
        )
    return res


def reset_stale() -> int:
    """启动即清残留：本机是单进程，启动时任何 summarizing 的周报行都必然是死进程的。

    ★ 用 `all_objects` + kind 过滤：默认 manager 只放行日报，用它一条都扫不到。
    ★ 状态名与日报统一为 `summarizing`（原 `running`）—— 否则 `summary.IN_FLIGHT`
      认不出周报，进程被杀后会永远卡住。
    """
    n = SummaryRun.all_objects.filter(kind=KIND, state="summarizing").update(
        state="failed", error="进程重启，残留重置"
    )
    return n


# ==================================================================== 通知
def notify_weekly_failed(week_end, error: str, *, force: bool = False) -> bool:
    """周报失败通知：同一原因不重复打扰（`notified` 标记）。

    ★ `force=True`：绕过去重重发一次（手动重跑 / 运维想再提醒时用）。
    """
    from core.models import SummaryRun as _SR

    row = _SR.all_objects.filter(kind=KIND, date=week_end).first()
    if row and row.notified and not force:
        return False
    if row:
        row.notified = True
        row.save(update_fields=["notified"])
    if not notify_enabled():
        return False
    return notify.send(
        "谛听 · 周报未生成",
        f"{week_end.isoformat()} 的周报没生成：{error}",
        subtitle="手动重跑：python scripts/run_weekly.py --force",
        # ★ 失败落点给**必然打得开**的总结页（周报页没文件，点开是空的）
        url=_reader_url("/summary?src=weekly-notify"),
        group=f"diting-weekly-{week_end.isoformat()}",
    )


def notify_weekly(week_end, *, stars: int = 0) -> bool:
    """周报生成完成通知。落点是**本地周报页**。"""
    if not notify_enabled():
        return False
    extra = f"｜本周 star {stars} 篇" if stars else ""
    return notify.send(
        "谛听 · 周报已就绪",
        f"过去 7 天的周报已生成{extra}",
        subtitle=week_end.isoformat(),
        url=_reader_url(f"/summary/weekly/{week_end.isoformat()}" "?src=weekly-notify"),
        group=f"diting-weekly-{week_end.isoformat()}",
    )


def _reader_url(path: str) -> str:
    """本地页面绝对 URL（通知点击要用；没配 BASE 就留空，通知本身仍可读）。"""
    base = (getattr(settings, "NOTIFY_READER_BASE", "") or "").rstrip("/")
    return f"{base}{path}" if base else ""


def sk_enabled() -> bool:
    from core import settingkit as sk

    try:
        return bool(sk.effective("weekly_enabled"))
    except Exception:  # noqa: BLE001  设置项还没登记时不该让周报永远跑不起来
        return True


def notify_enabled() -> bool:
    from core import settingkit as sk

    try:
        return bool(sk.effective("notify_weekly"))
    except Exception:  # noqa: BLE001
        return True
