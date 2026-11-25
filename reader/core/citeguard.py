"""带「引用守卫」的 LLM 调用层（从 summary.py 抽出的公共层）。

★ 为什么独立（2026-10-31）：`_call` / `_call_cited` / `_strip_fence` 原本住在
  `core/summary.py`，`weekly.py` 要复用只能 `summary._call_cited` 跨模块反向
  import（summary 又 import 了周报要用的其它东西，形成隐式耦合）。wiki 编译
  也要复用同一套「记账 + 预算闸 + 引用守卫 + 去围栏」，抽出来让 summary /
  weekly / wiki 三处共用一份，避免双份漂移。

依赖边界（刻意最小）：
  - `costreport.chat_recorded`：记账 + 预算闸的**唯一出口**，业务方不许绕过。
  - `settingkit.effective`：读调用上限等设置（走 15s 缓存）。
  - `reporttext.count_cites`：引用计数（纯函数，无 LLM 依赖）。
  - `jsontool.strip_code_fence`：去 markdown 围栏。

★ 抽层阶段**保持行为不变**：summary.py / weekly.py / verify 脚本对
  `summary._call` 的既有引用一律不改语义（summary 里 re-export 同名）。
"""
import logging

from core import costreport as cr
from core import jsontool
from core import settingkit as sk
from core.reporttext import count_cites

log = logging.getLogger(__name__)


def fresh_stats() -> dict:
    """给 `_call` 用的一次性统计容器（**唯一实现**）。

    ★ 键是 `_call` 的契约：calls / budget_stop / tokens / reasoning_tokens /
      errors / truncated，少一个就在调用点 KeyError。此前对话、提取、lint
      各抄一份 —— 抄五份必然有一份漏键，而失败是「跑到第 N 步才炸」。
    ★ `tool_calls`（2026-11-05 第四步）：`_call` 把本轮模型想调的工具写进这里，
      loop 从 `stats` 读。手写 stats 的调用方（verify 脚本）没这个键也不炸 ——
      `_call` 用下标**赋值**创建它，读取走 `.get()`。
    """
    return {"calls": 0, "budget_stop": False, "tokens": 0,
            "reasoning_tokens": 0, "errors": [], "truncated": [], "tool_calls": []}


