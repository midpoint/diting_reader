"""极简 Markdown → HTML（只为我们自己生成的日报服务）。

为什么不装第三方 markdown 库：
  1. 报告是**我们自己拼的**（头部是代码拼的，正文来自模型），只需要覆盖
     标题 / 列表 / 引用 / 粗体 / 链接 / 代码块 / 表格 这几样；
  2. 依赖越少越好（离线 + 无供应链风险），而且这个渲染器**先 escape 再解析**，
     天生没有 XSS 面 —— 模型就算输出 `<script>` 也只会显示成文本。

安全前提：**先 html.escape，再套标签**，且链接只放行 http/https/相对路径。
"""
import html
import re

# ★ 行级语法（标题/列表/引用/表格/链接）统一在 core.mdlines 定义 ——
#   渲染端只引用，改语法只改一处。
from core.mdlines import BQ as _BQ, HEAD as _HEAD, HR as _HR
from core.mdlines import LINK as _LINK, OL as _OL, TABLE_SEP as _TABLE_SEP
from core.mdlines import UL as _UL, WIKILINK as _WIKILINK
from core.mdlines import cells as _cells, wikilink_parts as _wikilink_parts

_CODE = re.compile(r"```[a-zA-Z0-9_+-]*\n(.*?)```", re.S)
#: 只为取有序列表的**起始编号**用（`<ol start="3">`）。
#: ★ 不并进 mdlines.OL：那边的 group(1) 是缩进、group(2) 是内容，
#:   加一个捕获组就会把引用方的组号一起改掉。
_OLNUM = re.compile(r"^\s*(\d+)[.)]\s+")


def _collect(re_, lines: list, i: int, n: int) -> tuple:
    """收集一个列表的全部项，**允许项之间夹空行**（松散列表）。

    ★ 为什么必须跨空行（2026-10-31 真实故障）：模型几乎总是「每项之间空一行」地
      输出有序列表，而这里原来只吃**连续**的 `1.` 行 —— 于是 5 条要点被渲染成
      5 个独立的 `<ol>`，浏览器每个都从 1 开始编号，页面上每一行都写着「1.」。
      CommonMark 里空行只是让列表「变松散」，并不结束列表。
    ★ 空行之后必须是**同类**标记才继续：`1.` 后面接 `- ` 那是另一个列表，不是
      同一条（跨类型续接会把两种语义揉成一个 ol）。
    """
    items = []
    while i < n:
        m = re_.match(lines[i])
        if m:
            items.append(m.group(2))
            i += 1
            continue
        if not lines[i].strip():
            j = i
            while j < n and not lines[j].strip():
                j += 1
            if j < n and re_.match(lines[j]):
                i = j
                continue
        break
    return items, i


