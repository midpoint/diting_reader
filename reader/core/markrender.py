"""把划线/批注的**文本锚点**回填成正文里的 inline 高亮（选中处原地高亮）。

为什么需要这一层（阶段 1 的「选中即标注」交互引入）：
  划线/批注是在「选中正文一段文字」时产生的，用户期望**渲染时那行字本身就是
  高亮的**，而不是只在底部列表里出现一行摘录。要做到这点，选中时得记下
  「这段文字在正文里的位置」—— 而正文是 Miniflux 抓来的第三方 HTML，每次抓取
  都可能变，DOM 路径（XPath/Range）是脆的。所以用**文本锚点**。

★★ 核心纪律：**不假定锚点在全文中唯一**（2026-10-31 修正）
  最初的实现是「按 text 找到第一处就 return，prefix/suffix 只在 text 找不到时才用」。
  这在正文重复时是错的：同一句话出现两次（常见的「股价不便宜」这种短语、并列段落、
  表格行），用户划的是第二处，高亮永远落在第一处；而 prefix/suffix 因为排在第 2、3
  顺位，几乎从不生效 —— 等于白存。
  现在改成：**枚举全文所有命中，逐个打分，取分最高的那个**。锚点因此扩成五个字段：
    · text    选中的那段文字
    · prefix  前文（32 字）
    · suffix  后文（32 字）
    · offset  选区起点在**全文归一化纯文本**里的字符下标（位置先验）
    · nth     同样文字在此之前出现过几次（序号先验）
  排序依据（见 `_locate` 的 score）：
    ① prefix/suffix 的实际重合长度最大（上下文一致性，最可信）
    ② nth 相同（序号一致）
    ③ |候选位置 - 记录的 offset| 最小（位置最近）
  三个先验互相备份：正文里插了新段落 → offset 漂了但上下文还在；同类短语换了位置 →
  上下文变了但序号/位置能兜。
  ★ 仍然不可能 100% 正确的场景（如实记下来，不假装解决）：**整段完全相同的重复**
    （例如同一个表格行出现两次），五个字段全一样时无从区分 —— 此时按「离原位置最近」
    落位，高亮会落在视觉上无法分辨的两处之一。这属于文本锚点的理论上限，不是实现缺陷。
  ★ 遗留锚点（只有 text/prefix/suffix 的老数据）继续可用，只是缺少 ①②③ 里的后两个先验，
    命中多处时退回「第一处」——对已有数据是最好选择，新数据不会再产生这种退化。

★ 匹配不到怎么办（这是设计的核心，不是 bug）：
  文本锚点是「尽力而为」的定位。正文变了、被截断、选中的文字被删掉，
  都可能匹配失败。匹配失败的标记**不报错、不丢**，它仍在底部列表里可见，
  只是正文里没有那层黄底 —— 这是优雅降级，不是"数据丢了"。

★ 安全边界：
  - 输入的 `text` 一定来自用户选区，渲染时先 html.unescape 掉正文里的实体再比对；
    绝不把锚点当 HTML 拼进去；
  - 包裹标签只带 class 和 data 属性，不含用户可控的任意属性。
"""
import html
import re

# 正文里包裹高亮的标签。class 在 CSS 里给底色；data-mark 让前端能反查/删除；
# data-note 是「hover 时显示的文字」（划线备注 / 批注正文），没有就不输出该属性。
# ★ data-note 必须转义：它来自用户输入，直接拼进属性会破坏 HTML（引号闭合）。
_OPEN = '<mark class="wiki-hl" data-mark="{}"{}>'
_OPEN_AN = '<mark class="wiki-hl wiki-hl-an" data-mark="{}"{}>'
_CLOSE = "</mark>"

# 跨块级标签的高亮会把 <mark> 拉开（浏览器会在 </p> 处自动闭合，第二段留下孤儿标签）。
# 所以候选区间里一旦含块级标签就不收 —— 用户跨段落选中本来也不是"一处连续高亮"。
_BLOCK_TAG = re.compile(
    r"</?(?:p|div|li|ul|ol|tr|td|th|tbody|thead|table|h[1-6]|blockquote|pre|br|hr"
    r"|section|article|figure|figcaption|dd|dt|dl)\b[^>]*>", re.I)

# 标签扫描（用于"区间配平"）。自闭合与空元素不入栈。
_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)\b[^>]*?(/?)>")
_VOID = {"br", "hr", "img", "input", "meta", "link", "source", "col", "area",
         "base", "embed", "param", "track", "wbr"}