def _call(kind, system, user, stats, *, feed_id=None, category_id=None, run_id=None,
          max_tokens=2048, temperature=0.2, tag="", label="", retry_on_empty=True,
          retry_on_truncated=False, reserve=0, pool=None, max_calls=None,
          messages=None, tools=None, entry_id=None):
    """带双闸（单次调用上限 + 池预算）的调用 + 记账。返回 (text, err)。

    ★ 推理模型的坑（实测 deepseek-flash）：推理链 token 计入 completion_tokens，
      与正式回答**共享 max_tokens**。预算不够时推理吃光配额 → content 为空串且
      finish_reason="length" —— 这不是"模型拒绝"，是**被截断**。
      处理：先把预算翻倍重试一次（自愈），仍空才认输。绝不把空串当正常输出。

    ★ `label`：给人看的名字（源名 / 分类名 / 标题）。截断告警里只写
      `S2 feed=37(5000)` 根本看不懂，得带上"是哪个源的哪一层、预算多少"。
    ★ `reserve`：给下游层预留的调用数。下层用 1 给 S0 留一次，避免
      S3/S2/S1 把调用数吃光后整份日报降级成规则版。
    ★ `pool`：默认 `POOL_SUMMARY`。★ 周报必须传 `POOL_WEEKLY`（2026-10-29）：
      它跑在周日 12:00，当天日报已经花过钱，挤在总结池里会撞 `budget_stop`
      ⇒ 周报"没生成、也没报错"。wiki 必须传 `POOL_WIKI`（见 costreport）。
    ★ `max_calls`：单次跑的调用上限；默认 `settings.SUMMARY_MAX_CALLS`。
    ★ `messages`（2026-10-31，对话用）：给了就**原样**当消息体，替代
      `system`/`user` 两条。多轮历史必须这样传，否则每轮只有单条 user，
      模型会反复问「你刚才说的是哪篇」。`system`/`user` 保持必填只为向后兼容；
      传 messages 时它们被忽略。
    ★ `tools`（2026-11-05 第四步）：并排于 `messages` 透传给 `chat_recorded`。
      模型想调工具时（有 tool_calls、content 空）不算「推理吃光预算」，
      翻倍重试只对**既无 content 也无 tool_calls** 的真·空生效；本轮 tool_calls
      写进 `stats["tool_calls"]` 带出给调用方（loop）。
    ★★ `retry_on_truncated`（2026-11-10，S3 用）：`finish_reason=length` 但**有**正文
      时也要翻倍重试。默认 False —— 半截内容对有些调用方仍有用（周报 32k 预算下
      重打一次很贵），但对「输出有硬上限」的调用（S3 只要 100–200 字）半句话就是
      废稿：一批 S3 调用里几次把断在逗号上的片段当成了摘要。
      重试失败（第二次连正文都没了）时用第一次的半截兜底，绝不比不开这个开关更差。
      最终仍截断才写 `stats["truncated"]`，且**只写一次**（写了两次会让报告头重复点名）。
    ★ `entry_id`：只用于记账。此前日报的 `LlmUsage.entry_id` 全是 NULL，
      报告里那句「S3 单篇 …」在成本页对不回具体文章。
    """
    cap = max(0, int(max_calls if max_calls is not None
                     else sk.effective("summary_max_calls"))
              - max(0, int(reserve)))
    if stats["calls"] >= cap:
        return "", "达到单次调用上限"
    if stats["budget_stop"]:
        return "", "日预算已用尽"

    if messages is None:
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user}]

    budget = int(max_tokens)
    last_err = ""
    who = f"{tag}{' · ' + label if label else ''}"
    # ★ 任一条「撞顶可自愈」的支路打开就允许两轮（预算翻倍），但两条支路各管自己：
    #   只开 `retry_on_truncated` 的调用，绝不会因为正文为空就悄悄多花一次钱。
    attempts = 2 if (retry_on_empty or retry_on_truncated) else 1
    #: 撞顶时拿到的半截正文。第二次连正文都没了就用它兜底 —— 开了开关也绝不比
    #: 不开更差（这是「重试」相对「直接用半截」唯一必须守住的底线）。
    partial, partial_budget = "", budget
    for attempt in range(attempts):
        res = cr.chat_recorded(
            kind, messages,
            pool=pool or cr.POOL_SUMMARY, temperature=temperature, max_tokens=budget,
            retries=2, timeout=180,
            feed_id=feed_id, category_id=category_id, summary_run_id=run_id,
            entry_id=entry_id, tools=tools)
        if res.budget_stop:
            stats["budget_stop"] = True
            # ★ 按池名说话：原先写死「日报池」，wiki 对话超限时会 log 成日报池，
            #   排查时把人引到错的池子（与预算卡文案同一个坑）。
            log.warning("%s池预算已用尽（%s）", cr.POOL_LABELS.get(pool or cr.POOL_SUMMARY),
                        tag or "-")
            return "", res.err
        stats["calls"] += 1
        # ★ 本轮模型想调的工具写进 stats 带出给 loop（下标赋值，手写 stats 也能用）。
        #   getattr 兜底：老桩 / 自定义 res 没有 tool_calls 属性时当作空，不炸。
        tool_calls = list(getattr(res, "tool_calls", ()) or ())
        stats["tool_calls"] = tool_calls
        usage = res.usage or {}
        # ★★ 记账在 chat_recorded 里（每次调用必落一行）；stats 的 token 累计紧随
        #   其后 —— 早于下面所有 early-return / continue 分支。
        #   曾经写在「成功返回前」，于是「推理吃光预算、正文为空 → 翻倍重试」的那一次
        #   （真花钱、真消耗 token）没被计入 stats["tokens"]：
        #   实测 run 18 少算 16639（报告头 67091 vs LlmUsage 实际 83730），
        #   连带「含推理 N」的比例虚高（79% → 真实 63%）。
        #   ★ 成本不受影响：chat_recorded 每次调用都写，成本页读 LlmUsage 是准的。
        stats["tokens"] += ((usage.get("prompt_tokens") or 0)
                            + (usage.get("completion_tokens") or 0))
        stats["reasoning_tokens"] += int(usage.get("reasoning_tokens") or 0)
        if res.err:
            stats["errors"].append(f"{who}：{res.err}")
            last_err = res.err
            return "", res.err
        # ★ 「撞顶」= finish_reason=length 且**不是**在等工具调用。模型想调工具
        #   （有 tool_calls、content 空）是合法成功，不是失败，直接把 tool_calls
        #   留在 stats 里交给 loop 执行，绝不翻倍重试。
        if usage.get("finish_reason") != "length" or tool_calls:
            return (res.text or "").strip(), ""
        text = (res.text or "").strip()
        if not text:
            # ① 推理吃光预算、正文一个字没出 → 翻倍再来一次（如果还有下一轮）
            last_err = f"{who}：推理占满 {budget} tokens 预算，正文为空"
            if retry_on_empty and attempt + 1 < attempts:
                budget *= 2
                # ★ 这句日志 2026-11-03 修：原先无论还有没有下一轮都写「翻倍重试（→ N）」，
                #   最后一次照样这么写 —— 于是日志里出现「→ 16384」，而循环只有两轮，
                #   16384 那次**从来没有发生过**。排查时人以为「都试到 16384 了还不行」，
                #   正好是最需要准确的时刻在骗人。只有真会重试时才说重试。
                log.warning("%s，预算翻倍重试（→ %s）", last_err, budget)
                continue
            if partial:
                # 上一轮好歹有半截正文：拿它兜底，别把已经有的一点内容丢回空串
                stats["truncated"].append(f"{who}（预算 {partial_budget} tokens）")
                return partial, ""
            why = "已到重试上限" if attempt + 1 >= attempts else "本条不重试"
            log.error("%s（%s，不再翻倍）", last_err, why)
            break
        # ② 有正文但被硬截断：默认不重试（半截内容也有用、重试更贵），只留证；
        #    输出有硬上限的调用（S3）开 `retry_on_truncated`，翻倍再写一次。
        if retry_on_truncated and attempt + 1 < attempts:
            partial, partial_budget = text, budget
            log.warning("%s：有正文但被切断（%d tokens 用尽），预算翻倍重试（→ %d）",
                        who, budget, budget * 2)
            budget *= 2
            continue
        stats["truncated"].append(f"{who}（预算 {budget} tokens）")
        return text, ""
    stats["errors"].append(last_err or f"{who}：空输出")
    return "", last_err or "空输出"


