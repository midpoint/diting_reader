"""DailySummaryJob（M7）：周期总结的编排、分层压缩、记账与落盘。

三条刻意的设计选择：

1. **窗口 06:00 切**：`[前一日 06:00, 当日 06:00)`，不是自然日。
   07:30 跑，窗口结束在 06:00 —— 这样跨夜的内容归到"同一个晚上"，
   而不是被 00:00 劈成两半。

2. **口径写死 published_at**：`list_entries(published_after=...)` 走服务端过滤。
   既有教训：`published_at` vs `created_at` 在同一天能差 4 倍（53 vs 221），
   混用会让"某分类 0 条"变成彻头彻尾的假结论。

3. **正文只落盘、不进 db**：报告是给人读的 md 文件，db 只存运行元信息
   （状态 / 尝试次数 / 花费 / 异常）。这样报告可以用 git 管起来，
   也不会把 SQLite 撑大。

容错见 §6.4：stale 重置 + SummaryGuardJob 兜底 + 启动扫 7 天补跑。
"""
import hashlib
import logging
import re
import threading
import time
import traceback
from datetime import date as _date
from datetime import datetime, time as dtime, timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from core import costreport as cr
from core import llm, promptspec, settingkit as sk
from core import reportstore, summarycache
from core.citeguard import (  # noqa: F401  抽出的公共层，re-export 保既有引用不变
    CITE_MISS, CITE_RETRY, _call, _call_cited, _strip_fence,
)
from core.datasource import get_datasource, new_datasource
from core.logkit import fields
from core.models import EntryRating, FeedTriageConfig, LlmUsage, SummaryRun
from core.textutil import split_rsshub_quote, strip_boilerplate, to_plain

log = logging.getLogger(__name__)

# 状态里代表「正在进行」
IN_FLIGHT = ("prefetch", "waiting", "summarizing")

# ★ 同日互斥锁（进程内）。见下方 run() 注释：cron 与 guard 会在同一次
#   唤醒里同时进入同一天的 run()，必须串行化"抢占用行"这一段。
_RUN_LOCK = threading.Lock()

# ★ 本进程内活着的 run 线程（date -> threading.Thread，读写都在 _RUN_LOCK 里）。
#   2026-10-25 事故：stale 判定用挂钟（now - started_at > 90min），而
#   wait_until_fresh 的 deadline 用 monotonic —— macOS 睡眠期间挂钟照走、
#   monotonic 停摆，一次完全健康的运行挂钟年龄轻松超 90 分钟，被 guard
#   当崩溃残留"救活"重跑，两条管线并发双烧一轮 LLM。进程内 registry 才是
#   唯一可靠的"活着"判据：线程在就是活的（哪怕它正在睡眠里排队），线程
#   不在才是死的。挂钟年龄只保留给"死了多久"的日志描述用。
_ACTIVE_RUNS: dict = {}


def _run_alive(day) -> bool:
    """该日期在本进程内是否有活线程在跑（registry 的唯一读法）。"""
    owner = _ACTIVE_RUNS.get(day)
    return bool(owner and owner.is_alive()
                and owner is not threading.current_thread())

# ==================================================================== 纯文本层（已抽到 core.reporttext）
# ★ 素材装配 / 引用短名 / 质量审计是纯函数，住在 core.reporttext。
#   这里显式 re-export：模块内调用与 verify 脚本（summary.xxx）的既有引用不变。
from core.reporttext import (  # noqa: F401,E402
    NAME_MAX,
    _CITE_RE, _INFER_RE, _LINK_TARGET, _NAME_RULES, _NUM_RE,
    _RE_REPLY, _RE_REPOST, _RE_RETWEET,
    _cite_coverage, _cut, _name_map, _numbers, _point_coverage,
    _quote, _render_item, _sig, _uncited_categories, _uncovered_note,
    build_items, cap_feed_l1, classify_kind, condense, count_cites, deep_link,
    annotate_quotes, entry_url_map, feed_name, internalize_cites, level_label,
    number_audit,
    space_cites,
)




# ==================================================================== 时间窗口
def window_for(day: _date, hour: int = None):
    """返回该报告日的 (window_start, window_end)，皆为 aware datetime。"""
    hour = sk.effective("summary_window_hour") if hour is None else hour
    tz = timezone.get_current_timezone()
    end = timezone.make_aware(datetime.combine(day, dtime(hour, 0)), tz)
    return end - timedelta(days=1), end


# ★ 报告路径 / 原子写 / 枚举 / 读取统一走 core.reportstore（文件是正文的事实源）。
report_path = reportstore.path


def due_window_date(now=None) -> _date:
    """当前该产出哪一天的报告（= window_end 的日期）。"""
    now = now or timezone.localtime()
    return now.date()


def trigger_at(day: _date) -> datetime:
    """该报告日的编排触发时刻（默认 07:30 本地）。"""
    tz = timezone.get_current_timezone()
    return timezone.make_aware(
        datetime.combine(day, dtime(sk.effective("summary_hour"),
                                    sk.effective("summary_minute"))), tz)


# ==================================================================== 素材采集
def collect_window(start, end, max_entries: int = None) -> list:
    """按 published_at 口径取窗口内条目（分页，单次上限 1000）。"""
    ds = get_datasource()
    limit = int(max_entries or settings.SUMMARY_MAX_ENTRIES)
    out, offset = [], 0
    while len(out) < limit:
        page = ds.list_entries(
            status="all",
            limit=min(1000, limit - len(out)),
            offset=offset,
            published_after=start,
            published_before=end,
        )
        if not page:
            break
        out.extend(page)
        if len(page) < 1000:
            break
        offset += 1000
    out.sort(key=lambda e: e.published_at or e.created_at or timezone.now(), reverse=True)
    return out[:limit]



# ==================================================================== LLM 调用
# ★ `_call` / `_call_cited` / `_strip_fence` / `CITE_RETRY` / `CITE_MISS` 已抽到
#   `core.citeguard`（2026-10-31）：summary / weekly / wiki 三处共用一份。
#   顶部 re-export 保模块内与 verify 脚本的既有引用不变。

#: 调用闸关掉时的统一错误（调用方据此 break，而不是把剩余条目都写成失败）
GATE_ERRORS = {"达到单次调用上限", "日预算已用尽"}


def _cached(layer, scope, payload, stats, call, *, min_len=0, no_cache=False,
            meta=None):
    """缓存优先的调用包装：命中不花钱、不占调用数；失败/截断不写缓存。

    ★ 缓存查询必须在 `_call` 的双闸之前 —— 否则调用上限到了，
      明明有缓存也复用不了。
    ★ key 只由**输入**构造（素材 / 级别 / prompt / 模型）；温度、max_tokens
      等生成参数不进 key —— 改预算不该让已经成功的摘要重跑。
    """
    if not no_cache:
        got = summarycache.get(layer, scope, payload)
        if got is not None:
            stats.setdefault("reused", {}).setdefault(layer, 0)
            stats["reused"][layer] += 1
            return got, "", True
    if stats.get("budget_stop"):
        stats["gate_stop"] = True
        return "", "日预算已用尽", False
    before = len(stats.setdefault("truncated", []))
    text, err = call()
    if err in GATE_ERRORS:
        stats["gate_stop"] = True
    if (text and not err and len(text) >= min_len
            and len(stats["truncated"]) == before):
        summarycache.put(layer, scope, payload, text, meta=meta)
    return text, err, False


S3_SYSTEM = ("你是信息压缩器。只输出摘要正文，不要寒暄、不要标题、不要引用行。"
             "**保留发言归属**：【引文】是引用块，不要默认算作作者本人的判断。")


S3_USER = """把下面这篇文章压缩成 100–200 字的中文摘要。

要求：
1. 只保留【事实、数字、结论、可操作信息】，删掉铺垫、客套、广告、赞助鸣谢。
2. 必须保留所有具体数字（金额、百分比、日期、版本号、标的名）——数字是最高价值信息。
3. 若是教程/技术文，保留"做了什么 + 用什么方法 + 关键结论"三要素。
4. 不要评价，不要补充原文没有的内容。
5. 不要输出引用行（引用由系统统一附加）。
6. **保留发言归属**：`【引文】` 是引用块，不要默认算作作者本人的判断；
   其中「名字:」后是他人发言，不得把提问、引文里的假设写成作者的观点。

<article>
{body}
</article>"""

S2_SYSTEM = ("你是信息汇总助手。按主题合并同源内容，不逐条罗列；"
             "每条要点后带引用；只输出汇总正文，不要前后缀。")

S2_USER = """下面是订阅源「{feed_title}」在 {window} 内的 {n} 条内容（已按规则层压缩）。
请汇总成一段 {k} 字以内的中文概述。

要求：
0. **分级是读者先验**：素材标题后的〔L3〕重点 >〔L2〕关注 >〔L1〕低价值/普通；
   〔未定级·Lx〕按该源默认级别 Lx 理解。L1 是低优先，不等于没信息 —— 同一事件只有 L1
   来源时仍要写，不要因为级别低就删掉。
1. **先判断信息类型**：同一主题的重复/递进（同一事件连续报道、同一话题多次回复）
   合并成一条并标注"共 N 条相关"，不要逐条罗列。
2. **区分谁在说话**：博主本人 / 他人发言 / 转贴。`【引文】` 是引用块，
   里面的主张**不要默认算作博主本人**；其中以「名字:」「@名字」「回复@」开头的
   是跟帖或提问。他人发言里的假设、预测、反问不是博主的判断，不要写成
   「某源认为…」；确需写他人发言时，点名「提问者/被引者认为…」，不要并入
   博主的观点。博主只回答「都有可能」「难说」「不确定」时，
   必须保留这个对冲，不得替他收窄成确定结论。
3. **互动类内容**单独归一类，通常一句话带过，除非含实质观点或数据。
4. 每条要点后带引用 `[源名 · 时分](URL)`（素材里已给，**原样抄回**，不要改写格式，
   不要改回 `[#id]`，也不要省略）。引用必须指向真正支撑这句话的素材，
   不要把相邻主题的 URL 一起挂上来。
5. 输出格式：
   - 主题A：概述（引用）
   - 主题B：概述（引用）
   - 互动/其他：一句话

<entries>
{body}
</entries>"""

S2_MERGE_USER = """下面是同一个订阅源「{feed_title}」在 {window} 内**分 {nb} 批**汇总的结果。
请合并成**一整段** {k} 字以内的中文概述。

要求：
1. **不要保留批次结构**，不要写「第 1 批」「上文提到」这类衔接词 —— 读的人不知道这里分过批。
2. 跨批次重复的主题合并（同一事件在多批里出现时并成一条，可注明"共 N 条相关"）。
3. 保留全部引用 `[源名 · 时分](URL)`（原样抄回，不要改写格式）。
4. 按重要度排序，删掉纯重复和无信息量的互动。

<parts>
{body}
</parts>"""

S1_SYSTEM = ("你是分类主编。按重要度排序、跨源合并同主题，只输出汇总正文。"
             "**合并时必须说清观点是谁的**：不同源对同一件事有不同判断时，"
             "点名到源（如「甲源认为…；乙源则表示…」），不要揉成没有人称的结论。"
             "**引文/跟帖里的假设不是该源的观点**：被引原文、提问者的推测不能写成"
             "「X 认为…」；源只给了「都有可能/难说」这类对冲回答时，保留对冲，"
             "不得升级成确定判断。")

