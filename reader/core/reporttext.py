"""日报的纯文本层：素材装配 + 引用短名 + 质量审计（全部纯函数）。

从 core/summary.py 抽出（2026-10-24）：这一层不碰数据源 / 数据库 / LLM ——
输入是字符串与数据类，输出是字符串与 dict，因而可以被单测直接覆盖
（core/tests/test_summary_invariants.py），也可以被别的链路复用。
summary.py 只留编排：分层压缩、运行状态、容错与页面数据。

各函数的口径注释（为什么这么判、踩过什么坑）随函数原样保留 —— 那些是
不变量的一部分，不是废话。
"""
import os
import re

from django.conf import settings

from core.mdlines import WIKILINK as _WIKILINK, wikilink_parts as _wikilink_parts
from core.textutil import (split_blockquotes, split_rsshub_quote,
                           strip_boilerplate, to_plain)


_RE_REPOST = re.compile(r"^转(?:自[^：:]{0,10})?[：:]\s*")
_RE_REPLY = re.compile(r"^(?:回复@|回复\s*@|@[^\s:：]{1,20}\s*[:：])")
# ★ 英文转推（Twitter 的 `RT @user: 原文`）。上面两条是中文站的口径，
#   认不出它 —— 与引用转发同属"`classify_kind` 的英文盲区"。
_RE_RETWEET = re.compile(r"^RT\s+@[A-Za-z0-9_]{1,20}\s*[:：]")


def _quote(feed_id, published, url, name=None) -> str:
    """引用行：`[源名 · HH:MM](url)`（无源名时退回 `[#id · HH:MM]`）。

    ★ 2026-10-16 曾把源名从引用里删掉（当时是为了压掉文末那段 90 条的引用索引：
      「#29 · Twitter @Alex Chen」重复十几次又长又重复；索引本身已移除）。
    ★★ 用户读日报时提出的问题把它请了回来，但**换了个形态**：
      「总结的内容希望知道是哪个 feed 的观点，比如是甲源的还是乙源的」。
      一条要点末尾只写 `[#2 · 08:53]`，读者得回树上查 #2 是谁 —— 引用可点，
      但**观点归属不可读**。所以引用改带**短源名**（见 `feed_name`）：
      `[甲源 · 08:53](url)`。源 ID 不在引用里出现，而由
      「数量统计」的按源表 `源名 (#id)` 提供对应关系 —— 既短又不丢可追溯性。
    ★ 时间只留时分，日期由报告窗口决定。
    """
    ts = f"{published:%H:%M}" if published else ""
    label = name or f"#{feed_id}"
    if ts:
        label = f"{label} · {ts}"
    return f"[{label}]({url})"


#: 源名瘦身规则（按顺序应用）。目的：让引用里的名字**认得出来**又不太占地方。
_NAME_RULES = (
    (re.compile(r"\s*的\s*雪球全部动态\s*$"), ""),      # 甲源 的雪球全部动态 → 甲源
    (re.compile(r"^Twitter\s*@"), ""),                  # Twitter @Alex Chen → Alex Chen
    (re.compile(r"\s+[|｜]\s+.*$"), ""),                # A | NVIDIA Blog → A
    (re.compile(r"\s+[-–—]\s+.*$"), ""),                # 票房站 - 正在热映 - 完整版 → 票房站
    (re.compile(r"\s*的\s*bilibili\s*空间\s*$", re.I), ""),
)
#: 名字上限：超过就截断。★ 18 是"能放下 Anthropic Research"的最短长度；
#:   截断时英文按**词边界**切，不然会得到「Anthropic Re」这种断词。
NAME_MAX = 18


