"""周期总结页（M7）：日报列表 / 正文查看 / 手动补跑。

报告正文在磁盘（data/reports/daily-YYYY-MM-DD.md），db 只存运行元信息，
所以页面的职责就是这两件事的接缝：列出运行状态 + 渲染那个 md 文件。
"""
import datetime
import logging

from django.conf import settings
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core import clicktrack, marks, mdrender, settingkit as sk, summary
from core import weekly as wk

log = logging.getLogger(__name__)


#: 同一天有日报也有周报时，周报排在上面（"新出的永远在最上"）
_KIND_RANK = {"weekly": 0, "daily": 1}


def _daily_rows(limit: int = 30) -> list:
    """日报行 → 统一行结构。"""
    out = []
    for r in summary.recent_runs(limit):
        items = r["items"]
        size = (f"{items} 条" if items is not None else "— 条")
        size += f" · {r['anomalies']} 异常"
        day = r["date"].isoformat()
        out.append({
            "kind": "daily",
            "kind_label": "日报",
            "date": r["date"],
            "state": r["state"],
            "state_label": r["state_label"],
            "attempt": r["attempt"],
            "catch_up": r["catch_up"],
            "size": size,
            # 口径不同，用 title 说明，别让人拿周报的 61 和日报的 6 直接比
            "size_hint": "窗口内进入总结的条数 · 当天原始异常条数",
            "cost_cny": r["cost_cny"],
            "calls": r["calls"],
            "tokens": r["tokens"],
            "error": r["error"],
            "has_file": r["has_file"],
            "url": f"/summary/{day}",
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            # 表单端点/字段名预先算好，模板里就不用按 kind 分支（分支越少越不容易错）
            "run_url": "/summary/run",
            "run_key": "date",
            "run_value": day,
            "allow_no_cache": True,
        })
    return out


def _weekly_rows(limit: int = 8) -> list:
    """周报行 → 统一行结构。

    ★ 以库为准（`WeeklyRun` 已并入 `SummaryRun`）：**生成失败的周报也在这里**，
      日期不可点（没文件），但「重跑」按钮在 —— 这就是"失败也要看得见"的落点。
    """
    qs = list(wk.rows()[:limit])
    cost = summary.cost_map([r.id for r in qs])
    out = []
    for r in qs:
        c = cost.get(r.id) or {"cost": 0.0, "calls": 0, "tokens": 0}
        need = int(wk.WINDOW_DAYS)
        size = f"{int(r.source_count or 0)}/{need} 日报"
        if r.anomalies_total:
            size += f" · {int(r.anomalies_total)} 异常"
        missing = len(r.missing_json or [])
        if missing:
            size += f"（缺 {missing} 天）"
        day = r.date.isoformat()
        out.append({
            "kind": "weekly",
            "kind_label": "周报",
            "date": r.date,
            "state": r.state,
            "state_label": r.get_state_display(),
            "attempt": int(r.attempt or 0),
            "catch_up": False,
            "size": size,
            "size_hint": (f"{need} 天原始异常之和（跨度 {need} 天，"
                          "不要和单日日报的异常数直接比）"),
            "cost_cny": round(c["cost"], 4),
            "calls": c["calls"],
            "tokens": c["tokens"],
            "error": r.error or "",
            "has_file": wk.weekly_path(r.date).is_file(),
            "url": f"/summary/weekly/{day}",
            "started_at": timezone.localtime(r.started_at) if r.started_at else None,
            "finished_at": timezone.localtime(r.finished_at) if r.finished_at else None,
            "run_url": "/summary/weekly",
            "run_key": "week_end",
            "run_value": day,
            "allow_no_cache": False,      # 周报没有 S3/S2/S1 缓存可忽略
        })
    return out


def _report_rows() -> list:
    """日报 + 周报合成一张表：按「截至日」倒序，同日周报在上。"""
    rows = _daily_rows() + _weekly_rows()
    # ★ 两次稳定排序：先按 kind 排名（周报 0、日报 1），再按日期倒序。
    #   不能写成 sort(key=(date, rank), reverse=True) —— 那会把周报排到最后。
    rows.sort(key=lambda r: _KIND_RANK[r["kind"]])
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


STATES = ("pending", "prefetch", "waiting", "summarizing", "done", "failed")