# HTML 实体：正文里的 `&nbsp;` `&amp;` `&quot;` 必须先解回真字符才能和选区文字比。
_ENTITY = re.compile(r"&(?:#\d+|#[xX][0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);")

# 空白归一：浏览器选区拿到的是版面文字，HTML 里是源码换行/缩进。
# 两边都折成单个空格才能对齐（JS 侧 marksNorm 用同一条规则）。
_WS = re.compile(r"\s+")


def _note_attr(text: str) -> str:
    """把 hover 文字转成 ` data-note="…"`（无文字返回空串）。

    ★ 换行保留：批注常是多行，前端用 white-space: pre-wrap 还原。
      属性值里的裸换行是合法的，浏览器 getAttribute 会原样取回。
    """
    t = (text or "").strip()
    if not t:
        return ""
    return ' data-note="%s"' % html.escape(t, quote=True)


def _text_nodes(html_str: str):
    """把 HTML 切成「标签 / 文本」交替的 token 流，只 yield 文本 token 的区间。

    为什么不用 BeautifulSoup：这里**只有**「在文本里找一段、包一层」这一个需求，
    引一个解析库是纯技术债。用正则把标签和文本切开来遍历，足够且可控。
    """
    # 标签 vs 文本：把所有 <...> 当标签。正文是已经渲染过的 HTML（entry.content
    # 来自 Miniflux，或 mdrender 的输出），不会出现裸 `<` 混在文本里的情况。
    pos = 0
    for m in re.finditer(r"<[^>]*>", html_str):
        if m.start() > pos:
            yield pos, m.start()   # 文本区间 [pos, m.start())
        pos = m.end()
    if pos < len(html_str):
        yield pos, len(html_str)


def _stream(html_str: str):
    """把 HTML 摊平成「归一化纯文本 + 回指映射」。

    返回 (plain, starts, ends)：
      plain[i]      第 i 个归一化字符（连续空白折成一个空格）；
      starts[i]/ends[i]  该字符在 html_str 中的起止下标（右开区间）。

    ★ 为什么需要映射而不是直接在文本节点里找：
      1. 选区可能跨标签（`甲<b>乙</b>丙` 里选「甲乙丙」），按节点找永远失败；
      2. 要算「整篇里的第几处 / 离原位置多远」，必须有一条全文文本坐标轴。
    ★ 为什么要解实体：Miniflux 正文里 `&nbsp;` 遍地，用户看到的是一个空格、
      源码里是 6 个字符，不解就永远匹配不上（老实现的实际 bug）。
    """
    chars = []   # [(ch, html_start, html_end)]
    for s, e in _text_nodes(html_str):
        raw = html_str[s:e]
        pos = 0
        for m in _ENTITY.finditer(raw):
            if m.start() > pos:
                for i, ch in enumerate(raw[pos:m.start()]):
                    chars.append((ch, s + pos + i, s + pos + i + 1))
            decoded = html.unescape(m.group(0))
            if len(decoded) == 1:
                chars.append((decoded, s + m.start(), s + m.end()))
            else:
                # 一个实体解出多个字符（罕见）：首字符吃整段区间，其余退化成零宽，
                # 只为让边界落在实体外侧，不至于把标签切坏。
                chars.append((decoded[0], s + m.start(), s + m.end()))
                for ch in decoded[1:]:
                    chars.append((ch, s + m.end(), s + m.end()))
            pos = m.end()
        if pos < len(raw):
            for i, ch in enumerate(raw[pos:]):
                chars.append((ch, s + pos + i, s + pos + i + 1))

    plain, starts, ends = [], [], []
    for ch, hs, he in chars:
        if ch.isspace():
            if plain and plain[-1] == " ":
                ends[-1] = he          # 并入前一个空白 run（整段 run 共用一个空格）
                continue
            ch = " "
        plain.append(ch)
        starts.append(hs)
        ends.append(he)
    return "".join(plain), starts, ends


def _occurrences(plain: str, needle: str):
    """needle 在 plain 里的**全部**起点（允许重叠，如 "aaa" 找 "aa" 有两处）。"""
    out = []
    i = plain.find(needle)
    while i >= 0:
        out.append(i)
        i = plain.find(needle, i + 1)
    return out


def _common_suffix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[-1 - i] == b[-1 - i]:
        i += 1
    return i