def _call_stream(kind, system, user, stats, *, on_delta=None, pool=None,
                 max_tokens=2048, temperature=0.2, tag="", label="",
                 messages=None, max_calls=None, tools=None):
    """流式版 `_call`：**闸门 + 统计共用同一套语义，但不做翻倍重试**。

    ★ 加法式（2026-11-19）：不碰 `_call()`（几十个桩与 verify_wiki N 段钉死它的形状）。
    ★ 为什么不重试：`_call` 的「空响应预算翻倍重试 / 截断翻倍重试」在流式下**不能做** ——
      字已经吐给前端，重试一次屏幕上就是两段重复文本。所以流式只给一轮：
      闸下（max_calls / budget_stop）直接返回原因，不花钱；真调一次，空/截断如实回 err
      或半截正文，不静默。这与 `_call` 是**有意的差异**，不是漏实现。
    ★ stats 的每个键都照 `_call` 更新（calls/tokens/budget_stop/errors/tool_calls），
      调用方拿到的统计口径与非流式一致。★ tool_calls 必须带出：审批对话首轮就带
      TOOLS 走流式，`run()` 据此执行 set/merge_into；漏了它就只剩「模型没有返回
      内容」，用户说的「合并」永远不发生（2026-11-22 实测）。
    """
    cap = max(0, int(max_calls if max_calls is not None
                     else sk.effective("summary_max_calls")))
    if stats["calls"] >= cap:
        return "", "达到单次调用上限"
    if stats["budget_stop"]:
        return "", "日预算已用尽"
    if messages is None:
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user}]
    who = f"{tag}{' · ' + label if label else ''}"
    res = cr.chat_recorded_stream(
        kind, messages, pool=pool or cr.POOL_SUMMARY, temperature=temperature,
        max_tokens=int(max_tokens), on_delta=on_delta, tools=tools)
    if res.budget_stop:
        stats["budget_stop"] = True
        log.warning("%s池预算已用尽（流式 %s）",
                    cr.POOL_LABELS.get(pool or cr.POOL_SUMMARY), tag or "-")
        return "", res.err
    stats["calls"] += 1
    # ★ 与非流式 `_call` 同一口径：本轮模型想调的工具写进 stats 带出给 loop。
    #   getattr 兜底：老桩 / 自定义 res 没有 tool_calls 属性时当作空，不炸。
    stats["tool_calls"] = list(getattr(res, "tool_calls", ()) or ())
    usage = res.usage or {}
    stats["tokens"] += ((usage.get("prompt_tokens") or 0)
                        + (usage.get("completion_tokens") or 0))
    stats["reasoning_tokens"] += int(usage.get("reasoning_tokens") or 0)
    if res.err:
        stats["errors"].append(f"{who}：{res.err}")
        return "", res.err
    if usage.get("finish_reason") == "length":
        stats["truncated"].append(f"{who}（流式，预算 {max_tokens} tokens）")
    return (res.text or "").strip(), ""


