"""LLM 客户端 + 开销记账。

HTTP 调用姿势的两条实测经验（沿用自早期原型阶段，原型脚本已随开源清理移出）：
  · 外网 API 用 urllib **默认 opener** —— macOS 会读系统代理（_scproxy），
    用户开了代理工具时外网调用必须走它
  · 本机服务（Miniflux / ollama）必须 build_opener(ProxyHandler({}))，
    否则 localhost 也被丢给上游代理 → 502
"""
import http.client
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import timedelta
from urllib.request import Request

from django.conf import settings

from core.logkit import fields

log = logging.getLogger(__name__)

NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_last_call = [0.0]
THROTTLE = 0.5


def provider_cfg(provider: str = None) -> dict:
    p = (provider or settings.LLM_PROVIDER).lower()
    if p == "deepseek":
        return {"provider": "deepseek",
                "base": settings.DEEPSEEK_BASE_URL,
                "model": settings.DEEPSEEK_MODEL,
                "key": settings.DEEPSEEK_API_KEY}
    return {"provider": "agnes",
            "base": settings.AGNES_BASE_URL,
            "model": settings.AGNES_MODEL,
            "key": settings.AGNES_API_KEY}


def is_peak_now() -> bool:
    """DeepSeek 高峰时段：UTC 周一至周五 01-04、06-10（价格 ×2）。"""
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    return now.weekday() < 5 and (1 <= now.hour < 4 or 6 <= now.hour < 10)


def _throttle():
    if THROTTLE <= 0:
        return
    wait = THROTTLE - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()


