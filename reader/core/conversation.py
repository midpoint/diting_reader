"""内容块上的对话（阶段 2.1，**非流式** v1）。

定位：在「文章 / 日报 / 周报」正文底下，基于**这篇材料 + 我的划线与批注**问答。
这是 wiki 的第二半：划线批注是「我留下的痕迹」，对话是「把想法逼出来」的地方；
路线图的下一步（2.2 分诊 + 提取）就吃这里的对话记录。

★★ 三层不可混（用户 2026-10-31 当面纠正，我原来写反了）：
  1. **正文** = 别人的原文。
  2. **划线（quoted_text）** = 原文的片段被摘出来，只表示「**我在意这一段**」。
     它常是重要事实，也常是我认可（或存疑）的他人的判断 —— 但**不是我的判断**。
     所以既不能当「原文之外的新事实」，也不能当「我的观点」。
  3. **我自己敲的字**（划线的 note + 批注的 body）= **我的思考**。
     只有这一层能被称为「我的判断」。没有备注的划线 = 只标了关注、没留看法，
     AI 不许替它补想法。

边界：
  - **不碰模板、不碰请求**：只做「校验 → 取素材 → 拼消息 → 调模型 → 落库」。
  - **不直接 `llm.chat`**：一律走 `citeguard._call`（kind=wiki_chat、pool=POOL_WIKI），
    这样预算闸 + 记账 + 「推理吃光预算就翻倍重试」这三条血泪教训都自动继承。
  - **用户消息先落库**：模型失败/超限时，问题也要留下来 —— 否则用户白打一遍字，
    而且「我刚问了什么」在刷新后就没了（静默丢数据比报错更糟）。

★ 为什么对话不套 `_call_cited`（那是有引用守卫的版本）：对话可以有出处，
  但不是「每条要点必须抄回 `[源名 · 时](URL)`」那种报告体。硬套会让模型
  在闲聊里堆一串链接。需要可追溯的是**沉淀出去的那句话**（2.4），不是聊天本身。

上下文顺序（稳定前缀在前，提高 provider 侧缓存命中率）：
  系统（角色规则 + 标题 + 正文 + 划线 + 批注） → 最近 N 条历史 → 本次问题

★ 长文（2026-11-15）：正文超过 `WIKI_CHAT_ARTICLE_CHARS` 时**不再截断** ——
  `split_body()` 按段落/引文边界均衡切成 ≤ `WIKI_CHAT_MAX_CHUNKS` 片，每片一次
  「取材」调用（不下全文结论），最后一次「汇总」调用综合全部笔记作答。短文
  （实测约 92%）仍是原来一次调用的老路径，一个 token 都不多花。
  ★ 分片只为覆盖全文，**不是**把渲染路径改成会起模型：这里所有模型调用都在
  `ask()` 的 POST 里，页面渲染只读库。
"""
import logging
import math
import re

from django.conf import settings

from core import citeguard
from core import costreport as cr
from core import marks
from core import reporttext
from core import sourceblock
from core.models import Conversation
from typing import Optional

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ 预设话术（单一来源）
#: 快捷问题按钮发出的文本。**这些不是「我敲的字」** —— 是我点了一下，不是我想的。
#: `core/ingest.py` 的分诊必须把它们排除，否则「总结这篇文章」会被当成我的判断，
#: 整条沉淀链从第一个字就错了。
#: ★ 单一来源在这里，模板只消费它；`core/tests/test_ingest.py` 有双向守卫
#:   （模板里出现的 data-wc-ask / data-autosend 必须都在这里，反之亦然）。
QUICK_ASKS = (
    ("总结这篇", "用中文总结这篇：先给 3-5 条要点，再指出作者的核心论断。"),
    ("哪里站不住", "这篇里最站不住的一句是哪句？为什么？"),
    ("和我的划线矛盾吗", "我划的这几处和正文的结论有矛盾吗？"),
)

#: 打开 feed 文章时自动发的那一句（"点开即总结"）。同样不是我的字。
AUTOSEND_TEXT = "用中文总结这篇文章：先给 3-5 条要点，再指出作者的核心论断与主要证据。"

