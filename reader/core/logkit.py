"""日志底座：格式化 / 额外字段 / 脱敏 / 级别约定。

为什么要有这个文件
------------------
`logging` 本身够用，但**默认配置有三个坑**，都实测过（2026-10-16）：

1. `dictConfig` 里 handler 不写 `formatter` → `Formatter` 为 `None` → 输出走
   `logging._defaultFormatter`，**只剩正文**。实测打出来的就是光秃秃的
   `INFO 应该能看见` —— 没有时间、级别、logger 名。排查问题时等于没用。
2. `logging` 的 `extra` **不能和 LogRecord 的内置属性重名**（`message` / `name` /
   `asctime` / `levelname` / `args` …），撞了会直接抛 `KeyError` 让业务代码崩掉。
   所以这里统一规定：结构化字段全部塞进**一个**键 `f`（见 `fields()`），
   formatter 负责把它铺成 `k=v`。
3. 日志里最容易意外带出密钥（`Authorization: Bearer sk-…`、`SESSDATA=…`）。
   `redact()` 在 formatter 出口统一打码 —— 只要走了 handler 就漏不出去。

级别约定（**新增日志请照这个来，别随手挑**）
-------------------------------------------
| 级别      | 什么时候用                                                     | 例子 |
|-----------|----------------------------------------------------------------|------|
| `DEBUG`   | 每次都会发生的高频细节；正常运行时刷屏也不心疼                  | 每次 Miniflux 请求、每批 LLM 调用、每个 HTTP 请求 |
| `INFO`    | **一次状态变化**：任务开始/结束、开关切换、配置生效、启动横幅    | 每轮分级汇总、每个 job 完成、服务启动 |
| `WARNING` | 可恢复的异常/降级：重试、fail-open、预算用尽、慢请求、单源失败   | LLM 重试、源刷新失败、4xx、>800ms 的请求 |
| `ERROR`   | 需要人工介入：链路级失败、未捕获异常、写库失败、5xx              | 总结崩溃、job 抛异常、mark_read 失败 |
| `CRITICAL`| 进程级不可继续（本项目基本用不到）                              | 启动即失败 |

字段命名约定（`/logs` 页面靠它做筛选与展示）
--------------------------------------------
`feed=` feed id｜`entry=` 条目 id｜`cat=` 分类 id｜`run=` 运行标识（日期 / run id）
`ms=` 耗时毫秒｜`cost=` 花费（¥，4 位小数）｜`calls=` 调用次数｜`n=` 计数
`src=` 模块内的子步骤（triage / summary / fastrefresh …）｜`ok=` 布尔｜`err=` 错误摘要

不在日志里重复记账：**花费与条数的唯一事实源是 SQLite**（`LlmUsage` / `SummaryRun`），
日志只负责"什么时候发生了什么事"的时间线。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

# ---------------------------------------------------------------- 行格式（唯一定义处）
# ★ 行格式是**契约**：`apps/logs` 的解析器、verify_logging.py 都按它来。
#   要改就改这里，别在别处另写一份。
DATE_FMT = "%Y-%m-%d %H:%M:%S"
LINE_FMT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)-30s | %(message)s"

#: 结构化字段的分隔符（解析器按它切分「正文」与「字段」）
FIELD_SEP = "  ｜ "

#: 结构化字段统一挂在 LogRecord 的这个属性上（避开内置属性名冲突）
FIELDS_KEY = "f"

#: 单行正文上限：防止某个第三方库吐一整篇 HTML 把日志文件撑爆
MAX_MSG = 4000

#: 字段串上限（头部行太长会把正文挤出屏幕）
MAX_FIELDS = 400


def fields(**kw) -> dict:
    """构造 `extra=` 参数：`log.info("...", extra=fields(feed=29, ms=120))`。

    ★ 只用一个键装字典，避免 `extra={"feed": ...}` 这类写法撞上 LogRecord
      的内置属性（撞了会 `KeyError`，把业务代码打挂）。
    """
    return {FIELDS_KEY: {k: v for k, v in kw.items() if v is not None}}


# ---------------------------------------------------------------- 脱敏
# 只做"明显是密钥"的模式；不做泛化猜测，避免把正常内容打成马赛克。
_REDACT_PATTERNS = [
    (re.compile(r"(?i)\b(sk-[A-Za-z0-9_\-]{6,})"), r"sk-***REDACTED***"),
    # ★ 通用 `secret_` 前缀（很多第三方集成的旧式令牌都长这样）。不含 `key=value`
    #   结构，曾被下面那条通用规则漏掉过，所以单独一条、放最前。
    (re.compile(r"\b((?:ntn|secret)_[A-Za-z0-9]{6,})"), r"***REDACTED***"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{8,}"), r"\1***REDACTED***"),
    (re.compile(r"(?i)\b(authorization\s*[:=]\s*)\S+"), r"\1***REDACTED***"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|api_?key|token|access_token"
                r"|refresh_token|cookie|sessdata)([\"']?\s*[:=]\s*)([^\s,;&\"']{4,})"),
     r"\1\2***REDACTED***"),
]


def redact(text: str) -> str:
    """把疑似密钥/口令打码。formatter 出口统一调用，业务代码不用管。"""
    if not text:
        return text
    for pat, repl in _REDACT_PATTERNS:
        text = pat.sub(repl, text)
    return text


def brief(obj, limit: int = 300) -> str:
    """把任务返回值压成一行摘要（给 job 结束日志用）。

    `{"rated": 3, "llm_calls": 5, "errors": ["a", "b"], "budget_stop": False}`
      → `rated=3 llm_calls=5 budget_stop=0 errors=2项`
    ★ 不做通用 repr：字典里可能有几百条条目 id，直接 str() 会把日志撑爆。
    """
    if obj is None:
        return ""
    if isinstance(obj, (list, tuple, set)):
        return f"{len(obj)}项"
    if isinstance(obj, dict):
        parts = []
        for key, val in obj.items():
            if isinstance(val, (list, tuple, set)):
                if val:
                    parts.append(f"{key}={len(val)}项")
            elif isinstance(val, bool):
                parts.append(f"{key}={'1' if val else '0'}")
            elif isinstance(val, (int, float, str)):
                if val != "":
                    parts.append(f"{key}={_short(val, 60)}")
        text = " ".join(parts)
    else:
        text = _short(obj, limit)
    return text[:limit] + ("…" if len(text) > limit else "")


# ---------------------------------------------------------------- formatter
def _short(value, limit: int = 200) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return f"{value:g}"
    text = str(value)
    if len(text) > limit:
        text = text[:limit] + "…"
    return text.replace("\n", "\\n")


class HumanFormatter(logging.Formatter):
    """人类可读：`时间.毫秒 级别 logger | 正文  ｜ k=v k=v`。

    - 结构化字段铺成 `k=v`，方便直接 grep（`grep 'feed=29'`）
    - ★ 字段固定接在**首行**（不是整个 text 的末尾）：异常栈是多行，
      接末尾会跑到栈的最后一行上去，解析器就认不出这条记录了。
    - 出口统一 `redact()`
    """

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        record_fields = getattr(record, FIELDS_KEY, None)
        if record_fields:
            # ★ None 同样不铺出来：有人绕过 fields() 手塞 extra 时也保持同一约定
            pairs = " ".join(f"{k}={_short(v)}" for k, v in record_fields.items()
                             if v is not None)
            if pairs:
                if len(pairs) > MAX_FIELDS:
                    pairs = pairs[:MAX_FIELDS] + "…"
                head, nl, tail = text.partition("\n")
                text = f"{head}{FIELD_SEP}{pairs}{nl}{tail}"
        if len(text) > MAX_MSG:
            text = text[:MAX_MSG] + f"…（截断，原长 {len(text)}）"
        return redact(text)


class JsonFormatter(logging.Formatter):
    """机器可读（`LOG_FORMAT=json`）。给将来接外部采集留的口子，默认不开。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": f"{self.formatTime(record, DATE_FMT)}.{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(getattr(record, FIELDS_KEY, None) or {})
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


FORMATTERS = {
    "human": {"()": HumanFormatter, "fmt": LINE_FMT, "datefmt": DATE_FMT},
    "json": {"()": JsonFormatter},
}


# ---------------------------------------------------------------- 解析（/logs 页面与验证脚本共用）
_HEAD_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.(?P<ms>\d{3})\s+"
    r"(?P<level>[A-Z]+)\s+(?P<logger>\S+)\s+\|\s?(?P<body>.*)$"
)