def chat(messages, *, provider=None, model=None, temperature=0.1,
         max_tokens=4096, retries=3, timeout=90, tools=None):
    """返回 (content, usage, error, tries)。异常不抛，交给调用方 fail-open。

    ★ tries = 实际发出的传输尝试次数（含成功那次；1 = 一次成功）。
      业务调用方不要直接用本函数 —— 走 costreport.chat_recorded（记账 + 预算闸
      的唯一出口），tries 由它带回账本（LlmUsage.retries）。
    ★ `tools`（2026-11-05 第四步）：给了就并进 payload；响应里的 `tool_calls`
      放进 `usage`（即 norm，方案 A）带出，**不破坏 4 元组**。模型只回
      tool_calls、content 为空是合法的（它想调工具），下面的空判必须跳过它。
    """
    cfg = provider_cfg(provider)
    key = cfg["key"]
    if not key:
        return "", {}, f"缺少 {cfg['provider'].upper()}_API_KEY", 1
    model = model or cfg["model"]
    payload = {"model": model, "messages": messages, "temperature": temperature,
               "stream": False, "max_tokens": max_tokens}
    if tools:
        payload["tools"] = tools

    last = None
    for attempt in range(max(1, retries)):
        t0 = time.monotonic()
        try:
            _throttle()
            req = Request(cfg["base"] + "/chat/completions",
                          data=json.dumps(payload).encode(),
                          headers={"Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.load(r)
            usage = data.get("usage") or {}
            choices = data.get("choices") or []
            choice = choices[0] if choices else {}
            message = choice.get("message") or {}
            content = message.get("content") or ""
            # ★ `tool_calls` 是模型「想调工具」的返回（content 通常为空）。
            #   只回 tool_calls 不是空响应，是**合法的**成功 —— 下面空判必须跳过它，
            #   否则工具调用会被当成失败重试 3 次，loop 永远拿不到工具。
            tool_calls = message.get("tool_calls") or []
            # ★★ 空响应必须当**错误**，不能当成功。
            #    实测 2026-10-15：三次 triage 调用耗时 900 秒、prompt/completion 全 0、
            #    HTTP 200，旧代码走 `data.get("choices") or [{}]` → 拿到空串却零错误，
            #    于是：调用方以为调用成功 → 无有效票 → 30 条落 fail-open，
            #    而开销/健康页显示"0 次失败"。这种"成功"比失败更危险 —— 它静默。
            #    ★ 有 tool_calls 时跳过空判（第四步）：真·空 = 既无 content、
            #      也无 usage、也无 tool_calls。三者全无才是异常。
            if not choices or (not content and not usage and not tool_calls):
                raise ValueError(f"空响应：HTTP 200 但 choices={len(choices)}、"
                                 f"content={len(content)} 字、usage={bool(usage)}、"
                                 f"tool_calls={len(tool_calls)}")
            tries = attempt + 1
            # ★ deepseek-flash / *-reasoner 是**推理模型**：推理链走 reasoning_content，
            #   但它的 token **计入 completion_tokens**，与正式回答共享 max_tokens 预算。
            #   预算给小了 → 推理吃光 → content 可能是空串，且 finish_reason="length"。
            #   这两个字段必须带出来，否则调用方无法区分"模型不愿说"和"被预算截断"。
            norm = {
                "prompt_tokens": int(usage.get("prompt_tokens")
                                     or usage.get("prompt_eval_count") or 0),
                "completion_tokens": int(usage.get("completion_tokens")
                                         or usage.get("eval_count") or 0),
                "cached_tokens": int(usage.get("prompt_cache_hit_tokens") or 0),
                "cache_miss_tokens": int(usage.get("prompt_cache_miss_tokens") or 0),
                "reasoning_tokens": int(
                    (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0),
                "finish_reason": choice.get("finish_reason") or "",
                # ★ 方案 A：tool_calls 随 norm（= 调用方的 usage）带出，不破坏 4 元组。
                #   estimate_cost / record 只读固定 token 键，多这一个键不影响记账。
                "tool_calls": tool_calls,
            }
            # ★ 每次调用一行 DEBUG：单次调用的成本/延迟/是否撞预算截断都在这。
            #   记 INFO 会太吵（一轮分级可能几十次调用）；要看就 /logs 里筛 DEBUG。
            log.debug("llm ok model=%s in=%s out=%s（推理 %s）cache=%s finish=%s %sms",
                      model, norm["prompt_tokens"], norm["completion_tokens"],
                      norm["reasoning_tokens"], norm["cached_tokens"],
                      norm["finish_reason"] or "-", int((time.monotonic() - t0) * 1000),
                      extra=fields(src="llm", model=model,
                                   ms=int((time.monotonic() - t0) * 1000),
                                   tok_in=norm["prompt_tokens"],
                                   tok_out=norm["completion_tokens"]))
            return content, norm, "", tries
        except (urllib.error.HTTPError, urllib.error.URLError, KeyError,
                json.JSONDecodeError, TimeoutError, ValueError,
                # ★ 2026-11-23 实测：上游把 chunked 响应提前断开时 urllib 抛的是
                #   `http.client.IncompleteRead`（HTTPException 子类，**不是** URLError）。
                #   漏掉它 → 3 次重试不执行、chat_recorded 的记账行不写、
                #   scheduler._safe 吞掉异常打断整轮 triage。传输层异常一律进重试。
                http.client.HTTPException) as exc:
            last = exc
            code = getattr(exc, "code", None)
            if code in (401, 403):
                # 认证失败重试无意义，且**必须让人看见**（要改 .env）
                log.error("llm 认证失败 HTTP %s（provider=%s）——重试无意义，检查 .env 里的 key",
                          code, cfg["provider"], extra=fields(src="llm"))
                return "", {}, f"HTTP {code}（认证失败，重试无意义）", attempt + 1
            log.debug("llm 第 %s/%s 次失败：%s", attempt + 1, retries, exc,
                      extra=fields(src="llm"))
            if attempt + 1 < retries:
                time.sleep(2 ** attempt)
    log.warning("llm 调用失败（已试 %s 次，provider=%s model=%s）：%s",
                retries, cfg["provider"], model, last,
                extra=fields(src="llm"))
    return "", {}, f"{last.__class__.__name__}: {last}", max(1, retries)


def chat_stream(messages, *, provider=None, model=None, temperature=0.1,
                max_tokens=4096, timeout=180, on_delta=None, tools=None):
    """流式版 `chat`：边收边把文字增量交给 `on_delta(piece)`，返回同 `chat()` 的
    `(content, usage, error, tries)` 四元组。

    ★ 加法式（2026-11-19）：**不碰 `chat()`** —— 它被几十个调用点与非流式测试桩
      钉死，改动会一次性弄脏整个非流式链路。流式只用于对话，走这里。
    ★★ `stream_options.include_usage=True` 必须开：DeepSeek 的 usage **只在末帧**
      （choices=[] 那一帧）给出，不开就拿不到 token 数 → 记账记 0 → 成本账失真。
      这正是项目最高铁律「每次调用必记账」在流式下最容易破的地方。
    ★ tool_calls 的 arguments 在流里是**分片**给的（同一 index 逐段拼），按 index
      合并后塞进 norm，形状与非流式的 `chat()` 完全一致，调用方无需分叉。
    ★ 传输重试不在这里做：流一旦开始吐字再重来会重复显示。传输失败直接回 err，
      由上层 `citeguard._call_stream` 决定要不要整条重来（此时通常还没吐几个字）。
    """
    cfg = provider_cfg(provider)
    key = cfg["key"]
    if not key:
        return "", {}, f"缺少 {cfg['provider'].upper()}_API_KEY", 1
    model = model or cfg["model"]
    payload = {"model": model, "messages": messages, "temperature": temperature,
               "stream": True, "stream_options": {"include_usage": True},
               "max_tokens": max_tokens}
    if tools:
        payload["tools"] = tools
    _throttle()
    parts, usage, tools_by_index = [], {}, {}
    finish_reason = ""
    try:
        req = Request(cfg["base"] + "/chat/completions",
                      data=json.dumps(payload).encode(),
                      headers={"Authorization": f"Bearer {key}",
                               "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                obj = json.loads(data)
                if obj.get("usage"):
                    usage = obj["usage"]
                choices = obj.get("choices") or []
                if not choices:
                    continue                      # 末帧只有 usage
                delta = choices[0].get("delta") or {}
                if choices[0].get("finish_reason"):
                    finish_reason = choices[0]["finish_reason"]
                piece = delta.get("content") or ""
                if piece:
                    parts.append(piece)
                    if on_delta:
                        on_delta(piece)           # ★ 吐给前端，异常由上层吞
                for tc in delta.get("tool_calls") or []:
                    i = tc.get("index", 0)
                    slot = tools_by_index.setdefault(
                        i, {"id": "", "type": "function",
                            "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]
    except (urllib.error.HTTPError, urllib.error.URLError, KeyError,
            json.JSONDecodeError, TimeoutError, ValueError,
            http.client.HTTPException) as exc:
        code = getattr(exc, "code", None)
        if code in (401, 403):
            log.error("llm 流式认证失败 HTTP %s（重试无意义，检查 .env）", code,
                      extra=fields(src="llm_stream"))
            return "", {}, f"HTTP {code}（认证失败，重试无意义）", 1
        log.warning("llm 流式失败：%s", exc, extra=fields(src="llm_stream"))
        return "", {}, f"{exc.__class__.__name__}: {exc}", 1
    content = "".join(parts)
    tool_calls = [tools_by_index[i] for i in sorted(tools_by_index)]
    if not content and not usage and not tool_calls:
        return "", {}, "流式响应空（无正文/usage/tool_calls）", 1
    norm = {
        "prompt_tokens": int(usage.get("prompt_tokens")
                             or usage.get("prompt_eval_count") or 0),
        "completion_tokens": int(usage.get("completion_tokens")
                                 or usage.get("eval_count") or 0),
        "cached_tokens": int(usage.get("prompt_cache_hit_tokens") or 0),
        "cache_miss_tokens": int(usage.get("prompt_cache_miss_tokens") or 0),
        "reasoning_tokens": int(
            (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0),
        "finish_reason": finish_reason,
        "tool_calls": tool_calls,
    }
    log.debug("llm 流式 ok model=%s out=%s 帧 tool_calls=%s finish=%s",
              model, norm["completion_tokens"], len(tool_calls),
              finish_reason or "-", extra=fields(src="llm_stream", model=model))
    return content, norm, "", 1


def estimate_cost(provider: str, usage: dict):
    """估算 (usd, cny)。只认 deepseek 的价目；其他 provider 返回 (None, None)。"""
    if (provider or "").lower() != "deepseek":
        return None, None
    pr = settings.DEEPSEEK_PRICE
    hit = usage.get("cached_tokens", 0)
    miss = usage.get("cache_miss_tokens") or max(
        usage.get("prompt_tokens", 0) - hit, 0)
    out = usage.get("completion_tokens", 0)
    k = 2.0 if is_peak_now() else 1.0
    usd = (hit * pr["cache_hit"] + miss * pr["cache_miss"] + out * pr["output"]) * k / 1e6
    return usd, usd * settings.USD_CNY


def record(kind, *, provider="", model="", usage=None, retries=0, latency_ms=0,
           ok=True, error="", feed_id=None, category_id=None, entry_id=None,
           summary_run_id=None):
    """写一行 LlmUsage。feed_id / category_id 皆 None = 公共开销。"""
    from core.models import LlmUsage

    usage = usage or {}
    usd, cny = estimate_cost(provider, usage)
    return LlmUsage.objects.create(
        kind=kind, provider=provider, model=model,
        feed_id=feed_id, category_id=category_id, entry_id=entry_id,
        summary_run_id=summary_run_id,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        cached_tokens=int(usage.get("cached_tokens") or 0),
        cost_usd=usd, cost_cny=cny,
        retries=retries, latency_ms=latency_ms, ok=ok, error=(error or "")[:500],
    )


def spent_today_cny(kinds=None, days: int = 1) -> float:
    """近 days 天花费（days=1 即「今日」），这是预算闸与开销页共用的唯一口径。

    ★ `kinds` 非空时按 kind 过滤，用于分池预算。
    ★ 拆池的原因：分级每 5 分钟跑一次、总结一天一次，两者节奏与重要性都不同。
      共用一个池子时，补跑报告会挤掉当天分级（实测踩过）。
    ★ 必须 localtime() 再取零点：USE_TZ 下 now() 是 UTC，直接 .replace(hour=0)
      得到的是 UTC 零点（= 本地早上 8 点）—— 预算会晚 8 小时才翻篇，且与
      costreport.day_start 的本地零点口径不一致（两个口径曾在闸门与页面上各算各的）。
    """
    from django.db.models import Sum
    from django.utils import timezone
    from core.models import LlmUsage

    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    if days and int(days) > 1:
        start -= timedelta(days=int(days) - 1)
    qs = LlmUsage.objects.filter(ts__gte=start)
    if kinds:
        qs = qs.filter(kind__in=list(kinds))
    total = qs.aggregate(s=Sum("cost_cny"))["s"]
    return float(total or 0)
