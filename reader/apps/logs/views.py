"""日志查看页（/logs）。

为什么要有这个页面而不是让你去 `tail -f`
-----------------------------------------
阅读器是**后台进程**（WorkBuddy 里的一个 task），它的 stderr 你根本看不到；
`data/logs/reader.log` 又要开终端。但"现在的状态"这件事，
九成发生在你想起来看一眼的那一刻 —— 所以给它一个网页：
打开就能看到时间线，按级别/模块/关键字筛，每 3 秒自动刷新。

级别筛选的语义
--------------
`lv` 是**阈值**（含以上）：`lv=WARNING` = WARNING + ERROR + CRITICAL。
日常看 INFO+ 就够，出问题切 WARNING+ 或直接看 `file=error`（只收 ERROR+ 的那个文件）。
"""
import datetime
import logging

from django.shortcuts import render

from core import logview

log = logging.getLogger(__name__)

LEVEL_CHOICES = [("", "全部"), ("DEBUG", "DEBUG+"), ("INFO", "INFO+"),
                 ("WARNING", "WARNING+"), ("ERROR", "ERROR+")]
WINDOWS = [200, 400, 1000, 3000]


def _filters(request) -> dict:
    def _int(key, default):
        try:
            return int(request.GET.get(key) or default)
        except (TypeError, ValueError):
            return default

    kind = (request.GET.get("file") or "reader").strip()
    if kind not in logview.FILES:
        kind = "reader"
    # ★ 默认 INFO+：请求日志是 DEBUG（每次都记），默认打开就全是
    #   `GET / → 200` 会把"项目在做什么"淹没。想全要看就点「全部」（URL 变成 lv=）。
    if "lv" not in request.GET:
        lv = "INFO"
    else:
        lv = (request.GET.get("lv") or "").strip().upper()
        if lv and lv not in [c[0] for c in LEVEL_CHOICES]:
            lv = "INFO"
    window = _int("n", logview.DEFAULT_WINDOW)
    if window not in WINDOWS:
        window = logview.DEFAULT_WINDOW
    return {"file": kind, "lv": lv, "logger": (request.GET.get("logger") or "")[:60],
            "q": (request.GET.get("q") or "")[:80], "n": window}


def _context(request) -> dict:
    filters = _filters(request)
    view = logview.read_records(filters["file"], filters["n"])
    rows = logview.select(view["records"], min_level=filters["lv"],
                          logger=filters["logger"], q=filters["q"],
                          limit=filters["n"])
    counts = view["counts"]
    files = logview.log_files()
    for info in files.values():
        info["size_h"] = logview.human_size(info["size"])
        # ★ 时间在视图里格式化好：模板的 `date` 过滤器对 float 时间戳不可靠
        info["mtime_h"] = (datetime.datetime.fromtimestamp(info["mtime"])
                           .strftime("%m-%d %H:%M:%S")) if info["mtime"] else ""
    return {
        "filters": filters,
        "rows": rows,
        "counts": counts,
        # 级别 chip 的 (key, 标签, 本窗口计数) 一次算好，模板不用额外过滤器
        "level_chips": [{"key": key, "label": label, "n": counts.get(key, 0)}
                        for key, label in LEVEL_CHOICES],
        "scanned": view["scanned"],
        "scan_truncated": view["truncated"],
        "cur_file": view["file"],
        "files": files,
        "window_choices": WINDOWS,
        "total": sum(counts.values()),
    }


def index(request):
    # ★ 日志页不需要 feed 树（跟 /costs /summary 一样）
    return render(request, "logs/index.html", _context(request))


def rows(request):
    """htmx 轮询片段：只换表格区，不动筛选栏（否则正在输入的关键字会被刷掉）。"""
    return render(request, "logs/_rows.html", _context(request))
