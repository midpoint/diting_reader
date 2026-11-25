#!/usr/bin/env python3
"""日志系统的离线验证（零网络、零 LLM、不动生产日志文件）。

覆盖四层：
  A. 配置契约 —— settings.LOGGING 的三路输出、级别门槛、降噪名单、中间件
  B. 行格式契约 —— logkit 的 round-trip（写出去的行必须能被 /logs 解析回来）
  C. 落盘与读取 —— 临时目录里真写真读（不污染生产日志），logview 的过滤语义
  D. 调用点存在性 —— 关键路径上的日志行不许被"顺手删掉"（静态断言）

用法： python scripts/verify_logging.py
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

from core import logkit, logview  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond, extra=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  <<< {extra!r}")


def read(rel: str) -> str:
    return (Path(__file__).resolve().parent.parent / rel).read_text(encoding="utf-8")


SETTINGS = read("config/settings.py")
SCHED = read("core/scheduler.py")
TRIAGE = read("core/triage.py")
LLM = read("core/llm.py")
MINIFLUX = read("core/datasource/miniflux.py")
SUMMARY = read("core/summary.py")
REQLOG = read("core/reqlog.py")
APPS = read("core/apps.py")
LOGS_VIEWS = read("apps/logs/views.py")
IDX = read("templates/logs/index.html")
ROWS = read("templates/logs/_rows.html")
BASE = read("templates/base.html")

# ==================================================================== A. 配置
print("\n== A. 配置契约（settings.LOGGING）==")
cfg = settings.LOGGING
check("version=1 且不禁用既有 logger（否则第三方 logger 全变哑巴）",
      cfg["version"] == 1 and cfg["disable_existing_loggers"] is False)
check("三路 handler：console / file / errors", {"console", "file", "errors"} <= set(cfg["handlers"]),
      list(cfg["handlers"]))
con, fh, eh = cfg["handlers"]["console"], cfg["handlers"].get("file", {}), cfg["handlers"].get("errors", {})
check("console 门槛 = LOG_LEVEL，file 门槛 = LOG_FILE_LEVEL",
      con["level"] == settings.LOG_LEVEL and fh.get("level") == settings.LOG_FILE_LEVEL,
      [con.get("level"), fh.get("level")])
check("errors 只收 ERROR+（旧账要能翻，且不被 INFO 冲淡）", eh.get("level") == "ERROR")
check("file/errors 都是 RotatingFileHandler + utf-8 + 轮转参数齐",
      "RotatingFileHandler" in fh.get("class", "") and fh.get("encoding") == "utf-8"
      and fh.get("maxBytes", 0) > 0 and fh.get("backupCount", 0) > 0
      and "RotatingFileHandler" in eh.get("class", ""),
      [fh.get("class"), fh.get("maxBytes"), fh.get("backupCount")])
check("日志目录在 data/logs 下且真实存在",
      str(settings.LOG_DIR).endswith("data/logs") and settings.LOG_DIR.exists(),
      settings.LOG_DIR)
check("root 门槛 ≤ 两个 handler 里更低的那个（否则 DEBUG 到不了文件）",
      logging.getLogger().level <= min(logging.getLevelName(settings.LOG_LEVEL),
                                       logging.getLevelName(settings.LOG_FILE_LEVEL),
                                       key=lambda v: v if isinstance(v, int) else 0)
      if False else
      logging.getLogger().level <= min(int(getattr(logging, settings.LOG_LEVEL, 20)),
                                       int(getattr(logging, settings.LOG_FILE_LEVEL, 10))),
      logging.getLevelName(logging.getLogger().level))
noisy = {"django.db.backends", "django.template", "django.utils.autoreload",
         "urllib3", "requests", "apscheduler"}
check("降噪名单钉在 WARNING（尤其 django.db.backends：root 开 DEBUG 会把每条 SQL 写进文件）",
      all(cfg["loggers"].get(n, {}).get("level") == "WARNING" for n in noisy),
      {n: cfg["loggers"].get(n) for n in noisy})
check("django.request 只收 ERROR 且不进根（4xx/慢请求归 reqlog，避免一条请求两行日志）",
      cfg["loggers"]["django.request"]["level"] == "ERROR"
      and cfg["loggers"]["django.request"]["propagate"] is False)
check("django.server 的 access log 关掉（与 reqlog 重复）",
      cfg["loggers"]["django.server"]["propagate"] is False)
check("请求日志中间件在最外层（要量整条链路的耗时）",
      settings.MIDDLEWARE and settings.MIDDLEWARE[0] == "core.reqlog.RequestLogMiddleware",
      settings.MIDDLEWARE[:1])
check("自动轮询端点已排除（否则 /logs 页会把自己的请求写成日志）",
      "/logs/rows" in settings.LOG_IGNORE_PATHS, settings.LOG_IGNORE_PATHS)
check("图标端点已排除（无图标的源每次加载都 404，几十条预期内噪音）",
      "/icon/" in settings.LOG_IGNORE_PATHS)
check("级别环境变量都是合法级别名",
      settings.LOG_LEVEL in logkit.LEVELS and settings.LOG_FILE_LEVEL in logkit.LEVELS,
      [settings.LOG_LEVEL, settings.LOG_FILE_LEVEL])

# ==================================================================== B. 行格式
print("\n== B. 行格式契约（logkit round-trip）==")
check("LINE_FMT 带时间/级别/logger/正文（formatter 为 None 时只剩正文 —— 旧版实测踩过）",
      all(k in logkit.LINE_FMT for k in ("%(asctime)s", "%(levelname)", "%(name)", "%(message)s")),
      logkit.LINE_FMT)

fmt = logkit.HumanFormatter(logkit.LINE_FMT, logkit.DATE_FMT)


def render(record: logging.LogRecord) -> str:
    return fmt.format(record)


r = logging.LogRecord("core.probe", logging.INFO, __file__, 1,
                      "普通消息 %s", ("带参数",), None)
line = render(r)
head = logkit.parse_line(line)
check("无字段消息 round-trip：时间/级别/logger/正文都在",
      head and head["level"] == "INFO" and head["logger"] == "core.probe"
      and head["msg"] == "普通消息 带参数" and re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}$", head["ts"]),
      line)
r = logging.LogRecord("core.probe", logging.WARNING, __file__, 1, "带字段", None, None)
r.f = {"feed": 29, "ms": 41, "ok": True, "none": None}
head = logkit.parse_line(render(r))
check("结构化字段 round-trip：None 被丢、bool 变 0/1、数字原样",
      head and head["fields"] == {"feed": "29", "ms": "41", "ok": "1"},
      head and head["fields"])
try:
    raise RuntimeError("boom-line")
except RuntimeError:
    r = logging.LogRecord("core.probe", logging.ERROR, __file__, 1, "炸了", None, sys.exc_info())
    r.f = {"entry": 123}
    multi = render(r)
head = logkit.parse_line(multi.split("\n")[0])
check("异常记录：首行可解析且字段跟在**首行**（接末尾会跑到栈的最后一行）",
      head and head["msg"] == "炸了" and head["fields"] == {"entry": "123"}
      and "RuntimeError: boom-line" in multi,
      multi.split("\n")[0])
check("异常栈是多行（/logs 把续行归并进 <details>）", multi.count("\n") >= 3, multi.count("\n"))

long_r = logging.LogRecord("core.probe", logging.INFO, __file__, 1, "x" * 9000, None, None)
long_line = render(long_r)
check("超长正文被截断到 MAX_MSG（防第三方库吐整篇 HTML）",
      len(long_line) <= logkit.MAX_MSG + 60 and "截断" in long_line,
      len(long_line))

# 脱敏
secrets = [("sk-abcdef1234567890", "sk-"),
           ("Bearer sk-real-token-123456", "Bearer"),
           ("Authorization: Bearer xyz", "Authorization"),
           ("password=hunter2secret", "password"),
           ("SESSDATA=abc123def456ghi", "SESSDATA"),
           ("api_key=AKIA1234567890", "api_key"),
           ("refresh_token=zzzz12345678", "refresh_token")]
leak = []
for raw, _ in secrets:
    if raw in logkit.redact(f"前缀 {raw} 后缀"):
        leak.append(raw)
check("密钥/口令模式全部打码", not leak, leak)
check("脱敏不误伤普通内容", logkit.redact("分级一轮：待判 12 条，feed=29") == "分级一轮：待判 12 条，feed=29")
check("formatter 出口统一脱敏（不是靠调用方自觉）",
      "abcdef1234567890" not in render(logging.LogRecord(
          "t", logging.ERROR, __file__, 1, "key is sk-abcdef1234567890", None, None)))

j = logkit.JsonFormatter()
out = j.format(logging.LogRecord("core.probe", logging.INFO, __file__, 1, "json 行", None, None))
check("JsonFormatter 输出可解析且字段齐（LOG_FORMAT=json 留的口子）",
      json.loads(out)["level"] == "INFO" and json.loads(out)["logger"] == "core.probe", out)
check("fields() 丢掉 None 值", logkit.fields(a=1, b=None)["f"] == {"a": 1})
check("brief()：dict 压成 k=v、列表只给长度、None 给空串",
      logkit.brief({"rated": 3, "errors": ["a", "b"], "budget_stop": False, "empty": ""})
      == "rated=3 errors=2项 budget_stop=0"
      and logkit.brief(None) == "" and logkit.brief([1, 2, 3]) == "3项",
      logkit.brief({"rated": 3, "errors": ["a", "b"], "budget_stop": False, "empty": ""}))
check("brief() 不做通用 repr（几百个 id 的 dict 不会把日志撑爆）",
      len(logkit.brief({"ids": list(range(500))})) <= 300)

# ==================================================================== C. 落盘与读取
print("\n== C. 落盘与读取（临时目录，不碰生产日志）==")
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td) / "reader.log"
    h = logging.handlers.RotatingFileHandler(tmp, maxBytes=64 * 1024, backupCount=2,
                                             encoding="utf-8", delay=True)
    h.setFormatter(logkit.HumanFormatter(logkit.LINE_FMT, logkit.DATE_FMT))
    lg = logging.getLogger("verify_logging.tmp")
    lg.setLevel(logging.DEBUG)
    lg.addHandler(h)
    lg.propagate = False
    lg.debug("调试细节", extra=logkit.fields(feed=29, ms=12))
    lg.info("状态变化", extra=logkit.fields(src="triage", calls=3))
    lg.warning("可恢复异常")
    lg.error("要人介入", extra=logkit.fields(entry=7))
    h.flush()

    text = tmp.read_text(encoding="utf-8")
    levels = [logkit.parse_line(x)["level"] for x in text.splitlines()
              if logkit.parse_line(x)]
    check("四级都落了盘且按序", levels == ["DEBUG", "INFO", "WARNING", "ERROR"], levels)
    check("DEBUG 行在文件里（文件默认收全量）", "调试细节" in text and "feed=29" in text)

    err_h = logging.handlers.RotatingFileHandler(Path(td) / "error.log", maxBytes=64 * 1024,
                                                 backupCount=1, encoding="utf-8", delay=True)
    err_h.setLevel(logging.ERROR)
    err_h.setFormatter(logkit.HumanFormatter(logkit.LINE_FMT, logkit.DATE_FMT))
    lg2 = logging.getLogger("verify_logging.tmp2")
    lg2.setLevel(logging.DEBUG)
    lg2.addHandler(err_h)
    lg2.propagate = False
    lg2.info("这条不该进 error.log")
    lg2.error("这条该进")
    err_h.flush()
    etext = (Path(td) / "error.log").read_text(encoding="utf-8")
    check("error.log 只收 ERROR+（INFO 不进）", "这条该进" in etext and "这条不该进" not in etext, etext)

    tail = logview.tail_lines(tmp, 3)
    check("tail_lines 取的是**最后** n 行（不是前 n 行）",
          len(tail) == 3 and "要人介入" in tail[-1], tail)

    view = logview.read_records.__wrapped__ if hasattr(logview.read_records, "__wrapped__") else None
    # 用 logview 直接读这个临时文件：monkeypatch 目录，读完全部还原
    real_dir = logview.log_dir
    logview.log_dir = lambda: Path(td)  # type: ignore[assignment]
    try:
        got = logview.read_records("reader", 100)
        check("read_records：4 条记录、计数与记录数一致、正序",
              got["scanned"] == 4 and sum(got["counts"].values()) == 4
              and [x["level"] for x in got["records"]] == ["DEBUG", "INFO", "WARNING", "ERROR"],
              got["scanned"])
        check("read_records：派生字段（time / short / lv_short）已备好",
              got["records"][0]["time"].count(":") == 2
              and got["records"][0]["short"] == "verify_logging.tmp"
              and got["records"][0]["lv_short"] == "DBG",
              got["records"][0])
        rows = logview.select(got["records"], min_level="WARNING")
        check("select(min_level) 是阈值语义（含以上，不是只等于）",
              [x["level"] for x in rows] == ["ERROR", "WARNING"], [x["level"] for x in rows])
        rows = logview.select(got["records"], q="状态变化")
        check("select(q) 命中正文", len(rows) == 1 and rows[0]["msg"] == "状态变化")
        rows = logview.select(got["records"], logger="VERIFY_LOGGING.TMP")
        check("select(logger) 大小写不敏感", len(rows) == 4)
        rows = logview.select(got["records"], limit=2)
        check("select(limit) 取**最新**的且倒序（最新在上）",
              [x["level"] for x in rows] == ["ERROR", "WARNING"],
              [x["level"] for x in rows])
    finally:
        logview.log_dir = real_dir  # type: ignore[assignment]
        lg.removeHandler(h)
        lg2.removeHandler(err_h)
        h.close()
        err_h.close()

check("_short_logger 规则一致（都剥最外层包名）",
      logview._short_logger("core.reqlog") == "reqlog"
      and logview._short_logger("core.datasource.miniflux") == "datasource.miniflux"
      and logview._short_logger("django.request") == "request",
      [logview._short_logger("core.reqlog"), logview._short_logger("core.triage")])

# 生产文件只读检查（服务至少跑过一次才有）
real = logview.log_dir() / "reader.log"
if real.exists():
    got = logview.read_records("reader", 200)
    check("生产 reader.log 可解析（round-trip 在真实文件上也成立）",
          got["scanned"] > 0
          and all(x["level"] in logkit.LEVELS for x in got["records"]),
          got["scanned"])
else:
    check("生产 reader.log 存在（服务至少跑过一次）", False, real)

# ==================================================================== D. 调用点
print("\n== D. 调用点存在性（防被顺手删掉）==")
check("启动横幅：INFO 且带日志路径（排查问题的第一参照物）",
      "_startup_banner" in APPS and 'log.info("谛听阅读器启动' in APPS)
check("scheduler._safe：异常兜底 + DEBUG 计时（无事发生的轮次不打 INFO）",
      "log.error(" in SCHED and 'log.debug("[%s] 完成' in SCHED and "brief(result)" in SCHED)
check("triage：run_once 收尾走 _log_run，INFO 说清判了多少、DEBUG 才是空轮",
      "_log_run(stats)" in TRIAGE and "分级一轮：%s 个源" in TRIAGE
      and "无待判条目" in TRIAGE)
check("triage：无待判走 DEBUG（每 5 分钟一轮，INFO 会变成 288 行/天噪音）",
      'log.debug("分级一轮：无待判条目' in TRIAGE and 'log.info("分级一轮：' in TRIAGE)
check("llm.chat：成功 DEBUG / 认证失败 ERROR / 最终失败 WARNING 三级齐全",
      'log.debug("llm ok' in LLM and "log.error(\"llm 认证失败" in LLM
      and 'log.warning("llm 调用失败' in LLM)
check("miniflux：每次请求 DEBUG（带 ms），失败分级记（404→DEBUG、其余→WARNING）",
      MINIFLUX.count('log.debug("mf GET') == 1
      and "_log_http_fail" in MINIFLUX and "code == 404" in MINIFLUX
      and 'log.warning("mf %s %s 失败' in MINIFLUX)
check("★ miniflux：取状态码没用 `Response and X` 短路写法"
      "（Response.__bool__ = 状态码<400，404 时会返回 Response 本身 —— 实测踩到）",
      'resp = getattr(exc, "response", None)' in MINIFLUX
      and "resp.status_code if resp is not None else None" in MINIFLUX)
check("miniflux：非 JSON 响应单独告警（本机代理回 HTML 502 的老坑）",
      "返回的不是 JSON" in MINIFLUX)
check("summary：开始/完成都有 INFO（含窗口、条目、调用数、耗时）",
      'log.info("总结开始' in SUMMARY and 'log.info("总结完成' in SUMMARY)
check("summary：非致命问题逐条 WARNING（预算用尽 / 截断 / 空合成）",
      'log.warning("总结 %s 的非致命问题' in SUMMARY)
check("reqlog：5xx ERROR / 4xx WARNING / 慢请求 WARNING / 正常走配置级别",
      "status >= 500" in REQLOG and "status >= 400" in REQLOG
      and "ms >= self.slow_ms" in REQLOG and "self.level" in REQLOG)
check("reqlog：未捕获异常记全栈（后台进程没人看 5xx 页面）", "exc_info=True" in REQLOG)
check("/logs 页只读（views 里没有 POST 写库 / 没有调 Miniflux / 没有 LLM）",
      "llm." not in LOGS_VIEWS and "get_datasource" not in LOGS_VIEWS
      and "objects.create" not in LOGS_VIEWS)
check("/logs 的轮询片段不含 rail（换的是表格区，不动筛选栏）",
      "base.html" not in ROWS and "log-row" in ROWS)
check("rail 有日志入口", 'href="/logs"' in BASE)
check("日志页轮询条件里带「页面不可见就不轮询」",
      "visibilityState" in IDX, None)
check("日志页状态条如实标注计数口径（窗口内统计）", "本窗口内" in IDX)

# ==================================================================== 汇总
print("\n== 汇总 ==")
print(f"PASS {PASS} / FAIL {FAIL}")
sys.exit(1 if FAIL else 0)