def parse_line(line: str) -> Optional[dict]:
    """解析一行日志 → `{ts, level, logger, msg, raw}`；续行返回 None。

    续行（异常栈的第 2 行起）由调用方接到上一条的 `trace` 里。
    """
    m = _HEAD_RE.match(line.rstrip("\n"))
    if not m:
        return None
    body = m.group("body")
    # ★ 按 FIELD_SEP（两空格 + 全角竖线）精确切分：正文里偶尔出现的全角竖线
    #   不会被误当成分隔符（formatter 写入时带的就是这个完整分隔符）。
    msg, sep, tail = body.partition(FIELD_SEP)
    fields_map = {}
    if sep:
        for pair in tail.split():
            k, _, v = pair.partition("=")
            if k:
                fields_map[k] = v
        msg = msg.rstrip()
        tail = tail.strip()
    return {"ts": f"{m.group('ts')}.{m.group('ms')}", "level": m.group("level"),
            "logger": m.group("logger"), "msg": msg, "fields": fields_map,
            "fieldtext": tail, "raw": line.rstrip("\n")}


LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LEVEL_ORDER = {name: i for i, name in enumerate(LEVELS)}

#: 页面上的配色（跟着现有 CSS 变量走，见 /logs 模板）
LEVEL_CLASS = {"DEBUG": "lv-debug", "INFO": "lv-info", "WARNING": "lv-warn",
               "ERROR": "lv-error", "CRITICAL": "lv-critical"}