#: 全部预设话术的归一化集合（归一化在 ingest 里做，这里只给原文）。
PRESET_TEXTS = tuple(t for _label, t in QUICK_ASKS) + (AUTOSEND_TEXT,)

#: 系统提示。★ 第 2/3 条是这套产品的立场，也是用户当场纠正过的地方：
#: **划线 = 我在意（可能是原文事实、也可能是我认可的他人判断），只有我自己敲的
#: 备注/批注才是我的思考**。混掉这一层，对话就退化成「AI 替我复述我自己的想法」。
SYSTEM = """你是「谛听」阅读器里的读书助手。用户正在读下面的材料，并和你讨论它。

规则（按重要性从高到低）：
1. 只依据【材料】回答。材料里没有的，直说「材料里没提到」，不要用常识补。
2. 【我划出的原文】是用户从原文里摘出来的**片段**，只表示他注意到、在意这一段：
   可能是重要事实，也可能是他认可或存疑的「他人的判断」。
   **划线不是他的结论**：不要写成「你认为…」，也不要据此替他表态；
   它出自原文，但被划线并不等于更可信、更正确。
3. 【引文】是被文章引用进来的**别人的话**（回帖里引用的提问、README 的 Note、
   新闻里转述的原话）。它**不是本文作者的说法**：不要把【引文】里的主张写成
   「作者认为」，也不要拿它当作者的态度来评判作者。
   ★ 引文很长、作者只回了一句时，**以作者那句为主** —— 用户关心的是作者的回应，
   不是被引者说了什么；引文只是理解那句话所需要的上下文。
4. 只有【我的备注】【我的批注】是用户自己敲的字 —— 那才是他的思考与判断。
   可以质疑、可以指出与原文的出入，但不要把它们说成原文或作者的观点。
   某条划线没有备注，说明他当时只标记了关注、**没留下看法**：
   不要替他补想法；需要时可以直接问他为什么划这句。
5. 数字、日期、人名必须来自材料原文；凡是推算，明确写「按…推算」。
6. 中文回答，简洁：先给结论，再给依据。不要复述整段材料。
"""

#: 长文「分片取材」时追加在 SYSTEM 之后的规则。★ 反复强调「不下全文结论」：
#:   不写这条，每片都会各自给一份「总结」，汇总那次就变成把 N 份互相矛盾的
#:   结论拼起来 —— 比截断更难发现（看着很完整，其实每片都以偏概全）。
CHUNK_RULE = """

【本次任务：分片取材，不是最终作答】
上面的材料是一篇长文按顺序切出的**第 %(i)d/%(n)d 片**（全文共 %(n)d 片，本片约 %(size)d 字），
你只看到了这一片。请为最后要回答的那个问题做「取材笔记」：
- 只依据【本片正文】，列出与问题直接相关的要点、事实、数字和原话；原话照抄、不要改写。
- 每条用「- 」开头，尽量短；本片没有相关内容就只写一行「（本片无相关内容）」。
- ★ 不要下全文结论，不要写「总结」「总体来看」「作者的核心论断是」这类收束语，
  不要复述整片，也不要推测其它片写了什么。
- 【引文】…【/引文】里的话仍按上面的归属规则处理：那是被引者的话，不是作者的说法。
- 本片之外的内容一个字都不要编。
"""

#: 长文「汇总作答」时追加在 SYSTEM 之后的规则。
REDUCE_RULE = """

【材料说明】这篇正文共 %(total)d 字，已按顺序均衡分成 %(n)d 片读完，下面按原片顺序
列出每片的取材笔记。请综合**全部**笔记作答，不要只用前几片，也不要把「（本片无相关内容）」
当成全文没有。%(failed)s
"""