def _common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _elements(html_str: str):
    """扫出「成对出现的元素」区间：[(open_start, open_end, close_start, close_end, name)]。

    只用来做**区间配平**（见 `_balance`），不追求完整解析：自闭合、空元素、
    没闭合的标签一律跳过。
    """
    stack, out = [], []
    for m in _TAG.finditer(html_str):
        closing, name, selfclose = m.group(1) == "/", m.group(2).lower(), m.group(3) == "/"
        if name in _VOID or selfclose:
            continue
        if not closing:
            stack.append((name, m.start(), m.end()))
            continue
        for i in range(len(stack) - 1, -1, -1):
            if stack[i][0] == name:
                out.append((stack[i][1], stack[i][2], m.start(), m.end(), name))
                del stack[i:]
                break
    return out


def _balance(html_str: str, elements, hs: int, he: int):
    """把 [hs, he) 扩到「标签配平」，返回新的 (hs, he)；配不平返回 None。

    ★★ 为什么必须做（2026-10-31 实测踩到）：
      正文里 `**江山时序**：纯原生 JS…` 渲染成 `<strong>江山时序</strong>：纯原生…`。
      用户从「江」选到「地图」，区间起点落在 `<strong>` **内部**、终点在外面，于是直接
      拼接得到 `<strong><mark>江山时序</strong>：…地图</mark>` —— 非法嵌套。浏览器按
      HTML 的收养算法自行修复：遇到 `</strong>` 就把 `<mark>` 强行闭合，末尾的 `</mark>`
      成了孤儿被丢弃。**结果只高亮了「江山时序」**，用户看到的就是"我选的整句只亮了开头"。
      这不是 CSS 或前端的问题，是字符串拼接必然踩的坑，只能在生成时配平。
    ★ 处理方向：开标签在区间外、闭标签在区间内 → 起点回退到那个开标签之前；反过来
      （区间内开、区间外闭）→ 终点推到那个闭标签之后。视觉上会多吃一两个字符，
      但比"只亮一半"和"整条不亮"都好。
    """
    for _ in range(32):
        changed = False
        for os_, _oe, cs, ce, _name in elements:
            if os_ < hs and cs >= hs and ce <= he:
                hs, changed = os_, True          # 开在区间外、关在区间内
            elif hs <= os_ < he and ce > he:
                he, changed = ce, True           # 开在区间内、关在区间外
        if not changed:
            return hs, he
        if _BLOCK_TAG.search(html_str, hs, he):
            return None                          # 一旦扩成跨块，宁可不显示
    return None                                  # 32 轮还没配平，说明结构异常，放弃


def _locate(html_str: str, stream, anchor: dict, elements=None):
    """在整篇正文里给锚点找位置，返回 (start, end) 或 None。

    ★ 不假定唯一：枚举全部命中，按 (上下文重合度, 序号一致, 位置接近度) 排序取最优。
    """
    plain, starts, ends = stream
    needle = anchor.get("text") or ""
    if not needle:
        return None
    occ = _occurrences(plain, needle)
    if not occ:
        # 老数据/手工传进来的 text 可能带首尾空白，退一步去掉再试（offset 只影响排序先验）
        stripped = needle.strip()
        if stripped and stripped != needle:
            needle = stripped
            occ = _occurrences(plain, needle)
    if not occ:
        return None

    prefix = (anchor.get("prefix") or "").strip()
    suffix = (anchor.get("suffix") or "").strip()
    off = anchor.get("offset")
    off = int(off) if isinstance(off, (int, float)) else None
    nth = anchor.get("nth")
    nth = int(nth) if isinstance(nth, (int, float)) else None

    best = None
    for idx, a in enumerate(occ):
        b = a + len(needle)
        hs, he = starts[a], ends[b - 1]
        if _BLOCK_TAG.search(html_str, hs, he):
            continue                     # 跨块级标签，包不住，换下一个候选
        if elements:
            # ★ 行内标签嵌套不平衡也要修：`<strong><mark>江…</strong>…</mark>` 这种
            #   非法嵌套会被浏览器从 `</strong>` 处截断，用户只看到半句高亮。
            fixed = _balance(html_str, elements, hs, he)
            if fixed is None:
                continue
            hs, he = fixed

        ctx = 0
        if prefix:
            ctx += _common_suffix_len(plain[max(0, a - len(prefix)):a], prefix)
        if suffix:
            ctx += _common_prefix_len(plain[b:b + len(suffix)], suffix)
        score = (
            ctx,                                     # ① 上下文重合字数
            1 if (nth is not None and nth == idx) else 0,   # ② 序号是否就是当初那个
            -(abs(a - off) if off is not None else 0),      # ③ 离原位置越近越好
        )
        if best is None or score > best[0]:
            best = (score, hs, he, a, idx)
    if best is None:
        return None
    return best[1], best[2], best[3], best[4]