S1_USER = """下面是「{category_title}」分类在 {window} 内各源的汇总结果。请合成 {k} 字以内的一段。

要求：
0. **分级是读者先验**：素材标题后的〔L3〕重点 >〔L2〕关注 >〔L1〕低价值/普通；
   〔未定级·Lx〕按源默认级别 Lx 理解。级别是排序参考，不是硬过滤 —— L1 里的重要内容
   仍要提上来；同一主题只有 L1 来源时也要写。
1. 按【重要度】排序：有数字/结论的 > 有观点的 > 纯资讯 > 纯互动。
2. 跨源重复的主题合并（不同源报道同一事件时合并，并同时附上两边引用）。
3. **每条要点后必须带引用** `[源名 · 时分](URL)`（素材里已给，原样抄回）。
   引用**紧跟**它所支撑的那句话；不要把所有引用堆到段末。
4. **观点要归到人**：素材里每个源名就是发言者。写"X 认为…"而不是"有观点认为…"。
5. 明确指出本分类今天"有没有值得优先读的东西"——没有就直说「本日无高价值内容」。

<sources>
{body}
</sources>"""

S0_SYSTEM = "你是个人情报助理。生成一份 Markdown 日报，只输出报告正文。"

S0_USER = """基于下面的素材生成一份 Markdown 日报。

【硬性要求】
0. **分级是排序先验，不是硬过滤**：L3 重点 > L2 关注 > L1 低价值/普通；
   素材里的〔Lx〕是读者先验，〔未定级·Lx〕按源默认级别 Lx 理解。L1 只是低优先，
   如果内容确实重要仍要提上来；同一件事只有 L1 来源时不能因为级别低就不写。
   每源最多挑 1–2 条重点，别让默认 L3 的源整源刷屏。
1. **结构**：直接从「## 二、按分类内容汇总」开始，接着「## 三、异常告警」、
   「## 四、谛听札记」。
   ★ 不要输出「数量统计」段 —— 那一节由系统按表格与文字行生成并放在你正文前面。
   ★★ **你不需要、也不许解读这些统计**：条目数、占比、集中度、源产出、分类分布
      一律不许在正文任何地方复述或评论（"TOP3 源占 57% 意味着…"这种话不要写）。
      统计是系统给的运维口径，读者不关心，札记只看**情报内容本身**。
   ★ 不要输出一级标题（`# …`），报告标题由系统给。
2. **引用**：每条信息后必须原样带上素材里的 `[源名 · 时分](URL)`。漏引用视为失败。
   引用要**紧跟**它所支撑的那句话；不要把一串引用堆在段末。
   不要自己造引用格式，也不要改成 `[#id]` 或纯文字。
3. **观点必须归到人**：素材里每个源名就是发言者。跨源合并同一主题时，
   要写清"谁说"（如「甲源认为…；乙源补充…」），不要揉成"有观点认为"。
   **【引文】/跟帖/转贴里的主张不要默认归到发言者**，尤其不得把提问写成
   博主的结论；素材里是「或/可能/都有可能」的，不得升级成「才是/一定」；
   引用必须挂在真正支撑该句的素材 URL 上，不能把 A 条的链接挂到 B 条的观点后。
4. **数字必须与素材完全一致**：不得四舍五入、不得改写、不得补出素材里没有的数字。
   素材写 208 家影院就是 208，写成 209 属于编造。
5. **不许给自己找借口**：不要写「素材未提供引用链接」「无法按引用规范展开」这类话。
   每条素材都带引用，没引上只可能是你漏抄。
6. **异常段禁止润色**：状态码、报错原文、小时数必须**原样保留**，
   不要写成"出现了一些问题"这类模糊表述。
7. **区分证据等级**：你推断的内容标「（推断）」；来自素材的标「实测」；
   来自文档的标「记录」。**不要把推断写成事实**。
8. **「## 四、谛听札记」写法**（本段不复述、只判断，350 字以内）：
   - 给**至多 3 件**最值得我关注的事，每件一句话说清「是什么 + 为什么对我重要」
     （重要性按【读者画像】+【分级先验】判，不要按"热度"判），**每件必须带引用**。
   - 最后一句**跨文章才能看出的趋势或矛盾**：单篇看不出来的东西才写
     （同一件事两个源说法打架、一周内反复出现的信号）；属于推测的必须标「（推测）」。
   - **今天若无大事就直说「本日无大事」**，不要凑数、不要硬凑趋势、不要写鸡汤式建议。
   - 本段**不得出现条目数 / 占比 / 集中度等统计数字**，也不得复述上文要点
     （引用例外：点名文章时要带 `[源名 · 时分](URL)`）。
9. 不要写"窗口/库内共 M 个源"这类系统元信息（报告头部已由系统给出），
   也**不要自己写「来源索引」小节** —— 引用就地跟在要点后面即可。
10. **篇幅上限：正文控制在 1800 字以内**。宁缺毋滥 ——
    按分类各写 2–4 条最值得看的，写不完就先写重要的，**绝不为了凑数拉长**。

【窗口】{window}
【条目统计】窗口内 {n} 条来自 {p} 个源（其中 {unrated} 条尚未分级，按该源默认级别处理）
★ 上面这一行是**系统元信息**，只供你判断素材规模；正文任何位置都不要复述它。

【读者画像】（判断"对我重不重要 / 值不值得读"的主要依据；**不是分级判据**；分级是读者先验）
{profile}

【异常上下文】（必须原样写进报告）
{anomalies}

【各分类汇总】
{body}"""