#: 引文跨度与段落边界。**切点不许落在引文中间** —— 半截【引文】会让模型把后面
#: 整段算成别人的话（与 `reporttext.clip_quotes` 同一个事故，2026-11-03 实测）。
_QUOTE_SPAN = re.compile(r"【引文】.*?【/引文】", re.S)
#: 单个引文标记（`group(1)` 为 "/" 表示闭合）。分片补边界时按**深度**算，
#: 不按「开闭各数几个」算 —— 见 `_balance_quotes` 里那段（中间片 0/0 假平衡）。
_QUOTE_MARK = re.compile(r"【(/?)引文】")
_PARA_BREAK = re.compile(r"\n+")


def _stats() -> dict:
    """`_call` 需要的一次性统计容器（对话没有「一次运行」，用完即弃）。

    ★ 键必须和 `citeguard._call` 用到的一致 —— 定义已收到 `citeguard.fresh_stats()`
      一处（复制五份必然有一份漏键，失败点在「跑到第 N 步才炸」）。
    """
    return citeguard.fresh_stats()


def _header(blk: dict) -> list:
    """材料抬头（标题 + 来源行）—— 老路径 / 单片 / 汇总共用一份，别抄三遍。

    ★ 与 `ingest._material` 同一份来源行（2026-11-03）：问「作者说的这句话
      是什么意思」时，模型需要知道作者是谁，而不是靠订阅源名的先验知识猜。
    """
    parts = ["# 材料：%s" % (blk["title"] or "（无标题）")]
    note = sourceblock.source_note(blk)
    if note:
        parts.append(note)
    return parts


def _marks_sections(highlights, annotations) -> list:
    """划线 / 批注两块（老路径与汇总共用）。

    ★★ 小标题本身就是说明书：划线那块必须写明「只表示我在意」，批注那块写明
      「我自己敲的字」。模型的默认倾向是把清单里的东西都当成「用户的观点」，
      光靠 SYSTEM 里一条规则压不住，标签写在数据旁边才稳。
    ★ 入参是 `marks.list_marks` 的字典形式（不是模型对象）——那里已经是
      渲染层/对话层共用的规范化结构，别在这里再 .objects 查一次。
    """
    parts = []
    if highlights:
        parts += ["", "## 我划出的原文（%d 条；这些是原文片段，只表示我在意，"
                      "不是我的结论）" % len(highlights)]
        noted = 0
        for i, h in enumerate(highlights, 1):
            note = (h.get("note") or "").strip()
            parts.append("%d. 「%s」" % (i, (h.get("quoted_text") or "").strip()))
            if note:
                noted += 1
                parts.append("   └ 我的备注（我自己写的）：" + note)
        if not noted:
            # ★ 不写这句，模型几乎必然替这些划线编一个「你的观点」
            parts.append("（以上划线都没有备注：我只标了关注，没留下看法。）")
    if annotations:
        parts += ["", "## 我的批注（%d 条；我自己敲的字，是我的思考）"
                      % len(annotations)]
        for i, a in enumerate(annotations, 1):
            parts.append("%d. %s" % (i, (a.get("body") or "").strip()))
    return parts


def _quote_depth(text: str) -> int:
    """`text` 结尾处的引文嵌套深度（0 = 不在引文里）。"""
    depth = 0
    for m in _QUOTE_MARK.finditer(text):
        depth = max(0, depth - 1) if m.group(1) else depth + 1
    return depth


def _balance_quotes(text: str, starts_inside: bool = False) -> str:
    """让这一片**自己**带齐引文边界，能独立进模型。

    ★ 硬切点可能落在【引文】中间 ⇒ 一片可能是「引文的后半段」或「引文的正中间」。
      只按本片内的标记数配对是**不够的**：中间那片里一个标记都没有，配对结果
      「0 开 0 闭 = 没问题」，于是被引者的话就变成了作者自己说的 —— 正是
      2026-11-02 那类归属事故，只不过这次出现在分片边界上。所以要由调用方告诉
      我们「本片起点是否已经在引文内部」（`starts_inside`，由全文偏移量算出）。
    ★ 只补标记、不回退切点（与 `reporttext.clip_quotes` 同一取舍）：回退会白丢
      已经读入的正文。多出的是**标记**不是原文，`split_body` 的精确划分不受影响。
    """
    prefix = "【引文】" if starts_inside else ""
    depth = 1 if starts_inside else 0
    for m in _QUOTE_MARK.finditer(text):
        depth = max(0, depth - 1) if m.group(1) else depth + 1
    return prefix + text + "【/引文】" * depth