def _apply_mark(html_str: str, start: int, end: int, mark_id, kind: str, text: str = "") -> str:
    """在 [start, end) 处包一层高亮标签（纯字符拼接，start/end 必须落在文本区内）。"""
    tag = _OPEN_AN if kind == "annotation" else _OPEN
    return (html_str[:start] + tag.format(mark_id, _note_attr(text)) + html_str[start:end]
            + _CLOSE + html_str[end:])


def apply_marks(html_str: str, marks, undisplayed=None, resolved=None) -> str:
    """把一组划线/批注回填成 inline 高亮。

    marks: [{"id", "kind", "anchor", "text"}]，kind ∈ {"highlight", "annotation"}，
    text 是 hover 要显示的文字（划线备注 / 批注正文），可空。
    ★ 多个标记按「出现位置」从后往前套，避免改前面的 offset 影响后面的定位。
    ★ 匹配失败（anchor 缺失或定位不到）→ 跳过。
    ★ 区间重叠只留先出现的那条：两条高亮套在同一段文字上会互相嵌套、DOM 变脏
      （删一条时另一条的选择器也会跟着崩）。被挤掉的那条进 undisplayed，底部仍可改删。

    undisplayed: 可选的 list，函数往里 append **没能在正文里落位**的标记 id。
      ★ 这是"底部列表为什么不能直接删掉"的答案：正文里没落位的标记，页面上唯一
        还能看见/改/删的地方就是那个列表。不报错的静默降级必须有出口，
        否则数据还在库里、用户却完全看不到（这比报错更贵）。

    resolved: 可选的 list，往里 append **旧锚点补全信息** {"id","kind","offset","nth"} ——
      只针对「这次成功落位、但锚点里没有 offset」的条目（即三字段的遗留数据）。
      存储层（core.marks.annotate_html）拿它把位置先验一次性补齐，老标记自此和新标记
      一样具备消歧能力。★ 本模块**不碰数据库**，只报告"我把它落在哪了"。
    """
    if not marks:
        return html_str
    stream = _stream(html_str)          # ★ 全文只摊平一次：每条标记都重建是 O(n·m)
    elements = _elements(html_str)      # 同上：配平用的元素区间表也只扫一遍
    spans = []
    pending = []                        # (id, kind, plain_offset, nth)，等确定真的落位了再认领
    for mk in marks:
        anchor = mk.get("anchor")
        if not isinstance(anchor, dict):
            if undisplayed is not None:
                undisplayed.append(mk.get("id"))
            continue
        span = _locate(html_str, stream, anchor, elements)
        if span is None:
            if undisplayed is not None:
                undisplayed.append(mk.get("id"))
            continue
        if resolved is not None and "offset" not in anchor:
            # 落位用的是 _locate 选出来的那一处（不是"全文第一次出现"），补齐后
            # 老标记的 offset/nth 与它**现在**显示的位置自洽，下次正文再冒出重复也能分清。
            # ★ 先挂 pending：被下面的重叠判定挤掉的标记不该补位置（它压根没显示）。
            pending.append((mk["id"], mk["kind"], span[2], span[3]))
        spans.append((span[0], span[1], mk["id"], mk["kind"], mk.get("text") or ""))

    # 先按位置排序，再剔除互相重叠的（后到者让位），最后从后往前套
    spans.sort(key=lambda x: (x[0], x[1]))
    kept = []
    for sp in spans:
        if kept and sp[0] < kept[-1][1]:
            if undisplayed is not None:
                undisplayed.append(sp[2])
            continue
        kept.append(sp)
    if resolved is not None and pending:
        shown = {sp[2] for sp in kept}
        for mid, kind, off, nth in pending:
            if mid in shown:
                resolved.append({"id": mid, "kind": kind, "offset": off, "nth": nth})

    out = html_str
    for start, end, mid, kind, text in reversed(kept):
        out = _apply_mark(out, start, end, mid, kind, text)
    return out