def _prompt_sig(*parts) -> str:
    """prompt 指纹：改了提示词就自动换缓存 key，不用记得手动清缓存。"""
    h = hashlib.sha1()
    for part in parts:
        h.update(str(part).encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


S3_SIG = _prompt_sig(S3_SYSTEM, S3_USER)
S2_SIG = _prompt_sig(S2_SYSTEM, S2_USER)
S2_MERGE_SIG = _prompt_sig(S2_SYSTEM, S2_MERGE_USER)
S1_SIG = _prompt_sig(S1_SYSTEM, S1_USER)


def _model_name() -> str:
    cfg = llm.provider_cfg()
    return f"{cfg.get('provider', '')}/{cfg.get('model', '')}"


# ==================================================================== 分层压缩
def _level_brief(items) -> str:
    """给 S1/S0 的级别分布提示：L3 重点 / L2 关注 / L1 低价值·普通 / 未定级。"""
    counts = {3: 0, 2: 0, 1: 0}
    unrated = 0
    for it in items:
        if not it.get("rated"):
            unrated += 1
        else:
            lv = it.get("level")
            if lv in counts:
                counts[lv] += 1
    parts = [f"L{lv} {counts[lv]}" for lv in (3, 2, 1) if counts[lv]]
    if unrated:
        parts.append(f"未定级 {unrated}")
    return " / ".join(parts) or "无"


def _s3_pass(items, stats, run_id, no_cache=False):
    """超长单篇 → 100–200 字摘要（单独一次调用；输入指纹不变可复用）。"""
    long_items = [it for it in items
                  if it["kind"] == "normal" and it["raw_len"] > settings.SUMMARY_S3_CHARS]
    reserve = int(settings.SUMMARY_S0_RESERVE)
    for it in long_items:
        body = it["text"][:12000]
        # ★ 缓存只认「输入」：正文 + prompt 指纹 + 模型。
        #   温度 / max_tokens 是生成参数，不是外部输入；改预算不该让文章重跑。
        payload = {"mode": "one", "entry_id": it["id"], "text": body,
                   "prompt": S3_SIG, "model": _model_name()}
        text, err, _cached_hit = _cached(
            "s3", it["id"], payload, stats,
            lambda: _call("summary_entry", S3_SYSTEM,
                          S3_USER.format(body=body), stats,
                          feed_id=it["feed_id"], category_id=it["category_id"],
                          run_id=run_id, max_tokens=settings.SUMMARY_S3_MAX_TOKENS,
                          tag=f"S3 单篇 #{it['id']}",
                          label=(it["title"] or "")[:18], reserve=reserve,
                          # ★★ 2026-11-10：S3 的输出有**硬上限**（100–200 字），撞顶几乎
                          #   都是推理链吃配额，所以「翻倍重试」在这里是正解 ——
                          #   与 wiki 分组相反（那里输出随候选条数涨，正解是拆批）。
                          #   不开这个开关的后果实测过：4 次把断在逗号上的半句话当成了
                          #   摘要（11-10 日报 #8346），报告头只能干巴巴告警一句。
                          retry_on_truncated=True,
                          # 记账带上 entry_id：此前全是 NULL，「S3 单篇 #8346」在成本页
                          # 对不回具体文章。
                          entry_id=it["id"]),
            min_len=40, no_cache=no_cache,
            meta={"entry_id": it["id"], "title": (it["title"] or "")[:40]})
        if stats.get("gate_stop"):
            break
        if err or len(text) < 40:
            it["summary"] = f"（S3 压缩失败，保留截断原文）\n{it['text'][:600]}"
            continue
        it["summary"] = _strip_fence(text)[:1200]
        stats["s3"] += 1
    return long_items


def _s2_one(feed_id, cat_id, title, items, stats, run_id, window_label,
            tag_suffix="", no_cache=False):
    """一批条目的按源汇总；失败或空返回 ""（由调用方决定降级）。"""
    k = min(600, 150 * max(1, len(items) // 4))
    material = "\n\n".join(_render_item(it) for it in items)
    payload = {"mode": "one", "feed_id": feed_id, "feed_title": title,
               "window": window_label, "material": material,
               "prompt": S2_SIG, "model": _model_name()}
    text, err, _cached_hit = _cached(
        "s2", feed_id, payload, stats,
        lambda: _call_cited(
            "summary_feed", S2_SYSTEM,
            S2_USER.format(feed_title=title, window=window_label, n=len(items), k=k,
                           body=material),
            material, stats, feed_id=feed_id, category_id=cat_id, run_id=run_id,
            max_tokens=settings.SUMMARY_S2_MAX_TOKENS,
            tag=f"S2 按源 feed={feed_id}{tag_suffix}", label=title,
            reserve=int(settings.SUMMARY_S0_RESERVE)),
        min_len=1, no_cache=no_cache,
        meta={"feed_id": feed_id, "n": len(items), "batch": bool(tag_suffix)})
    if err or not text:
        return ""
    return text


def _s2_merge(feed_id, cat_id, title, parts, stats, run_id, window_label,
              no_cache=False):
    """把同一源的几批汇总合并成一段（批次结构对下游不可见）。"""
    k = min(1200, 500 * len(parts))
    body = "\n\n".join(f"【第 {i} 批】\n{p}" for i, p in enumerate(parts, 1))
    payload = {"mode": "merge", "feed_id": feed_id, "feed_title": title,
               "window": window_label, "material": body,
               "prompt": S2_MERGE_SIG, "model": _model_name()}
    text, err, _cached_hit = _cached(
        "s2", feed_id, payload, stats,
        lambda: _call_cited(
            "summary_feed", S2_SYSTEM,
            S2_MERGE_USER.format(feed_title=title, window=window_label,
                                 nb=len(parts), k=k, body=body),
            "\n\n".join(parts), stats, feed_id=feed_id, category_id=cat_id,
            run_id=run_id, max_tokens=settings.SUMMARY_S2_MAX_TOKENS,
            tag=f"S2 合并 feed={feed_id}", label=title,
            reserve=int(settings.SUMMARY_S0_RESERVE)),
        min_len=1, no_cache=no_cache,
        meta={"feed_id": feed_id, "parts": len(parts), "merge": True})
    if err or not text:
        # ★ 合并失败不退化成空：已有几批直接拼起来，信息不丢（宁可啰嗦一点）。
        #   合并"没引用"也算失败 —— 拼回去的几批本身都带引用，比一段无出处的概述有用。
        return "\n".join(parts)
    return text


def _s2_pass(groups, stats, run_id, window_label, no_cache=False):
    """按源汇总：单源 ≥ SUMMARY_S2_MIN 条才走（源内条目已规则压缩）。

    ★ 单源条目 > SUMMARY_S2_BATCH 时**先分批各自汇总、再合并一次**：
      34 条同源动态一次做完同主题去重/合并，推理链会把预算吃光
      （2026-10-16 实测 feed=29 completion=9999 仍被硬截断，正文只有在分批后才够用）。
      代价：该源的调用数从 1 变成 ceil(n/BATCH)+1，受 SUMMARY_MAX_CALLS 与总结池预算约束。
    ★ 产出契约不变：**一个源仍然只有一段 text**，S1 的输入形状不受影响。
    """
    results = {}
    batch = max(2, int(settings.SUMMARY_S2_BATCH))
    for feed_id, items in groups.items():
        if len(items) < settings.SUMMARY_S2_MIN:
            continue
        title = items[0]["feed_title"]
        cat_id = items[0]["category_id"]
        if len(items) <= batch:
            text = _s2_one(feed_id, cat_id, title, items, stats, run_id,
                           window_label, no_cache=no_cache)
        else:
            log.info("feed %s：%d 条超过分批阈值 %d，先分批汇总再合并",
                     feed_id, len(items), batch)
            chunks = [items[i:i + batch] for i in range(0, len(items), batch)]
            parts = []
            for i, chunk in enumerate(chunks, 1):
                part = _s2_one(feed_id, cat_id, title, chunk, stats, run_id,
                               window_label, tag_suffix=f" #{i}", no_cache=no_cache)
                if part:
                    parts.append(part)
                elif stats.get("gate_stop"):
                    break
            if not parts:
                if stats.get("gate_stop"):
                    break
                continue
            if len(parts) < len(chunks):
                # ★ 必须留痕：只覆盖了一部分，否则读者会以为"这个源今天就这么点内容"。
                stats["errors"].append(
                    f"S2 分批 feed={feed_id}：{len(items)} 条分 {len(chunks)} 批，"
                    f"只完成 {len(parts)} 批（覆盖不全）")
            text = (parts[0] if len(parts) == 1
                    else _s2_merge(feed_id, cat_id, title, parts,
                                   stats, run_id, window_label, no_cache=no_cache))
        if not text:
            if stats.get("gate_stop"):
                break
            continue
        results[feed_id] = {"title": title, "text": text, "items": items}
        stats["s2"] += 1
    return results


def _s1_pass(items, s2, by_cat, stats, run_id, window_label, no_cache=False):
    """按分类汇总：分类内 ≥2 源 或 ≥5 条才走。"""
    results = {}
    for cat_id, cat in by_cat.items():
        cat_items = cat["items"]
        n_src = len({it["feed_id"] for it in cat_items})
        if not (n_src >= settings.SUMMARY_S1_MIN_SOURCES
                or len(cat_items) >= settings.SUMMARY_S1_MIN_ENTRIES):
            continue
        # 输入优先用该分类下各源的 S2 结果；没走 S2 的源用原始条目
        blocks, covered = [], []
        for feed_id, its in cat["feeds"].items():
            brief = _level_brief(its)
            if feed_id in s2:
                blocks.append(f"▶ {s2[feed_id]['title']}（按源汇总｜{brief}）\n"
                              f"{s2[feed_id]['text']}")
                covered.extend(its)
            else:
                blocks.append(f"▶ {its[0]['feed_title']}（{brief}）\n"
                              + "\n\n".join(_render_item(it) for it in its))
                covered.extend(its)
        k = min(900, 300 * max(1, n_src))
        material = "\n\n".join(blocks)
        payload = {"category_id": cat_id, "category_title": cat["title"],
                   "window": window_label, "material": material,
                   "prompt": S1_SIG, "model": _model_name()}
        text, err, _cached_hit = _cached(
            "s1", cat_id, payload, stats,
            lambda: _call_cited(
                "summary_category", S1_SYSTEM,
                S1_USER.format(category_title=cat["title"], window=window_label,
                               k=k, body=material),
                material, stats, feed_id=None, category_id=cat_id, run_id=run_id,
                max_tokens=settings.SUMMARY_S1_MAX_TOKENS,
                tag=f"S1 分类 cat={cat_id}", label=cat["title"],
                reserve=int(settings.SUMMARY_S0_RESERVE)),
            min_len=1, no_cache=no_cache,
            meta={"category_id": cat_id, "n_src": n_src, "n": len(cat_items)})
        if err or not text:
            if stats.get("gate_stop"):
                break
            # ★ 分类汇总没引用 → 不采用它。S0 会退回该分类的**原始条目**
            #   （原始条目每条都带引用），宁可长一点也要可核。
            continue
        results[cat_id] = {"title": cat["title"], "text": text, "items": covered}
        stats["s1"] += 1
    return results


def _s0_pass(items, s1, by_cat, stats, run_id, anomalies, window_label,
             nodes=None, no_cache=True):
    """全局合成（记账 = 公共：不分摊到 feed/分类；**不缓存**）。"""
    blocks = []
    covered = []
    for cat_id, cat in by_cat.items():
        if cat_id in s1:
            blocks.append(f"### {cat['title']}（{_level_brief(cat['items'])}）\n"
                          f"{s1[cat_id]['text']}")
            covered.extend(s1[cat_id]["items"])
        else:
            blocks.append(f"### {cat['title']}（未触发分层，直接给条目｜"
                          f"{_level_brief(cat['items'])}）\n"
                          + "\n\n".join(_render_item(it) for it in cat["items"]))
            covered.extend(cat["items"])
    unrated = sum(1 for it in items if not it["rated"])
    material = "\n\n".join(blocks)
    # ★ 画像从「分级专属」共用过来：札记的「对我重不重要 / 值不值得读」全靠它。
    #   ★ 不再把数量统计喂给 S0 —— 喂了它就会去解读（每期都写"TOP3 源占比过大"），
    #     而统计是系统渲染的运维口径，不该由模型复述或评论（2026-10-27 用户反馈）。
    text, err = _call_cited(
        "summary_global", S0_SYSTEM,
        S0_USER.format(window=window_label, n=len(items),
                       p=len({it["feed_id"] for it in items}),
                       unrated=unrated,
                       profile=promptspec.profile_notes_block(),
                       anomalies="\n".join(anomalies) or "（无）",
                       body=material),
        material, stats, run_id=run_id,
        max_tokens=settings.SUMMARY_S0_MAX_TOKENS, temperature=0.3,
        tag="S0 全局合成", reject_uncited=False, reserve=0)
    if err or not text:
        return "", covered, err or "空输出"
    return text, covered, ""


# ==================================================================== 统计与异常
#: 规则层 kind → 中文名（报告与统计共用一份，别两处各写一遍）
KIND_LABEL = {"normal": "正常", "title_only": "标题流", "quote": "引用转发",
              "reply": "互动回复", "repost": "转贴"}


def _stats_data(items, by_cat, nodes=None) -> dict:
    """算一份真值。**统计不让模型算** —— 它只会照着素材里的数字编。"""
    by_feed = {}
    for it in items:
        name = it.get("feed_name") or f"#{it['feed_id']}"
        by_feed.setdefault(it["feed_id"], [0, name])[0] += 1
    top = sorted(by_feed.items(), key=lambda kv: (-kv[1][0], kv[0]))[:8]
    cats = sorted(by_cat.values(), key=lambda c: (-len(c["items"]), c["title"]))
    kinds = {}
    for it in items:
        kinds[it["kind"]] = kinds.get(it["kind"], 0) + 1
    total = len(items)
    return {
        "total": total,
        "feeds_with": len(by_feed),
        "feeds_all": len(nodes) if nodes is not None else None,
        "cats": [{"title": c["title"], "n": len(c["items"]),
                  "pct": (len(c["items"]) / total * 100 if total else 0)} for c in cats],
        "top": [{"feed_id": fid, "name": v[1], "n": v[0],
                 "pct": (v[0] / total * 100 if total else 0)} for fid, v in top],
        "concentration": (sum(v[0] for _, v in top[:3]) / total * 100 if total else 0),
        "zero_cats": sum(1 for c in cats if not c["items"]),
        "unrated": sum(1 for it in items if not it["rated"]),
        "kinds": kinds,
    }


def _stats_section(items, by_cat, nodes=None) -> str:
    """「一、数量统计」：**系统生成** —— 总览用文字行，明细留两张表。

    ★ 为什么系统生成：原来是让 S0 用散文复述我们给它的数字。
      后果有两个 —— ①「0 产出源数量：0（推断：14 个源均有产出）」这种
      自相矛盾的句子（素材只给了 TOP8，它只能猜）；②同一天的数字
      在不同段落里对不上。数字真值只在这里算一次（`_stats_data`），正文里不许再算。
    ★ 总览块 2026-10-27 由表格改成文字行：那 6 个指标是一句话的事，
      竖表反而把读者视线打断；「按分类」「按源」是多行明细，表格才扫得快 ——
      所以只保留这两张表。
    """
    d = _stats_data(items, by_cat, nodes)
    src_line = (f"有产出源 / 全部源 {d['feeds_with']} / {d['feeds_all']}"
                if d["feeds_all"] is not None else f"有产出源 {d['feeds_with']}")
    dist = " · ".join(f"{KIND_LABEL.get(k, k)} {v}"
                      for k, v in sorted(d["kinds"].items(),
                                         key=lambda kv: -kv[1]))
    L = ["## 一、数量统计", "",
         f"- 总条目 **{d['total']}**；{src_line}；"
         f"分类 {len(d['cats'])} 个（0 产出 {d['zero_cats']}）",
         f"- 集中度（TOP3 源）**{d['concentration']:.0f}%**；"
         f"未分级 {d['unrated']} 条",
         f"- 规则层分布：{dist or '—'}", ""]

    L += ["", "**按分类**", "", "| 分类 | 条数 | 占比 |", "| --- | --- | --- |"]
    for c in d["cats"]:
        L.append(f"| {c['title']} | {c['n']} | {c['pct']:.0f}% |")

    L += ["", f"**按源 TOP{len(d['top'])}**", "",
          "| 源 | #id | 条数 | 占比 |", "| --- | --- | --- | --- |"]
    for f in d["top"]:
        L.append(f"| {f['name']} | #{f['feed_id']} | {f['n']} | {f['pct']:.0f}% |")
    rest = d["total"] - sum(f["n"] for f in d["top"])
    if rest > 0:
        L.append(f"| 其余 {d['feeds_with'] - len(d['top'])} 个源 | — | {rest} | "
                 f"{rest / d['total'] * 100:.0f}% |")
    return "\n".join(L)


def _stats_block(items, by_cat, nodes=None) -> str:
    """给 prompt 的统计文本（与报告同一份真值，避免两套口径）。

    ★ 2026-10-27 起 S0 **不再接收它**：统计是系统渲染的运维口径，喂给模型
      它就会去解读（每期札记都在写"TOP3 源占比过大"）。札记要看的是情报内容。
      保留本函数是因为统计口径只有一处实现，将来若要给别的 prompt 用（比如
      周报）仍应从这里取。
    """
    return _stats_section(items, by_cat, nodes)


def _stale_details(nodes: dict, end) -> list:
    """窗口结束前未成功拉取的源（结构化，供异常段聚合/归因）。"""
    out = []
    for fid, node in sorted(nodes.items()):
        if node.last_success is None or node.last_success < end:
            gap_h = None
            if node.last_success is not None:
                gap_h = max(0.0, (end - node.last_success).total_seconds() / 3600)
            out.append({"feed_id": fid, "title": node.title,
                        "last_success": node.last_success, "gap_h": gap_h,
                        "never": node.last_success is None})
    return out


#: Miniflux 的「瞬时错误」白名单：DNS / 连接被重置 / 超时 / 上游 5xx。
#  ★ 不包含 404/403/410/"not a valid RSS"/解析失败 —— 那些重试也没用，
#    而且可能意味着源真的坏了，不能当噪音静默。
_TRANSIENT_ERROR_RE = re.compile(
    r"no such host|temporary failure in name resolution|"
    r"\bEOF\b|timed? ?out|timeout|context deadline exceeded|"
    r"connection reset|connection refused|broken pipe|"
    r"network is unreachable|no route to host|i/o timeout|"
    r"tls handshake|server misbehaving|"
    r"\b50[0-4]\b|service unavailable|internal server error|bad gateway",
    re.I)


def is_transient_error(message: str) -> bool:
    """错误消息是否属于“重试可能恢复”的网络/瞬时错误。"""
    return bool(message and _TRANSIENT_ERROR_RE.search(message))


def _merge_stale_details(stale: list, failed: list, end, nodes: dict = None) -> list:
    """把「补救刷新失败的窗口缺口」并回 stale 列表。

    ★ 为什么必须单独并：Miniflux 的 `last_success` 是从 `checked_at` 反推的。
      一个原本 `last_success < end` 的源，refresh 失败后 `checked_at` 变成 now、
      `parsing_error_count=1`，反推出来的 `last_success` 会**跳到 end 之后**，
      于是 `_stale_details()` 会误判它“已经新鲜”。失败事实必须覆盖这个启发式。
    """
    out = list(stale or [])
    seen = {x["feed_id"] for x in out}
    for f in failed or []:
        fid = f.get("feed_id")
        if fid in seen:
            continue
        # 如果之后 Miniflux 自己的批次已经把它抓成功（当前 has_error=False），
        # 就不再算未解决。
        node = (nodes or {}).get(fid)
        if node is not None and not node.has_error:
            continue
        ls = f.get("last_success")
        gap_h = None
        if ls is not None:
            gap_h = max(0.0, (end - ls).total_seconds() / 3600)
        out.append({"feed_id": fid, "title": f.get("title", ""),
                    "last_success": ls, "gap_h": gap_h,
                    "never": ls is None, "error": f.get("error", "")})
        seen.add(fid)
    out.sort(key=lambda x: (x["never"], -(x["gap_h"] or 0), x["feed_id"]))
    return out


def _transient_error_details(nodes: dict, end) -> list:
    """窗口已经完整、但最近一次抓取失败且错误可重试的源。

    ★ 这类错误不影响本期日报窗口，**不进日报异常段**；只用于在总结前顺手重试，
      清掉 Miniflux 的 parsing_error_count，避免 UI/后续报告一直挂着假红。
    """
    out = []
    for fid, node in sorted(nodes.items()):
        if not node.has_error:
            continue
        # 窗口内还没成功过的源已经由 _stale_details 覆盖（会走补救刷新）
        if node.last_success is None or node.last_success < end:
            continue
        if is_transient_error(node.error_msg):
            out.append({"feed_id": fid, "title": node.title,
                        "last_success": node.last_success,
                        "error": node.error_msg or ""})
    return out


def _host_sleep_hint(stale: list, end):
    """大量源在同一时刻最后成功 → 宿主休眠/采集停摆，而不是源站同时坏了。

    2026-10-22 实测：46 个源 `last_success` 全是 10-21 20:17（机器 20:27 合盖），
    旧异常段把它渲染成 36 行“距上次成功 9.7h”，模型只能猜。
    """
    if len(stale) < 3:
        return None
    buckets = {}
    for it in stale:
        ls = it.get("last_success")
        if ls is None:
            continue
        # 取 5 分钟桶：同一次 Miniflux 批次内各源完成时间会差几秒到几十秒
        key = ls.replace(minute=(ls.minute // 5) * 5, second=0, microsecond=0)
        buckets.setdefault(key, []).append(it)
    if not buckets:
        return None
    key, group = max(buckets.items(), key=lambda kv: len(kv[1]))
    gap_h = (end - key).total_seconds() / 3600
    if len(group) >= max(3, int(len(stale) * 0.5)) and gap_h >= 2:
        return {"last_success": key, "count": len(group), "gap_h": gap_h}
    return None


def collect_anomalies(nodes: dict, end, stats: dict) -> list:
    """异常段：解析错误 / 宿主停摆 / 补救刷新 / 仍未更新的源。

    ★ 一律用 `last_success()` 推导，**不用 checked_at**（后者是"最后一次尝试"，
      失败也写 —— 直接拿来当"最后成功"会得到乐观的错误结论）。
    ★ 2026-10-22 改造：36 个源同一时刻失败不再逐行刷屏；先归因为“宿主休眠/采集停摆”，
      再按缺口时长分桶列出。
    ★ 2026-10-23 改造：`has_error` 但 `last_success >= end` 属于**窗口后抓取失败**，
      不影响本期数据，**不再进日报**（会被瞬时错误重试处理）；只报真正的窗口缺口。
    ★ 2026-10-24 改造：**只报未解决**。刷新成功/等待补齐/✅ 全部更新都不写；
      只列“仍未拉取”的源、刷新失败/未尝试；重复告警收益为零。
    """
    lines = []
    for fid, node in sorted(nodes.items()):
        # ★ 只报“影响本期窗口”的错误：窗口后最近一次抓取失败是噪音，
        #   尤其唤醒后 DNS 未就绪会出现十几条同时失败；它们会由瞬时错误重试处理。
        if node.has_error and (node.last_success is None or node.last_success < end):
            lines.append(f"- ⚠️ #{fid} {node.title[:30]}：解析错误"
                         f"（{node.error_msg or '无详情'}）")

    fresh = stats.setdefault("freshness", {})
    # ★ 用 run() 合并过“刷新失败”的 after_stale；没有该字段时（preview/旧调用）现算。
    #   不能用 `or`：after_stale=[] 表示“全部解决”，不能被当成缺失。
    stale = fresh.get("after_stale")
    if stale is None:
        stale = _stale_details(nodes, end)

    # ★ 只有仍未解决的 stale 才归因宿主停摆；全部补齐时不再提（用户要“只报未解决”）。
    hint = _host_sleep_hint(stale, end)
    if hint:
        fresh["host_sleep"] = hint
        lines.append(
            f"- 🕒 宿主机/采集疑似停摆：{hint['count']}/{len(nodes)} 个源的最后成功时间"
            f"集中在 {timezone.localtime(hint['last_success']):%m-%d %H:%M}"
            f"（距窗口结束 {hint['gap_h']:.1f}h）。这通常是 Mac 休眠 / Docker 暂停，"
            "不是这些源同时坏了。")

    sched = stats.get("refresh") or {}
    # ★ 只展示“仍未解决”的刷新失败/未尝试；成功刷新与 ✅ 补齐不再写进日报。
    stale_ids = {x["feed_id"] for x in stale}
    failed = [x for x in sched.get("failed", [])
              if not x.get("retry") and x.get("feed_id") in stale_ids]
    stale_skipped = sched.get("stale_skipped", sched.get("skipped", 0))
    if failed or stale_skipped:
        line = "- 🔁 补救刷新未解决"
        if failed:
            line += f"：失败 {len(failed)} 个"
        if stale_skipped:
            line += f"{'，' if failed else '：'}未尝试 {stale_skipped} 个"
        if sched.get("elapsed_sec") is not None:
            line += f"（耗时 {sched['elapsed_sec']:.0f}s）"
        lines.append(line)
        for f in failed[:8]:
            lines.append(f"    - ✗ #{f['feed_id']} {f['title'][:24]}：{f['error']}")
        if len(failed) > 8:
            lines.append(f"    - ✗ …另有 {len(failed) - 8} 个刷新失败")

    if stale:
        # 按缺口时长分桶：不再输出 N 行同质文本；每桶最多点名 8 个源
        buckets = [("从未成功", []), ("≥6h", []), ("2–6h", []), ("<2h", [])]
        for it in stale:
            if it["never"]:
                buckets[0][1].append(it)
            elif it["gap_h"] >= 6:
                buckets[1][1].append(it)
            elif it["gap_h"] >= 2:
                buckets[2][1].append(it)
            else:
                buckets[3][1].append(it)
        lines.append(f"- 窗口结束前仍未成功拉取：{len(stale)} 个源")
        for label, items in buckets:
            if not items:
                continue
            names = "、".join(f"#{x['feed_id']} {x['title'][:18]}" for x in items[:8])
            more = f" 等 {len(items)} 个" if len(items) > 8 else ""
            lines.append(f"    - {label}（{len(items)}）：{names}{more}")
    return lines


# ==================================================================== 补救刷新
def refresh_stale(nodes: dict, end, *, max_feeds: int = None,
                  workers: int = None) -> dict:
    """窗口内未成功拉取的源 + 窗口后瞬时错误 → 走 `PUT /v1/feeds/{id}/refresh`。

    两类候选：
      · stale：`last_success < end` 或 None → **窗口缺口**，必须刷；
      · retry：窗口已完整但最近一次抓取失败（DNS/EOF/timeout/5xx）→ **顺手重试**，
        不影响日报，只清 Miniflux 的 parsing_error_count，避免 UI/后续报告长期假红。

    ★ 有界并发（默认 4），每个 worker 用自己的 `MinifluxDataSource`
      （`requests.Session` 非线程安全）；返回 `stale_*` / `retry_*` 分开计数，
      日报只展示 `stale_*`，`retry_*` 只进日志。
    """
    stale = [n for n in nodes.values() if n.last_success is None or n.last_success < end]
    stale.sort(key=lambda n: n.last_success or datetime.min.replace(tzinfo=dt_timezone.utc))
    stale_ids = {n.id for n in stale}

    retry_pool = []
    if getattr(settings, "SUMMARY_RETRY_TRANSIENT_ERRORS", True):
        for node in nodes.values():
            if node.id in stale_ids or not node.has_error:
                continue
            # 窗口内还没成功过的已经在 stale 里；这里只处理窗口后的瞬时错误
            if node.last_success is None or node.last_success < end:
                continue
            if is_transient_error(node.error_msg):
                retry_pool.append(node)
        retry_pool.sort(key=lambda n: n.id)
        retry_pool = retry_pool[:max(0, int(getattr(
            settings, "SUMMARY_RETRY_TRANSIENT_MAX", 20)))]

    limit = settings.SUMMARY_REFRESH_MAX if max_feeds is None else int(max_feeds)
    stale_targets = stale if limit <= 0 else stale[:limit]
    remaining = None if limit <= 0 else max(0, limit - len(stale_targets))
    retry_targets = retry_pool if remaining is None else retry_pool[:remaining]
    targets = ([(n, False) for n in stale_targets]
               + [(n, True) for n in retry_targets])
    out = {
        "candidates": len(stale) + len(retry_pool),
        "stale_candidates": len(stale), "retry_candidates": len(retry_pool),
        "tried": 0, "ok": 0, "fail": 0,
        "stale_tried": 0, "stale_ok": 0, "stale_fail": 0,
        "retry_tried": 0, "retry_ok": 0, "retry_fail": 0,
        "skipped": (len(stale) - len(stale_targets)
                    + len(retry_pool) - len(retry_targets)),
        "stale_skipped": len(stale) - len(stale_targets),
        "retry_skipped": len(retry_pool) - len(retry_targets),
        "refreshed": [], "failed": [], "elapsed_sec": 0.0,
    }
    if not targets:
        return out

    worker_n = settings.SUMMARY_REFRESH_WORKERS if workers is None else int(workers)
    worker_n = max(1, min(worker_n, len(targets)))
    tls = threading.local()

    def _one(node):
        ds = getattr(tls, "ds", None)
        if ds is None:
            ds = new_datasource()   # 独立 Session，避免并发共用连接池/认证状态
            tls.ds = ds
        ds.refresh_feed(node.id)

    t0 = time.monotonic()
    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=worker_n,
                            thread_name_prefix="summary-refresh") as pool:
        futures = {pool.submit(_one, node): (node, is_retry)
                   for node, is_retry in targets}
        for fut in as_completed(futures):
            node, is_retry = futures[fut]
            kind = "retry" if is_retry else "stale"
            out["tried"] += 1
            out[f"{kind}_tried"] += 1
            try:
                fut.result()
                out["ok"] += 1
                out[f"{kind}_ok"] += 1
                out["refreshed"].append({"feed_id": node.id, "title": node.title,
                                         "retry": is_retry})
            except Exception as exc:  # noqa: BLE001  单个源失败不能毁掉整轮
                out["fail"] += 1
                out[f"{kind}_fail"] += 1
                out["failed"].append({"feed_id": node.id, "title": node.title,
                                      "error": f"{type(exc).__name__}: {exc}",
                                      "retry": is_retry,
                                      "last_success": node.last_success})
                log.warning("补救刷新失败 feed=%s（%s）：%s", node.id,
                            "窗口后重试" if is_retry else "窗口缺口", exc)

    out["elapsed_sec"] = time.monotonic() - t0
    out["refreshed"].sort(key=lambda x: x["feed_id"])
    out["failed"].sort(key=lambda x: x["feed_id"])
    log.info("补救刷新：窗口缺口 %s（尝试 %s 成功 %s 失败 %s，跳过 %s）｜"
             "窗口后瞬时错误 %s（尝试 %s 成功 %s 失败 %s，跳过 %s）"
             "｜并发 %s 耗时 %.0fs",
             out["stale_candidates"], out["stale_tried"], out["stale_ok"],
             out["stale_fail"], out["stale_skipped"],
             out["retry_candidates"], out["retry_tried"], out["retry_ok"],
             out["retry_fail"], out["retry_skipped"],
             worker_n, out["elapsed_sec"])
    return out


# ==================================================================== 报告渲染
def _header(day, start, end, row, stats) -> list:
    lines = [f"# 谛听日报 · {day.isoformat()}", ""]
    lines.append(f"> 窗口 {start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M}（本地时区，按 pubDate）")
    if row.catch_up:
        # ★ 用**这一行记录的实际完成时间**，不是"现在"：手工重建头部（改口径、
        #   补审计）会让"补跑于"漂移到重建那一刻，事后看日志对不上账。
        #   真跑的时候 finished_at 还没写（它在落盘之后才 set），才回退到当前时间。
        ts = timezone.localtime(row.finished_at) if row.finished_at else timezone.localtime()
        lines.append(f"> 补跑于 {ts:%Y-%m-%d %H:%M}（第 {row.attempt} 次尝试）")
    fresh = stats.get("freshness") or {}
    hs = fresh.get("host_sleep")
    if hs:
        lines.append(
            f"> 🕒 数据缺口：{hs['count']} 个源最后成功集中在 "
            f"{timezone.localtime(hs['last_success']):%m-%d %H:%M}，"
            f"距窗口结束 {hs['gap_h']:.1f}h（疑似宿主机休眠/容器暂停）")
    # ★ 只提示未解决的数据缺口；全部补齐时不再写“数据补齐”，减少噪音。
    if fresh.get("stale_after"):
        lines.append(
            f"> ⚠️ 数据不完整：仍有 {fresh['stale_after']} 个源窗口内未成功拉取，"
            "尾段内容可能缺失")
    reuse = stats.get("reused") or {}
    reused_total = sum(int(reuse.get(k, 0)) for k in ("s3", "s2", "s1"))
    lines.append(f"> 生成方式：S3 单篇 {stats['s3']} / S2 按源 {stats['s2']} / "
                 f"S1 按分类 {stats['s1']} / S0 全局 {stats['s0']}"
                 f"（S=Summary 层）｜"
                 f"调用 {stats['calls']} 次｜{stats['tokens']} tokens"
                 + (f"（含推理 {stats['reasoning_tokens']}）" if stats.get("reasoning_tokens") else ""))
    if reused_total:
        lines.append(f"> 缓存：复用 {reused_total} 次（S3 {reuse.get('s3', 0)} / "
                     f"S2 {reuse.get('s2', 0)} / S1 {reuse.get('s1', 0)}；S0 不缓存）")
    if stats.get("no_cache"):
        lines.append("> 本次忽略缓存重跑：S3/S2/S1 全部重新调用（S0 本来就不缓存）。")
    if stats.get("dropped_l1"):
        lines.append(f"> 单源 L1 过多：{stats['dropped_l1']} 条低价值/普通内容未纳入"
                     f"（每源保留最近 {settings.SUMMARY_FEED_L1_KEEP} 条 L1；"
                     "L2/L3 全部保留）")
    if stats.get("truncated"):
        # ★ 推理模型共享预算，被截断必须写在最显眼处，否则读的人会把半截当全文。
        #   ★★ 一行字要能自解释：`S2 feed=37(5000)` 没人看得懂 —— 必须带上
        #      「哪一层 / 哪个源 / 预算多少 / 是截断还是拒答」。
        lines.append("> ⚠️ **以下调用触到输出上限、内容不完整**"
                     "（推理链与正文共享 max_tokens，输出被**硬截断**而非模型拒答）："
                     + "、".join(stats["truncated"]))
    if stats.get("pt_total"):
        # ★ 口径是**要点**不是条目：一条要点概括 N 条素材、只带代表引用是设计
        #   （见 `_point_coverage`）。报「条」会把「合并」误报成「漏引用」。
        ok, tot = stats["pt_hit"], stats["pt_total"]
        detail = f"正文 {stats.get('cites', 0)} 处链接"
        if stats.get("cite_total") and stats["cite_hit"] < stats["cite_total"]:
            # 同一要点合并多条素材时只给代表引用 —— 说明清楚，别让读者以为漏了
            detail += (f"，覆盖 {stats['cite_hit']}/{stats['cite_total']} 条素材"
                       "；合并型摘要不逐条列全，属正常")
        mark = "" if ok >= tot else f" ⚠️ **{tot - ok} 个要点未附引用**"
        lines.append(f"> 引用：{ok}/{tot} 个要点可点回原文（{detail}）{mark}")
    if stats.get("cite_missing"):
        # ★ 与「覆盖率不足」分开写：这里是**汇总环节自己没产出引用**，
        #   系统已回退到原始条目，读者看到的正文可能是"条目清单"而非概述。
        lines.append("> ⚠️ 以下汇总环节未产出引用，已回退为原始条目："
                     + "、".join(stats["cite_missing"]))
    if stats.get("uncited_cats"):
        lines.append("> ⚠️ 正文整段未带引用：" + _uncovered_note(stats["uncited_cats"]))
    if stats.get("num_unknown"):
        lines.append("> ⚠️ 数字核对：以下数字在素材里找不到（疑为改写或编造，"
                     "引用前请核对原文）：" + "、".join(stats["num_unknown"]))
    lines.append("")
    return lines


def _strip_autogen(body: str) -> str:
    """删掉 S0 自作主张加的一级标题与「数量统计」段。

    ★ 这两样现在都由系统生成（标题在 `_header`，统计在 `_stats_section`）。
      模型即使被明确要求"不要输出"，也仍会照着旧习惯写一遍 ——
      所以**不靠提示词，靠删**：标题删首行，统计段删到下一个二级标题为止。
    """
    lines = (body or "").split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and re.match(r"^#\s+\S", lines[i]):
        lines = lines[i + 1:]

    out, skipping = [], False
    for ln in lines:
        if re.match(r"^#{1,3}\s*[一二三四五六七八九十0-9、. 　]*数量统计", ln):
            skipping = True
            continue
        if skipping:
            if re.match(r"^##\s+\S", ln):     # 下一个二级标题 → 恢复
                skipping = False
            else:
                continue
        out.append(ln)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()



def render_rule_report(day, start, end, row, items, by_cat, anomalies, stats,
                       nodes=None) -> str:
    """降级报告：不调 LLM 也能出一份可读、可追溯的日报。

    触发场景：预算用尽 / 模型全挂 / 显式 `--no-llm`。核心保证是
    **统计与异常一定在**，内容部分退化为按分类的条目清单。
    """
    L = _header(day, start, end, row, stats)
    L += [_stats_section(items, by_cat, nodes), ""]
    L += ["## 二、异常告警", "", *(anomalies or ["- （无异常）"]), ""]
    L += ["## 三、内容（规则版 · 未调用模型）", ""]
    for cat in sorted(by_cat.values(), key=lambda c: -len(c["items"])):
        if not cat["items"]:
            continue
        L += [f"### {cat['title']}（{len(cat['items'])} 条）", ""]
        for it in cat["items"]:
            L.append(f"- {_render_item(it)}")
        L.append("")
    return "\n".join(L)


# ==================================================================== 主流程
def _meta_index():
    """{feed_id: {title, category_id, category_title, default_level}} + 侧栏用树。

    ★ 未评级条目的级别按该 feed 的 default_level 算（缺省全局 L2）——
      不再在 build_items 里写死 L2，否则「默认 L1 的低价值源」会被误抬成关注。
    """
    meta, tree = {}, []
    try:
        tree = get_datasource().list_tree()
    except Exception as exc:  # noqa: BLE001
        log.warning("取树失败（总结仍可跑，源名会退化为 #id）：%s", exc)
    defaults = {r.feed_id: r.default_level
                for r in FeedTriageConfig.objects.all()
                if r.default_level is not None}
    for cat in tree:
        for f in cat.feeds:
            meta[f.id] = {"title": f.title, "category_id": cat.id,
                          "category_title": cat.title, "node": f,
                          "default_level": defaults.get(f.id,
                                                        settings.UNRATED_LEVEL)}
    return meta, tree


def _nodes_of(tree) -> dict:
    return {f.id: f for cat in tree for f in cat.feeds}


def preview(day: _date = None, *, refresh: bool = False) -> dict:
    """只算不写（预览）：不建 SummaryRun、不落盘、**不调 LLM**。

    用途是上线前自检（"窗口里有多少条、规则层能压掉多少、哪些源是异常"），
    所以刻意连补救刷新都不做 —— 一个自检动作不该对外部系统产生副作用。
    """
    day = day or due_window_date()
    start, end = window_for(day)
    meta, tree = _meta_index()
    nodes = _nodes_of(tree)
    stats = {"refresh": refresh_stale(nodes, end) if refresh else {}}
    anomalies = collect_anomalies(nodes, end, stats)
    entries = collect_window(start, end)
    ratings = {r.entry_id: r for r in EntryRating.objects.filter(
        entry_id__in=[e.id for e in entries])}
    items = build_items(entries, ratings, meta)
    dropped_l0 = sum(
        1 for e in entries
        if ((ratings.get(e.id) is not None and ratings[e.id].level == 0)
            or (ratings.get(e.id) is None
                and (meta.get(e.feed_id) or {}).get(
                    "default_level", settings.UNRATED_LEVEL) == 0)))
    items, dropped_l1 = cap_feed_l1(items, settings.SUMMARY_FEED_MAX_ITEMS,
                                    settings.SUMMARY_FEED_L1_KEEP)
    kinds = {}
    for it in items:
        kinds[it["kind"]] = kinds.get(it["kind"], 0) + 1
    by_feed = {}
    for it in items:
        by_feed.setdefault(it["feed_id"], []).append(it)
    by_cat = {}
    for it in items:
        by_cat.setdefault(it["category_title"], []).append(it)
    return {
        "date": str(day), "dry_run": True,
        "window": [start.isoformat(), end.isoformat()],
        "scanned": len(entries), "leads": len(items),
        "dropped_l0": dropped_l0, "dropped_l1": dropped_l1,
        "unrated": sum(1 for it in items if not it["rated"]),
        "kinds": kinds, "anomalies": anomalies,
        "s3_candidates": [it["id"] for it in items
                          if it["kind"] == "normal"
                          and it["raw_len"] > settings.SUMMARY_S3_CHARS],
        "s2_sources": {str(f): len(v) for f, v in by_feed.items()
                       if len(v) >= settings.SUMMARY_S2_MIN},
        "s1_categories": {k: len(v) for k, v in by_cat.items()
                          if len({i["feed_id"] for i in v}) >= settings.SUMMARY_S1_MIN_SOURCES
                          or len(v) >= settings.SUMMARY_S1_MIN_ENTRIES},
    }


def wait_until_fresh(end, *, deadline_min: int = None, poll_sec: int = None,
                     fallback_nodes: dict = None) -> dict:
    """轮询 feed 健康状态，直到窗口结束前都已成功拉取，或到达 deadline。

    ★ 为什么不是 `sleep(15min)`：Miniflux 的全局轮询是 60 分钟开一次闸，
      休眠唤醒后下一批可能在任何时刻（2026-10-22 是唤醒后 21 分钟）。
      固定 sleep 要么白等、要么等不到；这里每 `poll_sec` 复检一次，批次一到就走。
    ★ `deadline_min=0` 仍会做一次立即复检（用于手动补跑不想等的情况）。
    """
    deadline_min = (settings.SUMMARY_FRESH_DEADLINE_MIN if deadline_min is None
                    else max(0, int(deadline_min)))
    poll_sec = (settings.SUMMARY_FRESH_POLL_SEC if poll_sec is None
                else max(1, int(poll_sec)))
    t0 = time.monotonic()
    deadline = t0 + deadline_min * 60
    checks = 0
    last_log_t = 0.0
    last_count = None

    meta, tree = _meta_index()
    nodes = _nodes_of(tree) or (fallback_nodes or {})
    stale = _stale_details(nodes, end)
    while True:
        checks += 1
        now = time.monotonic()
        if not stale:
            return {"nodes": nodes, "meta": meta, "tree": tree, "stale": [],
                    "waited_sec": now - t0, "checks": checks,
                    "deadline_min": deadline_min}
        if now >= deadline:
            return {"nodes": nodes, "meta": meta, "tree": tree, "stale": stale,
                    "waited_sec": now - t0, "checks": checks,
                    "deadline_min": deadline_min}

        # 只在「首次 / 数量变化 / 每分钟」打日志，避免 20s 一条刷屏
        if checks == 1 or len(stale) != last_count or now - last_log_t >= 60:
            log.info("等待 feed 更新：仍有 %s/%s 个源未更新，最多再等 %ss"
                     "（第 %s 次检查）",
                     len(stale), len(nodes), max(0, int(deadline - now)), checks)
            last_log_t, last_count = now, len(stale)

        time.sleep(min(poll_sec, max(0.5, deadline - now)))

        # list_tree 有 20s 缓存：显式失效，确保看到 Miniflux 刚写回的 checked_at
        ds = get_datasource()
        if hasattr(ds, "_invalidate"):
            ds._invalidate()
        meta, tree = _meta_index()
        fresh_nodes = _nodes_of(tree)
        if fresh_nodes:
            nodes = fresh_nodes
        stale = _stale_details(nodes, end)


def _allow_refresh(day: _date, catch_up: bool) -> bool:
    """今天/唤醒后的补跑必须允许刷新；只有“历史日期补跑”才假设数据已定型。

    ★ 旧逻辑是 `if not catch_up`，而 Guard 对今天也传 `catch_up=True`
      （`guard_tick`），于是休眠唤醒后的主恢复路径**完全不刷新**。
    """
    return (not catch_up) or (day == due_window_date())


def run(day: _date = None, *, catch_up: bool = False, force: bool = False,
        fresh_deadline_min: int = None, wait_min: int = None,
        dry_run: bool = False, no_cache: bool = False) -> dict:
    """跑一天的周期总结。幂等（date 唯一）；已 done 且非 force 直接跳过。

    `no_cache=True`（页面「忽略缓存重跑」）：跳过 S3/S2/S1 的缓存读取，
    但仍然把新结果写回缓存；S0 本来就不缓存。
    """
    day = day or due_window_date()
    # `wait_min` 是旧参数名（语义=固定睡多久）；保留兼容，映射到新的“最多等到多久”
    if fresh_deadline_min is None and wait_min is not None:
        fresh_deadline_min = wait_min
    if dry_run:
        return preview(day)
    # 总开关（scheduler 无条件注册，自动路径 daily/guard/catch_up 在这里被拦）。
    # force=True 是「重跑今天」这类手动明确动作，放行——关掉的是自动生成，不是人。
    if not force and not sk.effective("summary_enabled"):
        return {"date": day, "skipped": "日报已关闭（设置页 → 日报与总结）"}

    t0 = time.monotonic()
    start, end = window_for(day)
    # ★★ 抢占用行必须互斥：`summary_daily`(07:30 cron) 与 `guard_tick`(每 30 分钟)
    #    是两个独立的 job，机器休眠后**会在同一次 DarkWake 里一起补跑**（实测
    #    2026-10-15 07:58）。两者都调 run(同一天) 时会各自读写同一行 SummaryRun，
    #    结果出现只在并发下才可能的状态：state=summarizing 但 finished_at 已写、
    #    attempt/catch_up 被对方回滚。这里把「判重 + 抢占 attempt」做成临界区。
    with _RUN_LOCK:
        # ★ 必须显式带 kind：SummaryRun 同时装日报与周报，唯一键是 (kind, date)。
        #   漏了 kind 会退化成「按 date 找」，拿到那天的周报行 → 误判"已生成"。
        row, _created = SummaryRun.objects.get_or_create(
            kind=SummaryRun.KIND_DAILY, date=day)
        if row.state == "done" and not force:
            return {"date": day, "skipped": "已生成（force=1 可重跑）", "state": row.state}
        # ★ 「在跑」的判据是 registry（_run_alive），不是挂钟年龄：睡眠会让
        #   健康运行的挂钟年龄虚涨（2026-10-25 双跑事故）。registry 里没有
        #   活线程却停在 IN_FLIGHT → 是崩溃残留，直接抢占重跑，不必等 90 分钟。
        if row.state in IN_FLIGHT and _run_alive(day):
            log.warning("总结 %s 已在跑（%s，起于 %s），本次跳过（防同日并发）",
                        day, row.state, row.started_at)
            return {"date": day, "skipped": f"进行中（{row.state}）", "state": row.state}

        row.attempt += 1
        row.catch_up = bool(catch_up)
        row.state = "prefetch"
        row.started_at = timezone.now()
        row.finished_at = None
        row.window_start, row.window_end = start, end
        row.error = ""
        row.report_path = str(report_path(day))
        row.save()
        # 注册必须与抢占同一临界区：状态落库和登记之间不留窗口，否则并发
        # 的 run() 会在窗口里看不到对方、双双抢到同一天。
        _ACTIVE_RUNS[day] = threading.current_thread()

    # ★ 只有真正抢到这一轮才记 INFO（跳过/已在跑的分支上面就 return 了），
    #   它是理解"那天早上到底跑了没有"的第一条线索。
    log.info("总结开始 date=%s 第 %s 次尝试｜窗口 %s → %s｜补跑=%s",
             day, row.attempt, start.strftime("%m-%d %H:%M"),
             end.strftime("%m-%d %H:%M"), bool(catch_up),
             extra=fields(src="summary", run=str(day), attempt=row.attempt))

    stats = {"calls": 0, "tokens": 0, "reasoning_tokens": 0,
             "s3": 0, "s2": 0, "s1": 0, "s0": 0,
             "reused": {"s3": 0, "s2": 0, "s1": 0},
             "errors": [], "truncated": [], "budget_stop": False,
             "gate_stop": False, "dropped_l1": 0, "no_cache": bool(no_cache),
             "refresh": {}, "cite_missing": [], "freshness": {}}
    try:
        summarycache.purge_expired()
    except Exception as exc:  # noqa: BLE001  缓存清理失败不该拖垮日报
        log.debug("清理总结缓存失败：%s", exc)
    meta, tree = _meta_index()
    nodes = _nodes_of(tree)
    stale_before = _stale_details(nodes, end)
    transient_before = _transient_error_details(nodes, end)
    stats["freshness"] = {
        "stale_before": len(stale_before),
        "before_stale": stale_before,
        "stale_after": len(stale_before),
        "retry_before": len(transient_before),
        "attempted": False,
        "waited_sec": 0.0,
        "deadline_min": 0,
    }
    allow_refresh = _allow_refresh(day, catch_up)
    failed_stale = []   # 窗口缺口补救刷新失败（必须覆盖 last_success 启发式）

    try:
        # ① 补救刷新 + 轮询等待新鲜
        #   ★ 不再固定 sleep(15min)：Miniflux 60 分钟才开一次闸，休眠唤醒后下一批
        #     可能在任何时刻；轮询到“没有 stale”或 deadline 才继续。
        #   ★ 今天/唤醒后的 Guard 也必须刷新（历史日期才跳过）。
        #   ★ 窗口后瞬时错误也顺手重试，但**不进日报**（只清 Miniflux 的假红）。
        if allow_refresh and (stale_before or transient_before):
            log.info("总结前检查：窗口缺口 %s 个，窗口后瞬时错误 %s 个，开始补救刷新",
                     len(stale_before), len(transient_before),
                     extra=fields(src="summary", run=str(day)))
            stats["refresh"] = refresh_stale(nodes, end)
            row.state = "waiting"
            row.save(update_fields=["state"])
            wait_res = wait_until_fresh(
                end, deadline_min=fresh_deadline_min, fallback_nodes=nodes)
            if wait_res.get("nodes"):
                nodes = wait_res["nodes"]
            if wait_res.get("meta"):
                meta = wait_res["meta"]
            if wait_res.get("tree"):
                tree = wait_res["tree"]
            refresh = stats["refresh"]
            failed_stale = [f for f in refresh.get("failed", [])
                            if not f.get("retry")]
            # ★ refresh 失败后 Miniflux 的 last_success 反推会“跳到 end 之后”，
            #   必须把失败项并回 stale，否则会误报“全部补齐”。
            after = _merge_stale_details(wait_res["stale"], failed_stale, end, nodes)
            stats["freshness"].update({
                "attempted": True,
                "stale_after": len(after),
                "after_stale": after,
                "waited_sec": wait_res["waited_sec"],
                "deadline_min": wait_res["deadline_min"],
                "checks": wait_res["checks"],
            })
            log.info("补救刷新/等待结束：窗口缺口仍有 %s 个（其中刷新失败 %s 个）；"
                     "窗口后错误重试 %s 个（成功 %s 失败 %s），等待 %.0fs",
                     len(after), len(failed_stale),
                     refresh.get("retry_tried", 0), refresh.get("retry_ok", 0),
                     refresh.get("retry_fail", 0), wait_res["waited_sec"],
                     extra=fields(src="summary", run=str(day)))
        elif not allow_refresh:
            log.info("补跑历史日期 date=%s：跳过补救刷新（数据已定型）", day,
                     extra=fields(src="summary", run=str(day)))

        # ② 复检异常
        try:
            new_meta, new_tree = _meta_index()   # 刷新后再取一次健康状态
            new_nodes = _nodes_of(new_tree)
            if new_nodes:
                meta, tree, nodes = new_meta, new_tree, new_nodes
            else:
                log.warning("总结复检拿到空树，沿用刷新前快照",
                            extra=fields(src="summary", run=str(day)))
            if stats["freshness"].get("attempted"):
                after = _merge_stale_details(
                    _stale_details(nodes, end), failed_stale, end, nodes)
                stats["freshness"]["stale_after"] = len(after)
                stats["freshness"]["after_stale"] = after
        except Exception as exc:  # noqa: BLE001
            # ★ 不复检就继续 = 用**刷新前**的健康快照算异常 → 异常段可能漏报。
            #   不打断流程，但必须留痕（否则"这次报告怎么没提某源挂了"无从追查）。
            log.debug("总结复检健康状态失败，沿用刷新前的快照：%s", exc,
                      extra=fields(src="summary", run=str(day)))
        anomalies = collect_anomalies(nodes, end, stats)
        row.anomalies_json = anomalies
        row.save(update_fields=["anomalies_json"])

        # ③ 采集 + 分层
        row.state = "summarizing"
        row.save(update_fields=["state"])

        entries = collect_window(start, end)
        ratings = {r.entry_id: r for r in EntryRating.objects.filter(
            entry_id__in=[e.id for e in entries])}
        items = build_items(entries, ratings, meta)
        items, stats["dropped_l1"] = cap_feed_l1(
            items, settings.SUMMARY_FEED_MAX_ITEMS, settings.SUMMARY_FEED_L1_KEEP)
        if stats["dropped_l1"]:
            log.info("单源 L1 过多：%s 条未纳入日报（L2/L3 全部保留）",
                     stats["dropped_l1"],
                     extra=fields(src="summary", run=str(day)))
        row.items_count = len(items)
        row.save(update_fields=["items_count"])   # 列表页要显示，先落库（失败也不至于留空）

        by_cat = {}
        for it in items:
            cat = by_cat.setdefault(it["category_id"], {
                "id": it["category_id"], "title": it["category_title"],
                "items": [], "feeds": {}})
            cat["items"].append(it)
            cat["feeds"].setdefault(it["feed_id"], []).append(it)

        window_label = f"{start:%m-%d %H:%M} → {end:%m-%d %H:%M}"
        # 外链 → 站内深链的映射（只在最后一步用，见 internalize_cites 的时序契约）
        url_map = entry_url_map(items)
        if not items:
            # ★ 空窗口直接走规则版降级，不调任何 LLM：S3/S2/S1 本就因空集合不跑，
            #   唯独 S0 无条件调用 —— 没内容时白烧一次合成调用（实测 0 条目仍 ¥0.0x）。
            #   body 置空即复用下方既有的规则版渲染 + 落盘路径，不新增分支。
            body, covered, err = "", [], ""
        else:
            _s3_pass(items, stats, row.id, no_cache=no_cache)
            groups = {}
            for cat in by_cat.values():
                for fid, its in cat["feeds"].items():
                    groups[fid] = its
            s2 = _s2_pass(groups, stats, row.id, window_label, no_cache=no_cache)
            s1 = _s1_pass(items, s2, by_cat, stats, row.id, window_label,
                          no_cache=no_cache)
            body, covered, err = _s0_pass(items, s1, by_cat, stats, row.id,
                                          anomalies, window_label, nodes,
                                          no_cache=True)
        stats["s0"] = 1 if body else 0

        # ④ 渲染 + 原子落盘
        if body:
            # ★ 模型改写/编造的数字要被抓出来：素材里的数字是**唯一真值**。
            #   实测踩过 208→209、878→880（见 `number_audit` 注释）。
            body = _strip_autogen(body)
            # ★ 连续引用塞空格：`)[` 背靠背肉眼分不清哪条引用支撑哪句话
            #   （放在覆盖统计之前/之后都不影响 —— 引用正则不依赖空格）。
            body = space_cites(body)
            # ★ 引用写进头部（替代原来的整段「来源索引」）：
            #   口径按**要点**（每个要点都得点得回原文），素材覆盖只作背景 ——
            #   合并型摘要的素材覆盖天然 <100%，拿它报警等于每期假红。
            hit, total = _cite_coverage(body, items)
            stats["cite_hit"], stats["cite_total"] = hit, total
            p_hit, p_total, n_links = _point_coverage(body)
            stats["pt_hit"], stats["pt_total"], stats["cites"] = p_hit, p_total, n_links
            stats["uncited_cats"] = _uncited_categories(body, by_cat)
            stats["num_unknown"] = number_audit(
                body, "\n".join([window_label]
                                + list(anomalies)
                                + [_stats_section(items, by_cat, nodes)]
                                + [f"{it['title']} {it['text']}" for it in items]))
            # ★ 外链 → 站内深链（点击定位到那条并展开，src=daily 供打开率统计）。
            #   必须在 number_audit / _cite_coverage 之后：它们按**素材外链**匹配，
            #   先改写会让覆盖统计清零（internalize_cites 的时序契约）。
            body = internalize_cites(body, url_map, "daily")
            md_lines = _header(day, start, end, row, stats)
            md_lines += [_stats_section(items, by_cat, nodes), "", body, ""]
            md = "\n".join(md_lines)
        else:
            # 规则版逐条都渲染了引用行 → 两个口径天然 100%，但头部也要如实写出来，
            # 否则「头部没有引用」会被误读成「一条都没引用」。
            uniq = {it["url"] for it in items if it["url"]}
            stats["cite_hit"] = stats["cite_total"] = len(uniq)
            # 每条 item 渲染成一行、各自带引用 ⇒ 要点数 = 渲染行数
            stats["pt_hit"] = stats["pt_total"] = len(items)
            stats["cites"] = len(uniq)
            md = render_rule_report(day, start, end, row, items, by_cat, anomalies, stats,
                                    nodes)
            # 规则版的引用行同样换成站内深链（口径与 LLM 版一致）
            md = internalize_cites(md, url_map, "daily")
            if err:
                md += f"\n> ⚠️ 降级原因：{err}\n"

        # ④ 渲染 + 原子落盘（tmp + replace，不会留半截 md）
        # ★ 传 degraded：这里知道自己是 LLM 版还是规则版（body 空 = 规则版）。
        #   预算耗尽的规则版**不许覆盖**已有的正常报告（2026-11-05 事故）。
        reportstore.write(day, md, degraded=not body)

        row.state = "done"
        row.finished_at = timezone.now()
        notes = []
        if stats["budget_stop"]:
            notes.append("预算用尽，内容降级为规则版")
        if not body and err:
            notes.append(f"S0 全局合成为空（{err}）")
        if stats.get("gate_stop"):
            notes.append("调用闸提前关闭，部分分层未完成")
        if stats["truncated"]:
            notes.append("触到输出上限、内容不完整（硬截断）：" + "、".join(stats["truncated"]))
        row.error = "；".join(notes)[:900]
        row.save(update_fields=["state", "finished_at", "error"])

        ms = int((time.monotonic() - t0) * 1000)
        reuse = stats.get("reused") or {}
        reused_total = sum(int(reuse.get(k, 0)) for k in ("s3", "s2", "s1"))
        log.info("总结完成 date=%s 条目 %s｜S3 %s · S2 %s · S1 %s · S0 %s｜"
                 "LLM %s 次（复用 %s） / %s tokens（推理 %s）｜引用 %s/%s 要点｜"
                 "%s｜耗时 %ss",
                 day, len(items), stats["s3"], stats["s2"], stats["s1"],
                 stats["s0"], stats["calls"], reused_total, stats["tokens"],
                 stats["reasoning_tokens"], stats.get("pt_hit", 0),
                 stats.get("pt_total", 0),
                 "规则版降级" if not body else "LLM 合成", ms // 1000,
                 extra=fields(src="summary", run=str(day), items=len(items),
                              calls=stats["calls"], reused=reused_total, ms=ms,
                              ok=True, degraded=bool(not body)))
        for note in notes:
            log.warning("总结 %s 的非致命问题：%s", day, note,
                        extra=fields(src="summary", run=str(day)))
        for err in stats["errors"]:
            log.warning("总结 %s 的调用错误：%s", day, err,
                        extra=fields(src="summary", run=str(day)))

        # ★ 生成完成通知：落点是**本地日报页**。它该说的是"今天的日报好了"，
        #   所以由**生成本身**触发，而不是绑在任何下游动作上。
        try:
            from core import notify as _nfy
            _nfy.daily(day, degraded=not body)
        except Exception as exc:  # noqa: BLE001  通知失败不该让"生成"看起来失败
            log.warning("日报完成通知失败（文件已落盘）：%s", exc,
                        extra=fields(src="summary", run=str(day)))

        return {"date": day, "state": "done", "items": len(items),
                "leads": len(items), "anomalies": len(anomalies),
                "calls": stats["calls"], "tokens": stats["tokens"],
                "reasoning_tokens": stats["reasoning_tokens"],
                "path": str(report_path(day)), "degraded": not body,
                "truncated": stats["truncated"],
                "s3": stats["s3"], "s2": stats["s2"], "s1": stats["s1"],
                "s0": stats["s0"], "reused": stats["reused"],
                "dropped_l1": stats["dropped_l1"], "errors": stats["errors"]}

    except Exception as exc:  # noqa: BLE001  任何异常都要留痕，否则 guard 无法判断
        log.error("总结失败 date=%s：%s\n%s", day, exc, traceback.format_exc())
        row.state = "failed"
        row.error = f"{type(exc).__name__}: {exc}"[:900]
        row.finished_at = timezone.now()
        row.save(update_fields=["state", "error", "finished_at"])
        # ★ 失败必须出声（静默失败会造成 FOMO：一整天没有日报，没人告诉你）。
        #   去噪：只在 ① 首次失败 ② 重试用尽 时发 —— 中间几次属于"正在处理"，
        #   同一原因反复响只会让人把通知关掉。
        try:
            from core import notify as _nfy
            max_attempt = int(sk.effective("summary_max_attempt"))
            terminal = row.attempt >= max_attempt
            if terminal or row.attempt <= 1:
                _nfy.daily_failed(day, row.error, attempts=row.attempt,
                                  max_attempts=max_attempt, terminal=terminal)
        except Exception as notify_exc:  # noqa: BLE001  通知失败不能吃掉原始错误
            log.warning("日报失败通知发送失败：%s", notify_exc,
                        extra=fields(src="summary", run=str(day)))
        return {"date": day, "state": "failed", "error": row.error}
    finally:
        # 摘牌必须无条件：done / failed / 未捕获异常，哪条路出去都要还掉
        # registry，否则这天的"活线程"判定永远为真，guard 从此不敢再碰它。
        # （抢占段与 try 之间若抛错导致没走到这里，线程已死，_run_alive()
        #   的 is_alive() 会让 reset_stale / 下一次 run() 正常接管。）
        with _RUN_LOCK:
            if _ACTIVE_RUNS.get(day) is threading.current_thread():
                del _ACTIVE_RUNS[day]


# ==================================================================== 容错
def reset_stale(max_age_min: int = None) -> int:
    """把卡住的运行重置为 pending（异常中断后的自愈入口）。

    进程崩 / 断电会让 state 永远停在 prefetch/waiting/summarizing，
    不重置的话 guard 会以为"有任务在跑"，从此再也不补跑。

    ★ 「活着」的判据是 _ACTIVE_RUNS（进程内线程），挂钟年龄只筛"死了多久"：
      2026-10-25 事故里健康运行的挂钟年龄被 macOS 睡眠拖过 90 分钟阈值，
      guard 按僵尸重置并起了第二条管线，双烧了一轮 LLM。
    ★ 与 run() 的抢占共用 _RUN_LOCK：guard / catchup 同一毫秒各调一次也
      会串行化，不会重复重置（当天 09:09 实测两条日志撞车）。
    ★ attempt 不再 +1：attempt 回归"真实执行次数"的语义，重置不是执行。
    """
    max_age = settings.SUMMARY_STALE_MIN if max_age_min is None else int(max_age_min)
    cutoff = timezone.now() - timedelta(minutes=max_age)
    n = 0
    reset_dates = []
    with _RUN_LOCK:
        candidates = SummaryRun.objects.filter(state__in=IN_FLIGHT).filter(
            Q(started_at__isnull=True) | Q(started_at__lt=cutoff))
        for r in candidates:
            if _run_alive(r.date):
                continue          # 活线程（睡眠中也算活）—— 绝不误杀
            _ACTIVE_RUNS.pop(r.date, None)   # 死线程残牌顺手摘掉
            r.state = "pending"
            r.error = (f"stale 重置（{max_age} 分钟未推进）"
                       + (f"；原错误 {r.error[:200]}" if r.error else ""))
            r.save(update_fields=["state", "error"])
            n += 1
            reset_dates.append(str(r.date))
    if n:
        log.warning("stale 重置 %s 条：%s", n, reset_dates)
    return n


def guard_tick() -> dict:
    """每 30 分钟兜底：过触发点且今天没 done、也没在跑 → 跑一次。

    ★ 今天必须走 `catch_up=False`：Guard 是休眠唤醒后的主恢复路径，
      旧代码传 `catch_up=True` 会跳过补救刷新，直接把 9.7h 前的数据写成日报。
    """
    reset_stale()
    now = timezone.localtime()
    today = now.date()
    if now < trigger_at(today):
        return {"ok": True, "skipped": "未到触发时刻"}
    row = SummaryRun.objects.filter(date=today).first()
    if row and row.state == "done":
        return {"ok": True, "skipped": "今天已生成"}
    if row and row.state in IN_FLIGHT and _run_alive(today):
        return {"ok": True, "skipped": f"进行中（{row.state}）"}
    # IN_FLIGHT 但没有活线程 → 崩溃残留，不必等 stale 重置，run() 直接抢占
    log.warning("Guard 触发补跑（今天，允许刷新）：%s", today)
    return {"ok": True, "ran": run(today, catch_up=False)}


def catch_up_scan(limit: int = None, max_attempt: int = None) -> dict:
    """扫最近 SUMMARY_LOOKBACK_DAYS 天（默认 3），按日期升序补跑缺失的那些天。

    ★ 周期性运行（每 SUMMARY_GUARD_MIN 分钟），不是"启动只跑一次"。
    ★ 窗口刻意只留 3 天：补跑一天要 ¥0.05～0.45，一次补 8 天既烧钱又会
      把当天分级挤停摆。更早的历史缺口不在这里管 —— 由 gaps() **列出来**，
      每行带一个手动补跑按钮，由人决定要不要花那笔钱。

    跳过规则：
      · 已在跑（prefetch/waiting/summarizing）→ 不插队，避免同一天并发跑两遍
      · 已 done → 跳过
      · 失败次数 ≥ max_attempt → 不再自动重试（否则会每 30 分钟烧一次钱）
    """
    reset_stale()
    limit = settings.SUMMARY_MAX_CATCHUP if limit is None else int(limit)
    max_attempt = (sk.effective("summary_max_attempt") if max_attempt is None
                   else int(max_attempt))
    today = timezone.localtime().date()
    now = timezone.now()
    missing = []
    for i in range(settings.SUMMARY_LOOKBACK_DAYS, 0, -1):
        d = today - timedelta(days=i)
        _, end = window_for(d)
        if end > now:                       # 窗口还没结束，跑不完整
            continue
        row = SummaryRun.objects.filter(date=d).first()
        if row:
            if row.state == "done":
                continue
            if row.state in IN_FLIGHT and _run_alive(d):
                continue      # 正在跑；死线程残留则往下走，交给 run() 抢占
            if row.attempt >= max_attempt:
                log.warning("补跑 %s 已达尝试上限 %s 次，跳过", d, row.attempt)
                continue
        missing.append(d)
    ran = []
    for d in missing[:limit]:
        ran.append(run(d, catch_up=True))
    return {"missing": len(missing), "ran": ran,
            "deferred": max(0, len(missing) - limit)}


def gaps(lookback: int = None) -> list:
    """列出**没有报告**的日期（新近 → 旧），供页面渲染逐日补跑按钮。

    与 catch_up_scan 的区别是决定性的：
      · catch_up_scan 只看 SUMMARY_LOOKBACK_DAYS（3）天，且会**自动**烧钱跑；
      · gaps 只**读**，回看更久，谁都不跑。
      超出自动窗口的历史缺口因此不会「静默悬空」——它们明晃晃列在页面上。

    判定：窗口已结束（end <= now）且（无 SummaryRun 行 或 state != done）。
    起点取 reportstore.active_since()（最早的报告文件），不列 reader 上线前的
    日子。正在跑的日期不算缺口（否则会显示成"缺"然后用户点了又撞上互斥）。
    """
    lookback = (settings.SUMMARY_GAP_LOOKBACK_DAYS if lookback is None
                else int(lookback))
    today = timezone.localtime().date()
    now = timezone.now()
    floor = max(reportstore.active_since(), today - timedelta(days=lookback))
    rows = {r.date: r for r in SummaryRun.objects.filter(date__gte=floor)}
    out = []
    for i in range((today - floor).days, 0, -1):
        d = today - timedelta(days=i)
        _, end = window_for(d)
        if end > now:
            continue                        # 窗口还没结束，不算缺口
        row = rows.get(d)
        if row is None:
            out.append({"date": d, "state": None, "state_label": "未跑过",
                        "attempt": 0, "in_flight": False,
                        "in_auto_window": i <= settings.SUMMARY_LOOKBACK_DAYS})
        elif row.state == "done":
            continue
        elif row.state in IN_FLIGHT:
            out.append({"date": d, "state": row.state,
                        "state_label": f"{row.get_state_display()}中",
                        "attempt": row.attempt, "in_flight": True,
                        "in_auto_window": i <= settings.SUMMARY_LOOKBACK_DAYS})
        else:
            out.append({"date": d, "state": row.state,
                        "state_label": row.get_state_display(),
                        "attempt": row.attempt, "in_flight": False,
                        "error": row.error,
                        "in_auto_window": i <= settings.SUMMARY_LOOKBACK_DAYS})
    return out


# ==================================================================== 手动触发
def _spawn(day, **kw):
    """在后台线程跑一次。

    同步跑会经历「补救刷新 + 最多 SUMMARY_FRESH_DEADLINE_MIN 分钟的等待 + 十几次
    LLM 调用」—— HTTP 请求早超时了，所以 Web 手动触发一律丢线程，页面只回"已触发"。
    """
    import threading

    def _work():
        from django.db import close_old_connections

        close_old_connections()      # 线程里必须重置连接（否则拿到父线程的旧连接）
        try:
            run(day, **kw)
        except Exception as exc:  # noqa: BLE001  run 内部已兜底，这里只防漏
            log.error("后台总结崩溃 date=%s：%s", day, exc)
        finally:
            close_old_connections()

    th = threading.Thread(target=_work, name=f"summary-{day}", daemon=True)
    th.start()
    return th


def trigger_async(day: _date = None, *, fresh_deadline_min: int = None,
                  force: bool = False, wait_min: int = None,
                  no_cache: bool = False) -> dict:
    """手动重跑某一天（Web 按钮）。

    ★ 今天重跑 → `catch_up=False`，允许补救刷新（旧代码一律 catch_up=True，
      于是“重跑今天”只是拿旧数据再总结一遍）；历史日期 → 跳过刷新。
    """
    day = day or due_window_date()
    if (SummaryRun.objects.filter(date=day, state__in=IN_FLIGHT).exists()
            and _run_alive(day)):
        return {"ok": False, "reason": "该日期正在运行中", "date": str(day)}
    _spawn(day, catch_up=(day < due_window_date()), force=force,
           fresh_deadline_min=fresh_deadline_min, wait_min=wait_min,
           no_cache=no_cache)
    return {"ok": True, "date": str(day)}


def catch_up_async(limit: int = 3) -> dict:
    """手动触发一轮补跑扫描（后台）。"""
    import threading

    def _work():
        from django.db import close_old_connections

        close_old_connections()
        try:
            catch_up_scan(limit=limit)
        finally:
            close_old_connections()

    threading.Thread(target=_work, name="summary-catchup", daemon=True).start()
    return {"ok": True, "limit": limit}


# ==================================================================== 页面数据
def cost_map(run_ids) -> dict:
    """{summary_run_id: {cost, calls, tokens}} —— 一次查询，日报周报共用。

    ★ 周报合并进 SummaryRun 之后，`LlmUsage.summary_run_id` 对两类都成立，
      所以这里不需要区分 kind（这正是当初合并的理由之一）。
    """
    ids = [i for i in run_ids if i]
    out = {}
    if not ids:
        return out
    for u in LlmUsage.objects.filter(summary_run_id__in=ids):
        acc = out.setdefault(u.summary_run_id, {"cost": 0.0, "calls": 0, "tokens": 0})
        acc["cost"] += float(u.cost_cny or 0)
        acc["calls"] += 1
        acc["tokens"] += (u.prompt_tokens or 0) + (u.completion_tokens or 0)
    return out


def recent_runs(limit: int = 30) -> list:
    """报告列表：状态 + 条目数 + 花费（按 summary_run_id 汇总）。

    ★ 只出日报（默认 manager 已按 kind 过滤）；周报走 `weekly.rows()`。
    """
    rows = list(SummaryRun.objects.order_by("-date")[:limit])
    cost = cost_map([r.id for r in rows])
    out = []
    for r in rows:
        c = cost.get(r.id) or {"cost": 0.0, "calls": 0, "tokens": 0}
        out.append({
            "date": r.date, "state": r.state, "state_label": r.get_state_display(),
            "attempt": r.attempt, "catch_up": r.catch_up,
            "items": (r.items_count or None),   # 0（未跑完）→ 模板显示 —
            "anomalies": len(r.anomalies_json or []),
            "cost_cny": round(c["cost"], 4), "calls": c["calls"], "tokens": c["tokens"],
            "error": r.error,
            "has_file": reportstore.path(r.date).is_file(),
            "started_at": timezone.localtime(r.started_at) if r.started_at else None,
            "finished_at": timezone.localtime(r.finished_at) if r.finished_at else None,
        })
    return out


def latest_report() -> dict:
    row = SummaryRun.objects.filter(state="done").order_by("-date").first()
    if not row:
        return {}
    text = reportstore.read(row.date)
    if text is None:
        return {"date": row.date, "missing": True}
    return {"date": row.date, "path": str(reportstore.path(row.date)), "text": text,
            "catch_up": row.catch_up, "attempt": row.attempt,
            "finished_at": timezone.localtime(row.finished_at) if row.finished_at else None}


def read_report(day: _date):
    """读取某日报告正文（供页面与周报素材用；不存在返回 None）。

    ★ 正文以文件为准（reportstore.read）；这里只额外带回该日的 SummaryRun 行
      （可能为 None —— 手放进目录的文件没有运行记录），给页面显示状态用。
    """
    text = reportstore.read(day)
    if text is None:
        return None
    return {"row": SummaryRun.objects.filter(date=day).first(),
            "path": str(reportstore.path(day)), "text": text}