CITE_RETRY = ("\n\n【上一次的输出漏掉了全部引用，不合格】重新输出：每条要点后必须"
              "原样抄回素材里的 `[源名 · 时分](URL)`。不要改写、不要省略、"
              "不要用 `[#id]` 代替，也不要在段末堆一串引用。")

#: 守卫的失败标记（调用方据此决定「回退原始条目」还是「照用但留痕」）
CITE_MISS = "输出无引用"


def _call_cited(kind, system, user, material, stats, *, tag, label="",
                reject_uncited=True, **kw):
    """「必须有引用」的调用：输出没有引用就**带强约束重试一次**。

    ★★ 为什么需要这道守卫（2026-10-17 实证）：AI 分类那 15 条的汇总
      一条引用都没带，S0 拿着它写不出引用，于是自己在报告里写
      「该段未附引用（实测：素材未提供可引用链接）」—— **把系统的漏接说成素材的问题**。
      引用覆盖率 33/67 看着"部分缺失"，实际是整段失引，被总数掩盖了。
    ★ `reject_uncited=True`（S2/S1）：宁可让上游回退到**原始条目**（它们天然带引用），
      也不要把一段没有出处的汇总喂给下游 —— 无引用的二手概述无法核对。
    ★ `reject_uncited=False`（S0）：整个报告没有引用也比没有报告强，
      所以保留第一版并在头部如实告警。
    """
    text, err = _call(kind, system, user, stats, tag=tag, label=label, **kw)
    if err or not text:
        return "", err
    text = _strip_fence(text)
    if not count_cites(material) or count_cites(text):
        return text, ""
    log.warning("%s 输出没有任何引用，带强约束重试（%s）", tag, label or "-")
    text2, err2 = _call(kind, system, user + CITE_RETRY, stats,
                        tag=tag, label=label, **kw)
    text2 = "" if err2 else _strip_fence(text2)
    if count_cites(text2):
        return text2, ""
    stats.setdefault("cite_missing", []).append(f"{tag}{' · ' + label if label else ''}")
    log.error("%s 重试后仍无引用（%s）", tag, label or "-")
    return ("" if reject_uncited else text), (CITE_MISS if reject_uncited else "")


def _strip_fence(text: str) -> str:
    return jsontool.strip_code_fence(text, "markdown", "md")