def _cut(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    if re.fullmatch(r"[\w\s.@&'\-]+", s, re.A):        # 纯 ASCII：按词切
        head = s[:limit]
        if " " in head:
            head = head.rsplit(" ", 1)[0]
        return head.strip() or s[:limit]
    return s[:limit]


def feed_name(title: str, feed_id=None, limit: int = NAME_MAX) -> str:
    """源标题 → 引用里用的短名（改不动的标题原样返回，截断到 `limit`）。"""
    s = (title or "").strip()
    for pat, rep in _NAME_RULES:
        s = pat.sub(rep, s).strip()
    if not s:
        return f"#{feed_id}" if feed_id is not None else ""
    return _cut(s, limit)


def _name_map(meta: dict) -> dict:
    """{feed_id: 短名}。★ 重名要区分，否则引用分不清是哪个源。

    重名的处理不是简单加 `#id`（「票房站#48」读者看不懂），而是：
    取这组标题的**公共前缀**，把各自剩下的那截当区分词 ——
    「票房站 - 即将上映」/「票房站 - 正在热映 - 完整版」
    → 「票房站 · 即将上映」/「票房站 · 正在热映」。
    """
    short = {fid: feed_name(m.get("title") or "", fid) for fid, m in meta.items()}
    groups = {}
    for fid, n in short.items():
        groups.setdefault(n, []).append(fid)
    out = dict(short)
    for n, fids in groups.items():
        if len(fids) == 1:
            continue
        titles = [(meta[fid].get("title") or "").strip() for fid in fids]
        pre = os.path.commonprefix(titles) if len(titles) > 1 else ""
        pre = re.sub(r"[\s\-–—|｜·/]+$", "", pre)
        for fid, t in zip(fids, titles):
            tail = re.sub(r"^[\s\-–—|｜·/]+", "", t[len(pre):])
            tail = feed_name(tail, fid) if tail else ""
            out[fid] = _cut(f"{n} · {tail}", NAME_MAX + 6) if tail else _cut(t, NAME_MAX + 6)
    groups2 = {}
    for fid, n in out.items():
        groups2.setdefault(n, []).append(fid)
    for n, fids in groups2.items():
        if len(fids) > 1:
            for fid in fids:
                out[fid] = f"{n}#{fid}"
    return out


def classify_kind(title: str, text: str, is_quote: bool = False) -> str:
    """规则层判定：normal / title_only / quote / reply / repost。

    ★ 「降级」= 把**正文压成一句话**，和「降级别」一点关系都没有 ——
      L0～L3 是分级（triage）的事，这里一行级别都不动。
    零成本先压一层，因为实测真正有信息增量的条目不到两成，而这几类的正文
    长度基本等于噪声：

      title_only  正文为空   → 只留标题 + 引用行，正文位置写「（标题流源，无正文）」
      quote       引用转发   → 「本人文字 + 引文前 N 字」（**在 build_items 里已压好**）
      reply       互动回复   → 只留前 200 字（`_render_item`）
      repost      转贴       → 只留前 120 字
      normal      其余       → 保留 condense() 后的正文（≤ 1200 字）

    这几类也因此**不进 S3 单篇压缩**（已经是"一句话"了，没必要再花一次调用），
    但仍以一句话的形式参与 S2/S1/S0 的汇总。总调用量因此降到全量不分层的约 1/3。

    ★ `is_quote` 由调用方从**原始 HTML** 判定（`split_rsshub_quote`）后传入：
      走到这里时正文已经过 `to_plain`，`<hr/>` 与 `rsshub-quote` 早被抹掉了。
      ★★ 中文的 `_RE_REPLY` / `_RE_REPOST` 认不出英文推文，所以 Twitter 源的
      引用转发曾经**一条都压不下来**（实测 70/70 判 normal、压缩比 100%），
      而它恰恰是"本人 21% + 被引用方 79%"的复合正文 —— 这才是 S2 被撑爆的根因。
      ★ 普通正文里的 `<blockquote>` **不**设 is_quote：那往往只是长文里的一段引用
      （README 的 Note、新闻里的原话），按整条引用转发处理会连 S3 压缩一起丢掉。
      它由 `build_items` 走 `annotate_quotes` 只标边界。
    """
    body = (text or "").strip()
    head = (title or "").strip()
    if not body:
        return "title_only"
    if is_quote:
        return "quote"
    if _RE_RETWEET.match(head) or _RE_RETWEET.match(body):
        return "repost"
    if _RE_REPLY.match(head) or _RE_REPLY.match(body):
        return "reply"
    if _RE_REPOST.match(head) and len(body) <= max(len(head) * 2, 60):
        return "repost"
    if _RE_REPOST.match(body) and len(body) <= 120:
        return "repost"
    return "normal"


def condense(text: str, limit: int) -> tuple:
    """长文截断：保留开头 70% + 结尾 30%，并**显式标注**省略字数。

    不标的话模型会把截断后的残缺正文当成完整内容来总结。
    """
    text = text or ""
    if limit <= 0 or len(text) <= limit:
        return text, False
    head = int(limit * 0.7)
    tail = limit - head
    gap = (f"\n…[此处省略 {len(text) - limit} 字，素材阶段已截断；"
           f"如需细节请读原文]…\n")
    return text[:head] + gap + text[-tail:], True


#: 微博 / 雪球 / 推特的转发分隔符：`//@某人：原帖`。`//` 后面那段是**别人的原文**，
#: 前面才是本人的话。和 RSSHub 的 `<hr>` 是同一件事的两种长相 —— 后者按块分，
#: 这个按行内符号分。
_RE_FORWARD = re.compile(r"\s*//\s*@[^\s:：]{1,30}\s*[:：]")


def mark_forward_quote(text: str) -> str:
    """`本人回复 //@某人：原帖` → 只给后半段套【引文】，前半段保持原样。

    ★ 为什么必须处理（2026-11-03 真实数据）：雪球一条「回复@A: 嗯嗯，明天卖点
      Google 的 put。//@A: 回复@B: 谷歌出财报了……」—— 本人只回了一句，后面
      一大段是别人写的。不切的话模型看到的是平铺的一串，把「谷歌出财报了」
      算到本人头上，claim 就写成了「财报后卖看跌期权建仓」（「财报后」来自引文，
      不是他说的）。用户的原话：「引文内容较多，我更关心的是作者的回复而非引文。」
    ★ 切开后**后半段整段**算引文，连它内部再嵌的 `<blockquote>` 一起 —— 反正
      都是别人的话，套一层就够，不做嵌套（嵌套只会让模型更难读）。
    ★ 只在**第一个** `//@` 处切：后面可能还有 `//@` 层层转发，都归引文那一侧。
    """
    m = _RE_FORWARD.search(text or "")
    if not m:
        return text
    head = (text[:m.start()] or "").rstrip()
    tail = (text[m.end():] or "").strip()
    if not tail:
        return head
    return head + ("\n" if head else "") + f"【引文】{tail}【/引文】"


def annotate_quotes(raw: str) -> str:
    """正文 HTML → 素材纯文本：给引用块加上【引文】边界。

    ★ 为什么不能直接 `to_plain`：它会把 `<blockquote>` 抹成空格，博主的话和
      跟帖/被引原文变成一串平铺的句子。模型看不出边界，就会把别人的假设
      当成博主判断（2026-11-02 实测：一条「都有可能，难在知道是谁」的回复，
      `<blockquote>` 里提问者的「应用公司而非基建」被写成了博主的观点，
      S0 又升级成「应用公司才是 AI 最大受益者」）。
    ★ 只加边界，**不截断、不降 kind**：引用块可能只是长文里的一段
      （README 的 Note、新闻里引用的原话），按"整条引用转发"处理会连 S3
      压缩一起丢掉，反而损失信息。截断只留给 RSSHub 那种整条都是引文的结构。
    ★ 标记是中性的「引文」而不是「他人观点」：README 的 Note 也是引文，
      断言"他人"会错；是不是某人的判断，交给 prompt 结合「名字:」判断。
    """
    parts = []
    for is_quote, seg in split_blockquotes(raw or ""):
        text = to_plain(seg).strip()
        if not text:
            continue
        # 块级引文直接包；非引文段里可能还藏着 `//@某人：` 的行内转发，
        # 交给 mark_forward_quote 再切一刀（两条判据在同一处收口）。
        parts.append(f"【引文】{text}【/引文】" if is_quote else mark_forward_quote(text))
    return "\n".join(parts)


_RE_QUOTE_BLOCK = re.compile(r"【引文】.*?【/引文】", re.S)


def unquoted(text: str) -> str:
    """把正文【引文】段整段去掉 —— 「作者自己说的话」才是这一篇的内容。

    ★ 只有一个调用方，但非有不可：force 提炼（用户点「发送到 wiki」）允许
      quote 来自正文，守卫拿 `blk["text"]` 做 hay。可 text 里含引文，
      于是**被引者的原话同样能通过守卫**，还被标成「正文事实」——
      这正是「从引文里提炼候选」的最后一道口子（2026-11-03 那条
      「财报后卖看跌期权建仓」就是 force 提炼来的，靠的正是引文里的「谷歌出财报了」）。
    ★ 去掉之后只剩作者那几句：转发帖可能一句不剩，那就是「这篇没有作者内容」，
      拒掉是对的，不该拿别人的话凑数。
    ★★ 去掉的只是**归属资格**，不是上下文：模型看到的材料始终是完整的
      `body_text(blk)`（引文全在、带边界）。本函数只服务守卫的比对底本，
      不进 prompt —— 别把「不能当作者的判断」误读成「读者不该看到」。
    """
    return _RE_QUOTE_BLOCK.sub("", text or "").strip()


def quoted(text: str) -> str:
    """只留【引文】段（别人说的话），带边界标记。

    ★ 用途单一：让守卫把「摘错层」和「纯编造」分开报。
      「这句在引文里」= 模型读懂了、只是它没资格当作者的判断；
      「这句哪儿都找不到」= 模型编的。混成一句话，人就分不清模型是笨还是坏，
      也不知道下一步该干什么（前者其实只要去划一条线就合法了）。
    """
    return "\n".join(_RE_QUOTE_BLOCK.findall(text or ""))


def clip_quotes(text: str, limit: int) -> str:
    """按字数截断，但**不让引文边界断在半截**。

    ★ 实测（2026-11-03，600 条真实条目，`WIKI_INGEST_ARTICLE_CHARS=4000`）：
      50 条被截，其中 1 条（entry 6905，全长 4029）正好切在第二段【引文】中间，
      模型收到一个开了不闭合的边界 —— 知道「后面是别人说的」，但不知道在哪结束，
      于是把剩余部分整段算进引文（或反过来）。
    ★ 只在未闭合时补一个【/引文】，**不倒退切点**：倒退会白丢已经读到的正文，
      补闭合只多 6 个字符。宁可多给一点，不要少给。
    ★ 没超长就原样返回，不碰内容 —— 截断这件事只在真的超长时才发生。
    """
    raw = text or ""
    if limit <= 0:
        return ""
    if len(raw) <= limit:
        return raw
    seg = raw[:limit]
    if seg.count("【引文】") > seg.count("【/引文】"):
        seg += "【/引文】"
    return seg


def body_text(blk: dict) -> str:
    """`sourceblock.load()` 的结果 → 喂给模型的正文（引用块带【引文】边界）。

    ★ 三条链路收口在这一个判据上：阅读时对话（`conversation._material`）和
      wiki 候选提炼（`ingest._material`）直接调本函数；日报/周报的 `build_items`
      拿的是 `entry.content`（HTML 字符串、不是 blk dict），直接调 `annotate_quotes`
      —— 那是本函数的内部实现，两种引文（`<blockquote>` 与 `//@`）都在里面，
      所以不会漂。**2026-11-02 的教训：同一个判据写三遍，就一定会有一遍漏掉，
      而且漏掉的那遍不报错** —— 当时只修了 `ingest._material`，
      `conversation._material` 拿 `blk["text"]` 一路错到 2026-11-03 用户再次提出。

    ★ 切 `html` 而不是 `text`：只有 html 还留着 `<blockquote>`；对 text 调
      `split_blockquotes` 永远返回「一段、不标注」，等于没修（实测过）。

    ★ 没有 `<blockquote>` 时原样返回 `text`，不绕 `to_plain` 一趟：多绕一步
      只会让换行/空白与旧行为出现无谓差异，而没引用的正文根本没有归属问题。
      也没有 html 时（部分源只有 text）同样退回 text —— 宁可少标边界，
      不能把正文整段丢掉。
    """
    raw_html = blk.get("html") or ""
    if "<blockquote" in raw_html.lower():
        text = annotate_quotes(raw_html)
        if text.strip():
            return text
    # ★ 没有块级引文，不等于没有引文：雪球/微博那种 `//@某人：` 是行内转发，
    #   `<blockquote>` 里根本没有它。不在这里补一刀，这类源就等于没修。
    return mark_forward_quote(blk.get("text") or "")


# ==================================================================== 待办装配
def build_items(entries, ratings: dict, meta: dict) -> list:
    """把 Entry → 素材 dict；跳过分级 L0（已读、fail-open、未评级且源默认 L0 都不进总结）。

    meta: {feed_id: {"title":…, "category_id":…, "category_title":…,
                     "default_level":…}}
    ★ 未评级条目按该 feed 的 default_level 算级别（缺省全局 L2），不再写死 L2。
    """
    items = []
    names = _name_map(meta)
    for e in entries:
        r = ratings.get(e.id)
        # ★ 分级 L0 不进总结：fail_open 的 L0 也不进（设计 §2 #13）
        if r is not None and r.level == 0:
            continue
        m = meta.get(e.feed_id) or {}
        feed_default = m.get("default_level", settings.UNRATED_LEVEL)
        feed_default = (int(feed_default) if feed_default is not None
                        else int(settings.UNRATED_LEVEL))
        # ★ 未评级也按 feed 默认级别处理：默认 L0 的源，未评级条目同样不进日报。
        if r is None and feed_default == 0:
            continue
        published = e.published_at or e.created_at
        head_html, tail_html, is_quote = split_rsshub_quote(e.content or "")
        if is_quote:
            # ★ 引用转发的正文主体是**被引用方**写的，不是他的话。实测 feed 29 昨天那 34 条：
            #   本人 1960 字、被引用方 7225 字（79%）—— 原样灌进 prompt 等于让模型读一堆
            #   他没说过的话，S2 的预算就是这么被撑爆的。只留他的点评 + 引文的"提子"。
            head, tail = to_plain(head_html), to_plain(tail_html)
            tail = tail[:settings.SUMMARY_QUOTE_CHARS]
            raw = (head + ("\n【引文】" + tail + "【/引文】" if tail else "")).strip()
        else:
            # ★ 普通正文里的 <blockquote> 同样要留边界（见 annotate_quotes），
            #   但不能走上面的截断分支：长文里的一段引用不该让整条失去 S3 压缩。
            raw = annotate_quotes(e.content or "")
        # ★ 源站塞进正文的「给 AI 的指令」必须在这里剥掉：它会被模型当成要求执行，
        #   于是日报每期都以「你看到的内容可能由第三方 AI…」开头（2026-10-17 实测）。
        raw = strip_boilerplate(raw)
        text, truncated = condense(raw, settings.SUMMARY_MATERIAL_CHARS)
        items.append({
            "id": e.id,
            "feed_id": e.feed_id,
            "feed_title": m.get("title") or e.feed_title or f"#{e.feed_id}",
            "feed_name": names.get(e.feed_id) or f"#{e.feed_id}",
            "category_id": m.get("category_id"),
            "category_title": m.get("category_title") or "未分类",
            "title": (e.title or "").strip(),
            "url": e.url or "",
            "published_at": published,
            "published_local": published.strftime("%m-%d %H:%M") if published else "—",
            "text": text,
            "raw_len": len(raw),
            "truncated": truncated,
            "is_quote": is_quote,
            # 未评级（triage 还没跑到）按该 feed 的 default_level 处理；
            # rated=False 仍保留，供素材标「未定级」并单独计数。
            "level": (r.level if r is not None else feed_default),
            "rated": r is not None,
            "kind": classify_kind(e.title, text, is_quote=is_quote),
            "summary": "",       # S3 产出（超长单篇）
        })
        items[-1]["quote"] = _quote(items[-1]["feed_id"], published, items[-1]["url"],
                                    name=items[-1]["feed_name"])
    return items


def level_label(item) -> str:
    """素材里的级别角标：L0–L3；未评级写「未定级·Lx」（x=feed 默认级别）。"""
    if not item.get("rated", True):
        lv = item.get("level")
        return f"未定级·L{lv}" if lv is not None else "未定级"
    lv = item.get("level")
    return f"L{lv}" if lv is not None else "未定级"


def cap_feed_l1(items, max_items: int, keep_l1: int):
    """单源条目过多时只丢 L1：保留最近 ``keep_l1`` 条，L2/L3 一条不丢。

    返回 ``(kept_items, dropped_count)``。只影响日报素材，不标已读、不删源、
    不改评级；调用方必须把 dropped_count 写进报告头，不能静默丢。

    ★ 「最近」用 published_at 倒序，时间相同用 entry id 兜底。L1 是低价值/普通，
      丢的是低优先级内容；L2/L3 是关注/重点，任何情况下都不在这里丢。
    """
    if max_items <= 0 or keep_l1 < 0:
        return items, 0
    by_feed = {}
    for it in items:
        by_feed.setdefault(it["feed_id"], []).append(it)
    drop_ids = set()
    for feed_items in by_feed.values():
        if len(feed_items) <= max_items:
            continue
        l1 = [it for it in feed_items if it.get("level") == 1]
        if len(l1) <= keep_l1:
            continue
        l1.sort(key=lambda it: (
            it["published_at"].timestamp() if it.get("published_at") else 0,
            it.get("id") or 0), reverse=True)
        for it in l1[keep_l1:]:
            drop_ids.add(it["id"])
    if not drop_ids:
        return items, 0
    return [it for it in items if it["id"] not in drop_ids], len(drop_ids)


def _render_item(item, *, with_body=True) -> str:
    """素材块的渲染（标题 + 引用行 + 正文）。

    ★★ 顺序是**标题在前、引用在后**（2026-10-17 改）。原来是 `[引用] 标题`，
      模型很自然地照抄成"引用 + 标题"，于是写出
      `票房站 - 正在热映 - 完整版显示，[#49 · 00:00](url) 三国第一部：争洛阳`
      这种引用插在句子中间的句子（用户："引用格式异常"）。
      引用跟在标题后面，模型就只会把它当**句末出处**用。
    """
    title = item["title"] or "（无标题）"
    trunc = "（长文已截断）" if item["truncated"] else ""
    lines = [f"**{title}**{trunc} 〔{level_label(item)}〕 {item['quote']}"]
    if item["summary"]:
        lines.append(item["summary"])
    elif item["kind"] == "title_only":
        lines.append("（标题流源，无正文）")
    elif item["kind"] == "quote":
        # text 已在 build_items 里压成「本人文字 + 引文前 N 字」，这里不再二次截断
        lines.append("（引用转发）" + item["text"])
    elif item["kind"] == "repost":
        lines.append("（转贴）" + item["text"][:120])
    elif item["kind"] == "reply":
        lines.append("（互动回复）" + item["text"][:200])
    elif with_body:
        lines.append(item["text"])
    return "\n".join(lines)


def _cite_coverage(body: str, items) -> tuple:
    """**素材**引用覆盖：(正文出现过链接的素材数, 可引用素材总数)。

    ★ 这个数**不是质量指标，只是背景信息**，原因见 `_point_coverage`：
      摘要是按主题合并的，一条要点概括 7 条素材只带一处（代表）引用是**设计要求**，
      所以它天然到不了 100%，不能拿它报警（2026-10-17 就报出了 30/68 的假警报）。
    ★ 按 URL 去重而不是按 (源, URL)：正文匹配只看 URL，两个源收录同一个链接
      （如票房站 #48/#49 同一部片）时，分母算两次而链接只有一条 —— 口径要一致。
    """
    uniq = {it["url"] for it in items if it["url"]}
    if not uniq:
        return 0, 0
    hit = sum(1 for url in uniq if f"]({url})" in body)
    return hit, len(uniq)


def _point_coverage(body: str) -> tuple:
    """**要点级**引用覆盖：(带引用的要点数, 要点数, 正文链接总数)。

    ★ 为什么不再用「条目覆盖率」当告警（2026-10-17 用户提问后改）：
      S2/S1 的 prompt 本来就要求「同一主题合并成一条、标注共 N 条相关」，
      68 条素材被压成 19 个要点，一个要点常概括 7 条素材却只带一处引用 ——
      于是「30/67 条可在正文点回原文 ⚠️」每期都会亮，而**每期其实都没有故障**。
      一个永远为红的告警等于没有告警，还会掩盖真问题（整段失引）。
    ★ 真正的硬要求是 prompt 里那句「每条信息后必须带上引用」，即
      **每个要点都得点得回原文** —— 按段落查才是对的口径（10-17 实测 19/19）。
    ★ 只数 **内容小节下**的要点：`## 三、异常告警`（状态码/报错原文）和
      `## 四、谛听札记`（判断/推测）**本来就不带引用**，算进来会凭空多出
      「未附引用」（10-17 实测：全篇 19/26，只看内容 19/19）。
      ★ 注意观察段里还可能嵌一个 `### 我的思考/发现/建议` —— 光看 `###` 层级
      挡不住它，必须按**顶级小节的语义**判定（异常/观察/建议/思考/结论 一律排除）。
      ★ 2026-10-27：「我的观察与建议」改名「谛听札记」—— 关键词必须跟着加
      `札记`，否则改名后整段被当成内容小节，覆盖率会**每期亮红且无从处理**。
      旧标题里的「观察」二字仍然保留在关键词里，老报告照旧。
    """
    paras, buf = [], []
    for raw in (body or "").split("\n"):
        if raw.strip():
            buf.append(raw.strip())
        elif buf:
            paras.append("\n".join(buf)); buf = []
    if buf:
        paras.append("\n".join(buf))

    # ★ 初值 True：没有 `##` 顶级小节（或小节名不含语义关键词）时按内容处理，
    #   避免模型换了标题措辞就整段不计数（宁可少数几个要点，也不静默归零）。
    points, in_content, in_sub = [], True, True
    for p in paras:
        # ★ 段落可能以标题开头：模型有时不留空行，写成 `### 投资\n- 要点`。
        #   以前整段 `continue` 会把紧跟标题的那些要点**一起丢掉** ——
        #   表现是覆盖率 `0/0`（不报警、静默归零），比误报危险得多。
        #   所以先把开头的标题行摘掉，剩下的行照常当要点。
        lines = p.split("\n")
        head = ""
        while lines and lines[0].startswith("#"):
            h = lines.pop(0)
            head = h
            if h.startswith("## "):
                # 顶级小节：异常告警（原样保留状态码）与观察建议（推断）**不由素材支撑**
                in_content = not any(k in h for k in
                                     ("异常", "观察", "建议", "思考", "结论", "札记"))
                # ★★ 这里是 `in_content` **不是 False**：没有 `###` 子标题的正文
                #    （周报就是：`## 二、本周主线` 后面直接跟要点）一旦设成 False，
                #    后面所有要点都被 `in_sub` 挡掉 ⇒ 覆盖率 `0/0` 静默归零。
                #    标题行自己不算要点由下面的 `if not lines` 兜住。
                in_sub = in_content
            elif h.startswith("### "):
                in_sub = in_content
            else:
                in_sub = False
        # ★★ `## 标题` 与它的要点**挤在同一段**（模型不留空行，周报尤其常见）时，
        #    摘掉标题后剩下的内容要按所在小节计入 —— 否则 `in_sub` 停在标题那个
        #    False 上，后面**所有要点都被跳过** ⇒ 覆盖率 `0/0`，不报警、静默归零
        #    （比误报危险得多；2026-10-29 周报实测就是这个 0/0）。
        if head and lines and in_content:
            in_sub = True
        if not lines or not in_sub:
            continue
        # 列表块：一行一个要点（整块算一个点会掩盖"某一条漏引用"）
        points.extend([ln for ln in lines if ln.strip()])
    # ★ 明确标了「（推断）」的要点不算失引：它本来就不由素材支撑，
    #   跟 `## 四、谛听札记` 是同一个道理。2026-10-19 实测：票房站两条状态矛盾
    #   那句话是模型自己下的判断（`（推断）`），却让「15 个要点 14 个带引用」
    #   亮了一个无从处理的 ⚠️。代价是模型可以靠贴「（推断）」躲掉检查 ——
    #   所以整段失引仍由 `_uncited_categories` 盯着，那才是真故障。
    points = [p for p in points if not _INFER_RE.search(p)]
    # ★ 档位标签行（`**必读**`）不是要点：它是分组标题，本身不需要引用。
    #   不排掉的话，「下周关注 / 谛听札记」那几段会凭空多出几个"未附引用"（实测 8/11）。
    points = [p for p in points if not _LABEL_RE.match(p.strip())]
    if not points:
        return 0, 0, 0
    hit = sum(1 for p in points if _CITE_RE.search(p))
    return hit, len(points), count_cites(body)


#: 一条引用：`[源名 · 时:分](url)`。只认 http(s)，避免把相对链接算进来。
_CITE_RE = re.compile(r"\[[^\]\n]+\]\(https?://[^)\s]+\)")
#: 模型自己下的判断（不是素材事实）—— 不参与引用统计，见 `_point_coverage`。
_INFER_RE = re.compile(r"[（(]\s*(?:推断|推测|判断|个人看法)\s*[）)]")
#: 分组标签行（`**必读**` / `**按分类**` 之类）：不是要点，也不该被算成"未附引用"。
_LABEL_RE = re.compile(r"^\*\*[^*\n]{1,20}\*\*$")


def count_cites(text: str) -> int:
    return len(_CITE_RE.findall(text or ""))


#: 紧跟在 `)` 后面的下一条引用（`[源名 · 时:分](url)`）—— 用于给**连续引用**塞间隔。
#: 源名里可能也含 ` · `（重名区分），所以判据锚在末段「 · 时:分」上，不数点数。
_NEXT_CITE_RE = re.compile(r"(?<=\))(?=\[[^\[\]\n]+ · \d{1,2}:\d{2}\]\([^)\s]+\))")


def space_cites(text: str) -> str:
    """连续引用之间插一个空格：`…(u1)[A · 08:53](u2)[B · 09:00](u3)`
    → `…(u1) [A · 08:53](u2) [B · 09:00](u3)`。

    模型把一条要点的多个引用背靠背排出来时，`)[` 挤在一起肉眼难分这条
    引用到底支撑哪句话。只动「紧跟 `)` 的引用起始」，不碰普通正文与表格。
    """
    return _NEXT_CITE_RE.sub(" ", text or "")


# ---------------------------------------------------------------- 外链 → 站内深链
#: markdown 链接（引用行 + 正文里偶现的裸链接）。只认 http(s)。
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


def entry_url_map(items) -> dict:
    """素材 URL → (feed_id, entry_id)。同一 URL 多条收录时**先到先得**。

    ★ 报告里模型引用的 URL 全部来自素材行（`_render_item` 的引用行），
      所以映射天然覆盖全部引用；映射不到的（比如它引了正文里出现的
      第三方链接）由 `internalize_cites` 保持外链原样 —— 宁缺毋错。
    """
    m = {}
    for it in items:
        u = (it.get("url") or "").strip()
        if u and u not in m:
            m[u] = (int(it.get("feed_id") or 0), int(it.get("id") or 0))
    return m


def deep_link(feed_id, entry_id, src: str, mark_id=None) -> str:
    """站内深链：定位到源 → 展开该条 → 滚到那一行，并带来源标记。

    与 L3 通知（`notify.reader_entry_url`）**同一个格式** —— 同一个动作
    不能有两种 URL 方言；`src` 是 utm_source，点击落在 `core/clicktrack`。

    `mark_id`：落地后再定位到那一条划线/批注（前端读 `?mk=`）。
    ★ 必须插在 `#row-*` **之前**：拼到 fragment 后面就成了锚点的一部分，
      前端 `URLSearchParams` 读不到。
    """
    base = (getattr(settings, "NOTIFY_READER_BASE", "") or "").rstrip("/")
    if not (base and feed_id and entry_id):
        return ""
    mk = f"&mk={int(mark_id)}" if mark_id else ""
    return (f"{base}/?sel=feed:{int(feed_id)}&open={int(entry_id)}"
            f"&src={src}{mk}#row-{int(entry_id)}")


def internalize_cites(text: str, url_map: dict, src: str) -> str:
    """报告里的外链 → 站内深链（点击定位 + 展开），带 utm_source。

    ★★ **调用时序契约**：必须在所有按**素材外链**匹配的审计之后
      （`_cite_coverage` 按 `](素材url)` 匹配、`number_audit` 摘链接目标），
      先改写会让覆盖统计清零。run() 里放在 number_audit 之后。
    ★ 映射不到的链接原样保留：读起来不缺链接，覆盖统计也不受影响。
    """
    if not url_map:
        return text or ""

    def rep(m):
        label, url = m.group(1), m.group(2)
        tgt = url_map.get(url)
        deep = deep_link(tgt[0], tgt[1], src) if tgt else ""
        return f"[{label}]({deep})" if deep else m.group(0)

    return _MD_LINK_RE.sub(rep, text or "")


def _uncited_categories(body: str, by_cat: dict) -> list:
    """哪些分类在正文里**一条引用都没有**。返回 [(分类名, 条数)]。

    ★ 为什么按分类查而不是只看总数：2026-10-17 的日报整体 33/67、看着还行，
      但「AI」那整段（15 条）一句引用都没有，模型还给自己找了理由
      （"素材未提供可引用链接"）—— 总体覆盖率会把这种**整段失引**淹掉。
    """
    out = []
    for cat in by_cat.values():
        if not cat["items"]:
            continue
        urls = [it["url"] for it in cat["items"] if it["url"]]
        if urls and not any(f"]({u})" in body for u in urls):
            out.append((cat["title"], len(cat["items"])))
    return out


#: 数字（含千分位与小数）。★ 只比对**有效性 ≥2 位**的数字：
#: 单个数字（TOP3、Q3、4K）噪声太大，逐个核会全是假警报。
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
#: markdown 链接的目标部分。★ 必须先从正文里摘掉：`](https://xueqiu.com/123456/412537742)`
#: 里的雪球用户 ID、推文 ID、票房站影片 ID 全是数字，素材侧并没有这些串 ——
#: 不摘的话第一次实测就把 9 个 URL 数字全报了警（假阳性淹掉真问题）。
_LINK_TARGET = re.compile(r"\]\((?:https?://|mailto:|/)[^)\s]*\)")


def _numbers(text: str, min_digits: int = 2) -> set:
    out = set()
    for raw in _NUM_RE.findall(text or ""):
        canon = raw.replace(",", "").rstrip(".")
        if len(re.sub(r"\D", "", canon)) >= min_digits:
            out.add(canon)
    return out


def _sig(n: str) -> str:
    """有效数字串：去掉千分位与小数点，只留数字。

    ★ 用来识别「**只挪了小数点**」的单位换算 —— 这类改写是**对的**，不能报警：
      2026-10-19 实测，素材原文 `13.43 BILLION`（十亿）被正确写成「134.3 亿」，
      而审计把 `134.3` 判成"素材里找不到"。两者有效数字都是 `1343`。
      同理 `12.67 billion` → 「126.7 亿」、「1.5 万」→「15,000」。
    ★ 为什么只放过"数字序列完全相同"的情形：整数 ×10 会多一个 0
      （`878` → `8780` 的序列是 `8780` ≠ `878`），所以真编造/真算错照样被抓 ——
      2026-10-17 那次 `208`→`209`、`878`→`880` 的有效数字都变了，仍然报警。
    """
    return re.sub(r"\D", "", n).lstrip("0") or "0"


def number_audit(body: str, material: str) -> list:
    """正文里**素材中找不到**的数字（疑似模型改写/编造）。

    ★ 为什么要有这道检查：2026-10-17 的日报把票房站条目里的
      「今天 208 家影院放映 878 场」写成了「209 家影院放映 880 场」——
      而库里那条 entry 的 content 从 10-15 21:40 起就没变过，
      全文搜 `209家影院` / `880场` 都是 0 行。**这不是四舍五入，是编数**。
      数字是日报里最容易被当真的东西，所以改成机械核对。
    ★ 只核 **≥3 位整数 / 带小数** 的数字：两位数是模型自己做的加总
      （"互动回复与转贴合计 19 条" = 11+2+6）最常见的落点，逐个核会全是假警报；
      而编造事故几乎都发生在标的名、金额、场次这种多位数字上。
    ★ 2026-11-22：链接目标一律不核（markdown 链接早就在 `_LINK_TARGET` 剥掉，
      但 wiki 内链 `[[slug]]` 一直漏着）。改名页 slug 带时间戳，
      `_check_links` 要求逐字照抄清单里的正确 slug，这里却把时间戳当「编造数字」
      拒写 —— 两道守卫互锁，模型怎么写都过不了。slug 是页标识，不是数字断言；
      别名 `[[slug|显示名]]` 的显示名是给人看的正文，仍参与核对。
    """
    known = _numbers(material, min_digits=1)
    known_sig = {_sig(k) for k in known}
    unknown = []
    scrubbed = _WIKILINK.sub(
        lambda m: "[[" + (_wikilink_parts(m)[1] or "") + "]]",
        _LINK_TARGET.sub("]", body or ""))
    for n in _numbers(scrubbed, min_digits=1):
        if len(re.sub(r"\D", "", n)) < 3 and "." not in n:
            continue
        # 单位换算（billion→亿等）只挪小数点，有效数字不变 → 不算编造
        if n in known or _sig(n) in known_sig:
            continue
        unknown.append(n)
    return sorted(unknown)[:10]


def _uncovered_note(cats: list) -> str:
    return "、".join(f"{name}（{n} 条）" for name, n in cats)

