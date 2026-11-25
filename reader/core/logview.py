"""日志读取层：把 `data/logs/*.log` 读成结构化记录（给 `/logs` 页面与验证脚本用）。

设计取舍
--------
* **只读尾部**：日志文件会轮转、会长到几 MB，页面不该全量读进内存。
  从文件末尾按 64KB 一块往回读，直到凑够行数 —— 打开页面是 O(窗口) 而不是 O(文件)。
* **计数是"窗口内"的**：级别计数只统计本次扫到的行，页面上如实标注。
  想要精确统计就得全量扫，那是 `grep -c` 的活，不该让页面承担。
* **续行归并**：异常栈的第 2 行起没有时间戳，按"接到上一条"处理；
  窗口开头被截断的半截记录直接丢掉（它的头部不在这段里）。
* 只读现役文件，不读 `.1` `.2` 轮转档 —— 要翻旧账就去 `error.log`（只收 ERROR+）
  或者拿 `data/logs/` 里的文件直接 grep。
"""
from __future__ import annotations

import os
from pathlib import Path

from core import logkit

SCAN_BLOCK = 64 * 1024
#: 页面可选的最大窗口（行）
MAX_WINDOW = 3000
DEFAULT_WINDOW = 400
#: 无论请求多少行，最多往回扫这么多行（防"要 3000 行"把 20MB 文件读穿）
SCAN_MULTIPLIER = 4

FILES = {
    "reader": ("reader.log", "全量（含 DEBUG）"),
    "error": ("error.log", "只收 ERROR 及以上"),
}


def log_dir() -> Path:
    from django.conf import settings
    return Path(settings.LOG_DIR)


def log_files() -> dict:
    """现役日志文件清单（含大小/修改时间），给页面顶部状态条用。"""
    out = {}
    for key, (name, desc) in FILES.items():
        path = log_dir() / name
        info = {"key": key, "name": name, "path": path, "desc": desc,
                "exists": path.exists(), "size": 0, "mtime": None}
        if info["exists"]:
            st = path.stat()
            info["size"] = st.st_size
            info["mtime"] = st.st_mtime
        out[key] = info
    return out


def tail_lines(path: Path, n: int) -> list:
    """从文件末尾取最后 n 行（按块回溯，不整读文件）。"""
    if not path.exists() or n <= 0:
        return []
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        data = b""
        while size > 0 and data.count(b"\n") <= n:
            step = min(SCAN_BLOCK, size)
            size -= step
            fh.seek(size)
            data = fh.read(step) + data
    text = data.decode("utf-8", errors="replace")
    return text.splitlines()[-n:]


#: 页面上的级别缩写 —— 不要用 `|slice:":4"`：DEBUG 会变成 "DEBU"（看着像错字）
LEVEL_SHORT = {"DEBUG": "DBG", "INFO": "INFO", "WARNING": "WARN",
               "ERROR": "ERROR", "CRITICAL": "CRIT"}


def _short_logger(name: str) -> str:
    """`core.datasource.miniflux` → `datasource.miniflux`、`core.triage` → `triage`。

    ★ 规则必须**一致**（都剥最外层包名），否则页面上 `core.reqlog` 和 `reqlog`
      混着出现，扫起来很难受。完整名仍在 title 里，hover 看得到。
    """
    parts = [p for p in (name or "").split(".") if p]
    if len(parts) > 1 and parts[0] in ("core", "django", "apps"):
        parts = parts[1:]
    return ".".join(parts) if parts else (name or "-")


def read_records(kind: str = "reader", window: int = DEFAULT_WINDOW) -> dict:
    """读日志 → `{records, counts, scanned, file}`（记录按**时间正序**）。"""
    window = max(1, min(int(window or DEFAULT_WINDOW), MAX_WINDOW))
    name, desc = FILES.get(kind, FILES["reader"])
    path = log_dir() / name
    scan = window * SCAN_MULTIPLIER
    lines = tail_lines(path, scan)

    records, counts = [], {lv: 0 for lv in logkit.LEVELS}
    for line in lines:
        rec = logkit.parse_line(line)
        if rec is None:
            if records:                     # 续行（异常栈）接到上一条
                records[-1]["trace"].append(line)
            continue                        # 窗口开头的半截记录：丢掉
        rec["trace"] = []
        # 展示用的派生字段（模板保持"只负责摆位置"）
        rec["time"] = rec["ts"][11:]
        rec["short"] = _short_logger(rec["logger"])
        rec["lv_short"] = LEVEL_SHORT.get(rec["level"], rec["level"])
        records.append(rec)
        counts[rec["level"]] = counts.get(rec["level"], 0) + 1

    return {"records": records, "counts": counts, "scanned": len(records),
            "window": window, "file": {"key": kind, "name": name, "desc": desc,
                                       "path": str(path)},
            "truncated": len(lines) >= scan}


def select(records, *, min_level: str = "", logger: str = "", q: str = "",
           limit: int = DEFAULT_WINDOW) -> list:
    """过滤 → 取**最新** limit 条（返回时倒序，最新在前）。"""
    floor = logkit.LEVEL_ORDER.get((min_level or "").upper(), -1)
    want_logger = (logger or "").strip().lower()
    needle = (q or "").strip().lower()

    out = []
    for rec in records:
        if floor >= 0 and logkit.LEVEL_ORDER.get(rec["level"], 0) < floor:
            continue
        if want_logger and want_logger not in rec["logger"].lower():
            continue
        if needle:
            hay = (rec["msg"] + " " + rec.get("fieldtext", "") + " "
                   + rec["logger"] + " " + "\n".join(rec.get("trace") or [])).lower()
            if needle not in hay:
                continue
        out.append(rec)
    out = out[-max(1, int(limit)):]
    out.reverse()
    return out


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"