def _inline(s: str, wikilinks=None) -> str:
    """行内元素。★ 先 escape 再套标签 —— 模型输出 `<script>` 也只会显示成文本。

    `wikilinks`（可选）：`callable(slug, alias) -> dict | None`，只给 wiki 正文传。
    返回 `{"href", "label", "missing", "why"}` 由这里 escape 后套标签；
    返回 `None` 表示保持字面 `[[...]]`。日报 / 划线正文不传 ⇒ 行为一字不变。
    ★ 放在所有 inline 规则**最后**：生成的 `<a>` 不能再被粗体/代码规则碰一次。
    """
    s = html.escape(s or "", quote=False)

    def _link(m):
        label = m.group(1)
        href = html.escape(html.unescape(m.group(2)), quote=True)
        return f'<a href="{href}" target="_blank" rel="noopener noreferrer">{label}</a>'

    s = _LINK.sub(_link, s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", s)

    if wikilinks is not None:
        def _wl(m):
            # ★ s 已经 escape 过：slug / 别名要先还原成原文再去查表，
            #   否则含 `&` 的 slug 会拿 `&amp;` 去匹配，永远查不到。
            parts = _wikilink_parts(m)
            slug, alias = html.unescape(parts[0]), html.unescape(parts[1])
            info = wikilinks(slug, alias)
            if not info:
                return m.group(0)
            label = html.escape(info.get("label") or slug, quote=False)
            missing = bool(info.get("missing"))
            tip = html.escape(
                ((info.get("why") or "内链") + "：" + slug) if missing else slug,
                quote=True)
            if missing:
                # 断链**不做成 <a>**：链过去就是 404，比不可点更糟。
                # 保留字面形态 + 可见说明（详情页面板另有一句文字提示）。
                return ('<span class="wikilink is-missing" title="%s">[[%s]]</span>'
                        % (tip, label))
            href = html.escape(info.get("href") or "", quote=True)
            # 最后一道闸：只放行站内相对路径，resolver 万一拼了外链也不带进来。
            if not href.startswith("/"):
                return m.group(0)
            return '<a class="wikilink" href="%s" title="%s">%s</a>' % (
                href, tip, label)

        s = _WIKILINK.sub(_wl, s)
    return s


def render(text: str, *, wikilinks=None) -> str:
    """渲染 markdown。

    `wikilinks` 只给 wiki 正文用（`core.wiki.wikilink_resolver()`）：把
    `[[slug]]` 变成指向已入库页的站内链接。**不传 = 完全不认识内链**，
    日报 / 划线 / 页面规范示例里的字面 `[[...]]` 保持原样。
    """
    src = (text or "").replace("\r\n", "\n")

    def inline(s):
        return _inline(s, wikilinks)

    stash = []

    def _keep(m):
        stash.append(m.group(1))
        return f"\x00CODE{len(stash) - 1}\x00"

    src = _CODE.sub(_keep, src)

    # ★ 这里**不能**对整体做 html.escape：`> ` 会被转成 `&gt; `，
    #   引用块从此再也匹配不上（踩过）。escape 一律推迟到 _inline 里做。
    lines = src.split("\n")
    out: list[str] = []
    i, n = 0, len(lines)

    def flush_para(buf):
        if buf:
            out.append("<p>" + inline(" ".join(buf)) + "</p>")
            buf.clear()

    para: list[str] = []
    while i < n:
        line = lines[i]

        if not line.strip():
            flush_para(para)
            i += 1
            continue

        m = _HEAD.match(line)
        if m:
            flush_para(para)
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{inline(m.group(2).strip())}</h{lvl}>")
            i += 1
            continue

        if _HR.match(line):
            flush_para(para)
            out.append("<hr>")
            i += 1
            continue

        # 表格：当前行有 | 且下一行是分隔行
        if "|" in line and i + 1 < n and _TABLE_SEP.match(lines[i + 1]):
            flush_para(para)
            head = _cells(line)
            i += 2
            body = []
            while i < n and "|" in lines[i] and lines[i].strip():
                body.append(_cells(lines[i]))
                i += 1
            th = "".join(f"<th>{inline(c)}</th>" for c in head)
            rows = "".join(
                "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>"
                for r in body)
            out.append(f'<table class="mdt"><thead><tr>{th}</tr></thead>'
                       f"<tbody>{rows}</tbody></table>")
            continue

        if _UL.match(line):
            flush_para(para)
            items, i = _collect(_UL, lines, i, n)
            out.append("<ul>" + "".join(f"<li>{inline(t)}</li>" for t in items)
                       + "</ul>")
            continue

        if _OL.match(line):
            flush_para(para)
            # 起始编号：模型常从 3. 开始续写（前一段说了 1.2.），HTML 默认从 1 数起
            num = _OLNUM.match(line)
            start = int(num.group(1)) if num else 1
            if start < 1 or start > 9999:
                start = 1
            head = "" if start == 1 else f' start="{start}"'
            items, i = _collect(_OL, lines, i, n)
            out.append(f"<ol{head}>"
                       + "".join(f"<li>{inline(t)}</li>" for t in items)
                       + "</ol>")
            continue

        m = _BQ.match(line)
        if m:
            flush_para(para)
            buf = []
            while i < n:
                mm = _BQ.match(lines[i])
                if not mm:
                    break
                buf.append(mm.group(1))
                i += 1
            out.append("<blockquote>" + inline(" ".join(buf)) + "</blockquote>")
            continue

        m = re.match(r"^\x00CODE(\d+)\x00$", line.strip())
        if m:
            flush_para(para)
            out.append(f"<pre><code>{html.escape(stash[int(m.group(1))])}</code></pre>")
            i += 1
            continue

        para.append(line.strip())
        i += 1

    flush_para(para)
    return "\n".join(out)
