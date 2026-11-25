"""日报 markdown 的行级语法（唯一一份定义）。

`core/mdrender.py` 是日报的渲染端（→ HTML）。行级语法曾经在两个渲染端各抄一套
正则、靠注释约定「两边一起改」，报告语法一旦演化就会出现「这边看着对、那边不对」，
且只能在成品里肉眼发现。第二个渲染端已经没了，但「语法只定义一次」这条纪律保留：
以后再加渲染端（比如导出 PDF）依然只认这里。

规则只在这里定义；渲染器只做自己的输出端（escape 顺序、块合并策略等
输出侧差异仍归各自模块）。
"""
import re

#: 标题 `#`..`######`（group(1)=井号数, group(2)=文本）
HEAD = re.compile(r"^(#{1,6})\s+(.*)$")
#: 无序列表（group(1)=缩进, group(2)=内容）—— 缩进层数决定嵌套
UL = re.compile(r"^(\s*)[-*+]\s+(.*)$")
#: 有序列表 `1.` / `1)`
OL = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
#: 引用块（连续多行合成一块）
BQ = re.compile(r"^>\s?(.*)$")
#: 水平分割线
HR = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
#: 表格分隔行（`| --- | --- |`）
TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")
#: 链接。★ 只匹配**安全**的 scheme —— 不安全的写法原样留着当普通文本显示，
#:   比"认出链接再丢弃"更不容易让读者莫名丢字。
LINK = re.compile(
    r"\[([^\]]+)\]\((https?://[^\s)]+|/[^\s)]*|#[^\s)]*|mailto:[^\s)]*)\)", re.I)

#: wiki 内链 `[[slug]]` / `[[slug|显示名]]`。★ **全项目唯一一份**：
#:   `core.wiki` 的反链索引 / 断链守卫 / `wiki_loop` 的提示词产物，
#:   `core.mdrender` 的阅读态渲染，都从这里引用。
#:   group(1) 是 slug（别名不进捕获组 —— `wiki_loop` 用 `findall()` 直接拿
#:   slug 列表，多一个捕获组就会变成 tuple 列表，悄悄改掉返回值形状）。
WIKILINK = re.compile(r"\[\[([^\]\|]+?)(?:\|[^\]]*)?\]\]")


def wikilink_parts(m) -> tuple:
    """`WIKILINK` 的 match → `(slug, 显示名)`；没写别名时显示名是空串。

    ★ 别名只能这样拆一次：反链索引看 `group(1)`，阅读态要把 `|显示名` 展示出来，
      两处各写一遍 `split("|")` 迟早漂。原先 `wiki._wikilink_display()` 里的那份
      已改为调这里。
    """
    raw = m.group(0)[2:-2]
    if "|" in raw:
        slug, _, alias = raw.partition("|")
        return slug.strip(), alias.strip()
    return raw.strip(), ""


def cells(row: str) -> list:
    """`| a | b |` → ["a", "b"]（首尾竖线剥掉，逐格 strip）。"""
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [c.strip() for c in row.split("|")]