def _cut_candidates(body: str) -> list:
    """可用切点：段落结束处 + 引文结束处；**排除引文内部的段落边界**。"""
    spans = [(m.start(), m.end()) for m in _QUOTE_SPAN.finditer(body)]

    def inside_quote(pos):
        return any(a < pos < b for a, b in spans)

    out = {m.end() for m in _PARA_BREAK.finditer(body)
           if not inside_quote(m.end())}
    out.update(b for _a, b in spans)
    return sorted(p for p in out if 0 < p < len(body))


def split_body(body: str, chunk_chars: int, max_chunks: int) -> list:
    """把长正文**均衡**切片；返回原文的一个精确划分（拼回去 == 原文）。

    ★ 三条不变量（改这个函数前先想清楚为什么）：
      1. 不丢字、不重字：`''.join(split_body(x, ...)) == x`。分片是为了覆盖全文；
         为了「整齐」丢掉几个换行，就又变成另一种更难发现的截断。
      2. 均衡：`n = min(max_chunks, ceil(len/chunk_chars))`，每片目标
         `ceil(len/n)`；切点取离理想位置最近的**安全边界**（段落 / 引文结束）。
         真没有边界可切（整段无换行）才硬切，硬切也照样不丢字。
      3. 片数有上限、正文无上限：超过 `max_chunks` 时把每片自动加长，绝不
         只取前几片 —— 用户这次报的 bug 就是「后半篇没了」。
    ★ 只做切片、不补引文标记（那是 `_balance_quotes` 在拼 prompt 时的事），
      这样「精确划分」才是可直接断言的纯函数。
    """
    body = body or ""
    total = len(body)
    size = max(1, int(chunk_chars))
    cap = max(2, int(max_chunks))
    if total <= size:
        return [body]
    n = min(cap, max(2, math.ceil(total / size)))
    target = math.ceil(total / n)
    candidates = _cut_candidates(body)
    cuts, prev = [], 0
    # ★ 最小间距不能省：引文结束处与其后的换行只差 1 个字符，两个都当候选时
    #   会切出「1 字片」——那片进模型既浪费一次调用又什么都没读到。
    min_gap = max(1, target // 3)
    for i in range(1, n):
        hi = total - (n - i)          # 给后面每片至少留 1 个字
        lo = prev + min_gap
        if hi < lo:
            break
        ideal = min(hi, max(lo, i * target))
        pool = [c for c in candidates if lo <= c <= hi]
        cut = min(pool, key=lambda c: (abs(c - ideal), c)) if pool else ideal
        cuts.append(cut)
        prev = cut
    out, prev = [], 0
    for cut in cuts + [total]:
        out.append(body[prev:cut])
        prev = cut
    return [c for c in out if c] or [body]


def _chunk_material(blk: dict, chunk: str, i: int, n: int,
                    starts_inside: bool = False) -> str:
    """单片材料：抬头 + 本片正文（带闭合的引文边界）。划线批注只在汇总里给。"""
    parts = _header(blk)
    parts += ["", "## 正文（全文第 %d/%d 片，本片约 %d 字）" % (i, n, len(chunk)),
              _balance_quotes(chunk, starts_inside)]
    return "\n".join(parts)


def _reduce_material(blk, notes, highlights, annotations,
                     total, n, failed) -> str:
    """汇总材料：抬头 + 按原顺序排列的全部分片笔记 + 我的划线与批注。"""
    parts = _header(blk)
    parts += ["", "## 全文取材笔记（正文共 %d 字，按顺序均衡分 %d 片）"
              % (total, n)]
    if failed:
        parts.append("★ 有 %d 片取材失败（见对应笔记）：那些片的内容没有读到，"
                     "回答里必须说明覆盖缺口，不许假装读到了。" % len(failed))
    parts.append("")
    parts += notes
    parts += _marks_sections(highlights, annotations)
    return "\n".join(parts)


def _material(blk: dict, highlights, annotations, body=None) -> str:
    """把「正文 + 划线 + 批注」拼成一段材料（放 system 里）—— **短文老路径**。

    ★★ 这里**永远不截断正文**（2026-11-15）。原来有 `clip_quotes(limit)` 加一句
      「（正文超过 N 字，以上为前 N 字）」—— 那就是用户报的「长文总结不可用了」
      的根因：超过 6000 字的文章，后半篇连模型都没见过。
      现在长文由 `ask()` 先 `split_body()` 切片、逐片取材、再汇总，本函数只剩
      「正文本来就短（≤ 一片）」这一条路。**故意把截断分支整段删掉**，而不是留
      一个「反正走不到」的分支：留着它，谁哪天把 `ask()` 改回单次调用就立刻复发，
      而且旧单测还会绿着（判据比代码更容易骗人）。
    ★ `body` 可由调用方传入，避免 `body_text()` 被重复算一遍。
    """
    # ★ 必须走 `body_text`：它给 `<blockquote>` 套【引文】边界。
    #   这里原来直接取 `blk["text"]`，而 text 已经过 `to_plain()`，
    #   `<blockquote>` 早被抹成空格 —— 博主回帖里引用的提问，在模型眼里就是
    #   博主自己说的话（2026-11-02 的归属事故正是这么来的，当时只修了 wiki
    #   提炼那条链路，对话这条漏了）。
    if body is None:
        body = reporttext.body_text(blk)

    parts = _header(blk)
    parts += ["", "## 正文", body]
    parts += _marks_sections(highlights, annotations)
    return "\n".join(parts)


def get_or_none(source_type: str, source_id: str):
    """按 (source_type, source_id) 取已有会话；不存在返回 None（不建）。"""
    st, sid, err = marks.validate_source(source_type, source_id)
    if err:
        return None
    return Conversation.objects.filter(source_type=st, source_id=sid).first()


def get_or_create(source_type: str, source_id: str):
    """取或建会话。返回 (conv, err)；校验失败 conv=None。"""
    st, sid, err = marks.validate_source(source_type, source_id)
    if err:
        return None, err
    conv, _ = Conversation.objects.get_or_create(source_type=st, source_id=sid)
    return conv, ""


def history(conv, limit: Optional[int] = None):
    """会话里的消息（按时间正序）。`limit` 只取最近 N 条（喂模型用）。"""
    if conv is None:
        return []
    qs = conv.messages.all()
    rows = list(qs if limit is None else qs.order_by("-id")[:limit])
    return list(reversed(rows)) if limit is not None else rows


def _ask_chunked(blk, body, chunks, question, hist, data, on_delta=None) -> tuple:
    """长文：逐片取材 → 一次汇总。返回 (text, err, note)。

    ★ on_delta（2026-11-19）：只有**汇总作答**那一次流式吐给前端；分片取材是
      「笔记」，不是给用户的答案，吐出来只会让人看到一堆半成品。

    ★ `stats` 是**跨全部片共用**的一份：预算闸 `budget_stop` 一旦被某片点亮，
      后续片会立刻短路（不会一片片去撞同一堵墙）。`max_calls` 给 `2*(n+1)`
      而不是 `n+1`：`_call` 默认 `retry_on_empty=True`，某片「推理吃光预算、正文
      为空」时会翻倍重试一次（那是**真花钱**的一次调用）；少给一半会让前面某片
      重试过就把后面的片误判成「达到单次调用上限」（静默漏读）。
    ★ 单片失败按用户 2026-11-15 拍板的边界：**继续**，但笔记里写明失败、
      汇总 prompt 里写明覆盖缺口、最终回答开头也写明第几片没读到 —— 三处
      都说，才不算「假装读完了全文」。全部片都失败才认输。
    ★ `body` 是为了算「本片起点是否在引文内部」：`chunks` 是 `body` 的精确划分，
      按 `len(chunk)` 递推偏移量即可，不用重新切一遍。
    """
    n = len(chunks)
    total = len(body)
    stats = _stats()
    cap = 2 * (n + 1)
    label = (blk["title"] or "")[:40]
    notes, failed = [], []
    stall = ""
    pos = 0
    for i, chunk in enumerate(chunks, 1):
        starts_inside = _quote_depth(body[:pos]) > 0
        pos += len(chunk)
        if stall:
            failed.append((i, stall))
            notes.append("### 第 %d/%d 片\n（没有继续调用：%s）" % (i, n, stall))
            continue
        msgs = [{"role": "system",
                 "content": (SYSTEM + CHUNK_RULE % {"i": i, "n": n,
                                                    "size": len(chunk)}
                             + "\n\n" + _chunk_material(blk, chunk, i, n,
                                                        starts_inside))},
                {"role": "user", "content": question}]
        text, err = citeguard._call(
            "wiki_chat", "", "", stats, messages=msgs, max_calls=cap,
            max_tokens=int(settings.WIKI_CHAT_MAX_TOKENS),
            temperature=float(settings.WIKI_CHAT_TEMPERATURE),
            pool=cr.POOL_WIKI, tag="wiki 对话 · 分片取材 %d/%d" % (i, n),
            label=label)
        why = err or ("" if text else "模型没有返回内容")
        if why:
            failed.append((i, why))
            notes.append("### 第 %d/%d 片\n（本片取材失败：%s）" % (i, n, why))
            log.warning("wiki 对话分片取材失败 %d/%d：%s", i, n, why)
            # ★ 预算用尽 / 达到调用上限：后面每一片都不可能成功，别空转
            if stats["budget_stop"] or "达到单次调用上限" in why:
                stall = why
        else:
            notes.append("### 第 %d/%d 片\n%s" % (i, n, text))

    if len(failed) == n:
        return "", "全文 %d 片取材都失败了：%s" % (n, failed[0][1]), ""

    gap = ("★ 本次有 %d 片取材失败（第 %s 片），这些片的内容没有读到；"
           "回答开头必须说明这个缺口，不许假装读完了全文。"
           % (len(failed), "、".join(str(i) for i, _ in failed))) if failed else ""
    msgs = [{"role": "system",
             "content": (SYSTEM + REDUCE_RULE % {"total": total, "n": n,
                                                 "failed": gap}
                         + "\n\n" + _reduce_material(
                             blk, notes, data["highlights"], data["annotations"],
                             total, n, failed))}]
    msgs += [{"role": m.role, "content": m.body} for m in hist]
    msgs.append({"role": "user", "content": question})
    if on_delta:
        # ★ 流式：汇总作答逐字吐；stats 沿用同一份（预算闸跨片共用，见上）。
        #   _call_stream 不做翻倍重试 —— 已经吐出去的字收不回。
        text, err = citeguard._call_stream(
            "wiki_chat", "", "", stats, on_delta=on_delta, messages=msgs,
            max_tokens=int(settings.WIKI_CHAT_MAX_TOKENS),
            temperature=float(settings.WIKI_CHAT_TEMPERATURE),
            pool=cr.POOL_WIKI, tag="wiki 对话 · 汇总作答", label=label,
            max_calls=cap)
    else:
        text, err = citeguard._call(
            "wiki_chat", "", "", stats, messages=msgs, max_calls=cap,
            max_tokens=int(settings.WIKI_CHAT_MAX_TOKENS),
            temperature=float(settings.WIKI_CHAT_TEMPERATURE),
            pool=cr.POOL_WIKI, tag="wiki 对话 · 汇总作答", label=label)
    if err or not text:
        return "", err or "模型没有返回内容", ""

    note = "（全文 %d 字，已分 %d 片读完" % (total, n)
    if failed:
        note += "；第 %s 片没读到（%s）" % (
            "、".join(str(i) for i, _ in failed), failed[0][1])
    note += "）\n\n"
    return text, "", note


def ask(source_type: str, source_id: str, question: str, on_delta=None) -> dict:
    """问一句，拿回复。返回 {ok, err, reply}。

    err 是**直接能显示给人**的话（含预算用尽的具体原因、正文读不到、模型报错）。

    ★ on_delta（2026-11-19，SSE）：给了就边生成边回调 `on_delta(piece)`；默认 None
      走原来的 `_call`（非流式）—— 几十处测试桩与非流式路径**逐字节不变**。
      ★ 用户消息在流式开始前就落库，失败/预算用尽时问题也留着（与原路径同）。
    """
    q = (question or "").strip()
    if not q:
        return {"ok": False, "err": "问题不能为空", "reply": ""}
    q = q[:max(1, int(settings.WIKI_CHAT_QUESTION_CHARS))]

    conv, err = get_or_create(source_type, source_id)
    if err:
        return {"ok": False, "err": err, "reply": ""}

    blk = sourceblock.load(conv.source_type, conv.source_id)
    if blk is None:
        # 正文读不到就不硬答：没有材料的「对话」只会变成模型的自由发挥
        return {"ok": False, "err": "读不到这篇的正文（条目可能已删除）", "reply": ""}

    data = marks.list_marks(conv.source_type, conv.source_id)

    # ★ 先取历史再落库本条，避免「历史里含本条 + 本次又发一次」的重复
    limit = max(0, int(settings.WIKI_CHAT_HISTORY))
    hist = history(conv, limit=limit) if limit else []

    conv.messages.create(role="user", body=q)

    # ★ 长文分片（2026-11-15）：先算出全文，再决定走老路径还是分片。
    #   `body_text()` 只算一次，两条路径共用 —— 老路径也不再重复解析 HTML。
    body = reporttext.body_text(blk)
    chunk_chars = max(500, int(settings.WIKI_CHAT_ARTICLE_CHARS))
    chunks = split_body(body, chunk_chars,
                        max(2, int(settings.WIKI_CHAT_MAX_CHUNKS)))
    note = ""
    if len(chunks) == 1:
        # 短文老路径：一次调用、一个 token 都不多花（实测约 92% 的提问）
        messages = [{"role": "system",
                     "content": SYSTEM + "\n\n" + _material(
                         blk, data["highlights"], data["annotations"], body=body)}]
        messages += [{"role": m.role, "content": m.body} for m in hist]
        messages.append({"role": "user", "content": q})
        if on_delta:
            text, err = citeguard._call_stream(
                "wiki_chat", "", "", _stats(), on_delta=on_delta,
                messages=messages, max_calls=1,
                max_tokens=int(settings.WIKI_CHAT_MAX_TOKENS),
                temperature=float(settings.WIKI_CHAT_TEMPERATURE),
                pool=cr.POOL_WIKI, tag="wiki 对话",
                label=(blk["title"] or conv.source_id)[:40])
        else:
            text, err = citeguard._call(
                "wiki_chat", "", "", _stats(), messages=messages, max_calls=1,
                max_tokens=int(settings.WIKI_CHAT_MAX_TOKENS),
                temperature=float(settings.WIKI_CHAT_TEMPERATURE),
                pool=cr.POOL_WIKI, tag="wiki 对话",
                label=(blk["title"] or conv.source_id)[:40])
    else:
        text, err, note = _ask_chunked(blk, body, chunks, q, hist, data,
                                       on_delta=on_delta)

    if err or not text:
        log.warning("wiki 对话失败 %s/%s: %s", conv.source_type, conv.source_id,
                    err or "空输出")
        # 用户消息已在库里（下次打开还在），这里只把原因显示出来
        return {"ok": False, "err": err or "模型没有返回内容", "reply": ""}

    reply = (note + text) if note else text
    conv.messages.create(role="assistant", body=reply)
    conv.save(update_fields=["updated_at"])
    return {"ok": True, "err": "", "reply": reply}


def clear(source_type: str, source_id: str) -> bool:
    """清空该内容块的对话（连消息一起删）。返回是否真的删了东西。"""
    conv = get_or_none(source_type, source_id)
    if conv is None:
        return False
    conv.delete()
    return True
