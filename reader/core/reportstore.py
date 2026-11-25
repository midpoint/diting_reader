"""日报文件的存取：路径 / 原子写 / 枚举 / 读取（唯一实现）。

为什么要有这个模块（2026-10-24 抽出）：
  以前多个模块曾各自实现「报告在哪 / 哪天有报告 / 怎么读」，并靠函数内延迟导入
  互相引用（A → B.kick；B → A.read_report）。
  「文件与 SummaryRun 谁是事实源」在各模块里各声明一遍、口径还不一致
  （gaps 以 SummaryRun 行为权威，report_days 以文件为准）。抽出后双方都只依赖这里：

  · **正文的事实源 = 文件**（`data/reports/daily-<date>.md`）—— `SummaryRun`
    只是运行元信息；「哪天有报告」的判据 = 文件存在且非空。
    依据（2026-10-16 教训）：10-12 的 SummaryRun.state="failed"（原子 rename 被
    沙箱文件策略拒绝），但报告文件本身是完整的 40KB —— 按运行记录枚举会
    **静默漏掉它**。推的是文件内容，判据就该跟文件对齐。
"""
import logging
import os
import re
from datetime import date as _date
from pathlib import Path

from django.conf import settings
from django.utils import timezone

log = logging.getLogger(__name__)

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def path(day: _date) -> Path:
    """某日报告的落盘路径（不管文件在不在）。"""
    return Path(settings.REPORT_DIR) / f"daily-{day.isoformat()}.md"


#: 规则版/降级报告的指纹（事故文件里就是这两句，见 2026-11-05）。
_DEGRADED_MARKS = ("（规则版", "规则版 · 未调用模型", "降级原因：")


def looks_degraded(md) -> bool:
    """这份正文是不是「预算耗尽 → 规则版」的降级产物。"""
    text = md or ""
    return any(m in text for m in _DEGRADED_MARKS)


def write(day: _date, md: str, *, degraded=None) -> Path:
    """原子落盘（tmp + os.replace）—— 不会留下半截 md。

    ★★ **降级版绝不覆盖正常版**（2026-11-05 事故的直接成因）：
      当天 4 份正常日报被一次预算耗尽的补跑换成了规则版（137–257 KB、
      无归纳），原件不可恢复。只修隔离只能防「跑错地方」，防不了
      「跑对了地方但产物更差」——后者才是数据丢失那一下。

      · `degraded` 由调用方给（它自己知道这次是不是预算降级）；
        不给（None）时按正文指纹判断，宁可靠内容也不要靠"我记得传了"。
      · 旧文件是正常版而新产物是降级版 ⇒ 新产物写到
        `daily-<date>.degraded-<时间>.md` 留档，**原文件一个字节不动**，
        并记 WARNING（不静默：下次看日志能发现"这天本该更新却没更新"）。
      · 其余情况（旧的不存在 / 旧的就是降级版 / 新的是正常版）照常覆盖。

     周报没有降级分支（`weekly.write_weekly` 只写 LLM 产物），所以不套这层。
    """
    p = path(day)
    p.parent.mkdir(parents=True, exist_ok=True)

    if degraded is None:
        degraded = looks_degraded(md)
    old = read(day)
    if degraded and old and not looks_degraded(old):
        stamp = timezone.now().strftime("%Y%m%d-%H%M%S")
        keep = p.with_name(f"{p.stem}.degraded-{stamp}.md")
        keep.write_text(md, encoding="utf-8")
        log.warning(
            "新报告是降级版（规则版），保留已有正常报告不覆盖：%s（降级版另存 %s）",
            p.name, keep.name)
        return p

    tmp = p.with_suffix(".md.tmp")
    tmp.write_text(md, encoding="utf-8")
    os.replace(tmp, p)
    return p


def read(day: _date):
    """读某日报告正文；文件不存在 / 为空 / 读失败 → None。"""
    p = path(day)
    try:
        if p.is_file() and p.stat().st_size > 0:
            return p.read_text(encoding="utf-8")
    except OSError as exc:  # noqa: BLE001  推送/页面都不该因读文件失败而炸
        log.warning("读报告失败 %s：%s", p, exc)
    return None


def days(since: _date = None, upto: _date = None) -> list:
    """有报告文件的日期（升序）。判据 = 文件在且非空（见模块头）。

    `since` / `upto` 是闭区间边界，None = 不设界。
    """
    root = Path(settings.REPORT_DIR)
    if not root.is_dir():
        return []
    out = []
    for p in sorted(root.glob("daily-*.md")):   # ★ `.md.tmp` 不匹配（原子写残留）
        # ★ 降级留档（`write()` 生成的 `daily-<date>.degraded-<ts>.md`）不是报告：
        #   不排除的话同一天会被枚举成两份（文件名里也含日期，正则照样能匹配）。
        if ".degraded-" in p.name:
            continue
        m = _DATE_RE.search(p.stem)
        if not m:
            continue
        try:
            d = _date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if since and d < since:
            continue
        if upto and d > upto:
            continue
        if p.stat().st_size > 0:
            out.append(d)
    return out


def first_day():
    """最早一份报告的日期；一份都没有 → None。"""
    ds = days()
    return ds[0] if ds else None


def active_since() -> _date:
    """缺口清单的下界：reader 真正开始产出报告的那天。

    ★ 三个候选锚点各有坑（实测踩过，原 summary._active_since 的结论）：
      - EntryRating 最早 rated_at = 分级链路首次跑的那天 → 太晚，会把真产出过
        报告的更早日子误判成缺口；
      - SummaryRun 最早 date → 历史导入时伪造的行会太早，把整个 9 月列成缺口；
      - 报告落盘目录里最早的 .md → 语义最贴切：「已经开始产出报告之后」才谈缺口。
    取不到文件时退回 SummaryRun，再退回 LlmUsage（两者都是「至少动过」的下界）。
    """
    from core.models import LlmUsage, SummaryRun

    d = first_day()
    if d:
        return d
    row = SummaryRun.objects.order_by("date").values_list("date", flat=True).first()
    if row:
        return row
    ts = LlmUsage.objects.order_by("ts").values_list("ts", flat=True).first()
    return timezone.localtime(ts).date() if ts else timezone.localdate()