def _parse_day(value):
    try:
        return datetime.date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def index(request):
    today = summary.due_window_date()
    rows = _report_rows()
    ctx = {
        "rows": rows,
        "ncols": 8,
        "gaps": summary.gaps(),
        "latest": summary.latest_report(),
        "today": today,
        "yesterday": today - datetime.timedelta(days=1),
        "cfg": {
            "hour": sk.effective("summary_hour"),
            "minute": f"{sk.effective('summary_minute'):02d}",
            "window_hour": sk.effective("summary_window_hour"),
            "fresh_deadline": settings.SUMMARY_FRESH_DEADLINE_MIN,
            "fresh_poll": settings.SUMMARY_FRESH_POLL_SEC,
            "refresh_max": settings.SUMMARY_REFRESH_MAX,
            "refresh_workers": settings.SUMMARY_REFRESH_WORKERS,
            "guard": settings.SUMMARY_GUARD_MIN,
            "stale": settings.SUMMARY_STALE_MIN,
            "lookback": settings.SUMMARY_LOOKBACK_DAYS,
            "gap_lookback": settings.SUMMARY_GAP_LOOKBACK_DAYS,
            "max_catchup": settings.SUMMARY_MAX_CATCHUP,
            "s3": settings.SUMMARY_S3_CHARS,
            "s2": settings.SUMMARY_S2_MIN,
            "feed_max": settings.SUMMARY_FEED_MAX_ITEMS,
            "feed_l1_keep": settings.SUMMARY_FEED_L1_KEEP,
            "cache_ttl": settings.SUMMARY_CACHE_TTL_DAYS,
            "max_calls": sk.effective("summary_max_calls"),
            "enabled": sk.effective("summary_enabled"),
        },
        "weekly_cfg": {
            "hour": settings.WEEKLY_HOUR,
            "minute": f"{settings.WEEKLY_MINUTE:02d}",
            "retry_hours": settings.WEEKLY_RETRY_HOURS,
            "enabled": wk.sk_enabled(),
            "weekday_label": ("周一", "周二", "周三", "周四", "周五", "周六", "周日")[
                int(settings.WEEKLY_WEEKDAY) % 7],
        },
    }
    return render(request, "summary/index.html", ctx)


def _marks_ctx(source_id: str) -> dict:
    """日报/周报详情页的划线/批注（wiki 阶段 1，锚定 report 源）。"""
    try:
        from core import marks
        return marks.list_marks("report", source_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("读取划线/批注失败 %s: %s", source_id, exc)
        return {"highlights": [], "annotations": [], "err": ""}


def _annotate(html_str: str, marks: dict, undisplayed: list = None) -> str:
    """正文 HTML 回填 inline 高亮；任何异常都退回原 HTML（高亮是增值，正文是主任务）。

    undisplayed: 收集没能在正文落位的标记 id —— 页面底部只显示这些，
      落位成功的靠 hover 就地改/删，不需要列表。
    """
    try:
        from core import marks as _marks
        return _marks.annotate_html(html_str, marks, undisplayed)
    except Exception as exc:  # noqa: BLE001
        log.warning("回填 inline 高亮失败：%s", exc)
        # 回填整体失败 → 正文里一个都看不见，全部按"未定位"交还给列表兜底
        if undisplayed is not None:
            mark_data = marks or {}
            undisplayed.extend(h["id"] for h in mark_data.get("highlights", []))
            undisplayed.extend(a["id"] for a in mark_data.get("annotations", []))
        return html_str


def _rerun_guard(source_id: str, what: str):
    """重跑前的二次确认：有划线/批注就返回 need_confirm 响应，否则返回 None。

    ★ 为什么拦在视图、不拦在 `summary.run`：重跑是「人的动作」，判据是
      「人有没有被告知」；调度器与自动补跑不走这两个端点，不该被拦。
    ★ 措辞与条数由 `marks.rerun_warning` 一处生成（日报/周报/将来别的报告共用），
      这里只做「有没有 → 拦不拦」。
    """
    warn = marks.rerun_warning("report", source_id, what=what)
    if not warn["total"]:
        return None
    return JsonResponse({
        "ok": False,
        "need_confirm": True,
        "highlights": warn["highlights"],
        "annotations": warn["annotations"],
        "msg": warn["msg"],
        # 老前端认 ok=false 会把 error 弹成 toast —— 给人话，不要留「未知原因」
        "error": f"{what}上有划线/批注，确认后才重跑",
    })


def detail(request, day):
    d = _parse_day(day)
    if d is None:
        raise Http404("日期格式应为 YYYY-MM-DD")
    # 通知深链带 src=daily-notify / daily-fail-notify → 记一次打开（utm 机制的记录端）
    clicktrack.record(request.GET.get("src") or "")
    got = summary.read_report(d)
    if got is None:
        raise Http404("该日期还没有报告")
    marks = _marks_ctx(f"daily-{d.isoformat()}")
    # ★ 只有"正文里落不了位"的标记才留在页面底部（其余靠 hover 就地改/删）
    undisplayed = []
    ctx = {
        "day": d,
        "row": got["row"],
        "path": got["path"],
        # ★ 正文渲染后回填 inline 高亮（选中处原地高亮）
        "body": _annotate(mdrender.render(got["text"]), marks, undisplayed),
        "raw": got["text"],
        "neighbors": _neighbors(d),
        "marks": marks,
        "marks_undisplayed": undisplayed,
        "marks_source_id": f"daily-{d.isoformat()}",
    }
    return render(request, "summary/detail.html", ctx)


def _neighbors(d):
    """前后各一条已生成的报告，便于连续翻看。"""
    out = {}
    prev = (summary.SummaryRun.objects.filter(date__lt=d, state="done")
            .order_by("-date").first())
    nxt = (summary.SummaryRun.objects.filter(date__gt=d, state="done")
           .order_by("date").first())
    if prev:
        out["prev"] = prev.date
    if nxt:
        out["next"] = nxt.date
    return out


@require_POST
def run_day(request):
    """手动跑某一天（后台线程；今天会等 feed 变新鲜，历史日期跳过刷新）。

    ★ 覆盖前二次确认：这天的报告上若有划线/批注，先返回 need_confirm 拦下，
      前端弹「仍然重跑？」拿 confirm=1 再来。判据在 `marks.rerun_warning`
      （措辞与条数一处生成），这里只决定「拦不拦」。
    """
    day = _parse_day(request.POST.get("date")) or summary.due_window_date()
    force = request.POST.get("force") == "1"
    no_cache = request.POST.get("no_cache") == "1"
    if force and request.POST.get("confirm") != "1":
        warn = _rerun_guard(f"daily-{day.isoformat()}", f"{day} 的日报")
        if warn:
            return warn
    # ★ 人点按钮才走到这里（低频），记下 force/no_cache：reqlog 只记路径，
    #   少了这两个参数就无法从日志区分「普通重跑」和「忽略缓存重跑」，
    #   2026-11-17 查 attempt≥2 时只能靠时间戳交叉猜。不要挪到 run() 里
    #   （那是 5 分钟一轮 + 日报编排的热路径）。
    log.info("手动重跑日报 date=%s force=%s no_cache=%s",
             day, int(force), int(no_cache))
    res = summary.trigger_async(day, force=force, no_cache=no_cache)
    return JsonResponse(res)


def weekly_detail(request, week_end):
    """周报正文页（与日报详情页同一套渲染）。"""
    d = _parse_day(week_end)
    if d is None:
        raise Http404("日期格式应为 YYYY-MM-DD")
    clicktrack.record(request.GET.get("src") or "")
    text = wk.read_weekly(d)
    if text is None:
        raise Http404("该周还没有周报")
    sid = f"weekly-{d.isoformat()}"
    marks = _marks_ctx(sid)
    undisplayed = []
    return render(request, "summary/weekly_detail.html", {
        "week_end": d,
        "body": _annotate(mdrender.render(text), marks, undisplayed),
        "raw": text,
        "path": str(wk.weekly_path(d)),
        "marks": marks,
        "marks_undisplayed": undisplayed,
        "marks_source_id": sid,
    })


@require_POST
def weekly_run(request):
    """手动生成/重跑周报（后台线程）。划线/批注与日报同一套覆盖告警。"""
    d = _parse_day(request.POST.get("week_end")) or wk._due_week_end()
    force = request.POST.get("force") == "1"
    if force and request.POST.get("confirm") != "1":
        warn = _rerun_guard(f"weekly-{d.isoformat()}", f"{d} 这一周的周报")
        if warn:
            return warn
    import threading

    def _work():
        try:
            wk.run(d, force=force)
        except Exception as exc:  # noqa: BLE001  线程里抛出会静默丢失
            log.error("手动周报异常 %s：%s", d, exc, exc_info=True)

    threading.Thread(target=_work, name=f"weekly-run-{d}", daemon=True).start()
    return JsonResponse({"ok": True, "week_end": str(d), "force": force})


@require_POST
def catch_up(request):
    return JsonResponse(summary.catch_up_async(limit=settings.SUMMARY_MAX_CATCHUP))
