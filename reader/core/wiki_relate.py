"""一篇文章 → 知识库里「讲同一件事」的页（2026-11-16 晚）。

## 为什么做这件事

32 页 / 6,858 字的知识库，之前只有两个入口：主动点「知识库」进去翻，或者在
审批浮层里看「相关页」。**日常读文章时碰不到它**。于是它长得再好也不会被用上
—— 而「有资格不读」这个命题的落点恰恰是：读之前先知道自己已有判断。

## 为什么零模型

`wiki_related` 那条线是模型推荐（审批期、人点了才花钱）。这里挂在**每次展开
文章**的路径上 —— 一天几十上百次。所以判据必须是本地的、可预期的、不花钱的。
渲染路径零 LLM 是既有铁律（`_approval_workarea_ctx` 13 个调用点都守这条）。

## 为什么不是纯 2-gram

试过就知道：一页 200 字，它的 2-gram 大多是这一段的**具体措辞**，同主题的另一
篇文章命中率极低。真正有区分度的是人手收敛过的那 68 个标签 + 页标题里的实体
名。所以打分是分层的，每层都自带一句人话理由（`why`），而不是一个黑箱分数：
界面要能回答「为什么说这页相关」，否则人不会信第二次。

四层（分数只是相对量级，没有绝对含义）：
  1. 页标题整串出现在文章里        +10
  2. 页的某个标签出现在文章里      +4 / log2(1+df)   ← 越稀有的词越有指向性
  3. 页描述的 2-gram 覆盖 ≥ 0.6    +3

`df` = 这个词覆盖多少页。全库都有的词（真实库「AI应用」7/32）几乎不带信息，
除以 log 之后自然退到 1.4 分左右，不会把一半的页都顶上来。

## ★ 拉丁字母不许做 2-gram（真实库实测踩的坑）

第一版把中英文一律切成 2 字组合，200 篇真实文章跑了 22% 命中，然后看到：

    LVMH's Media Division announces strategic partnership
      → 5.0  Gemini相关     （「标题讲的是同一件事」）
      → 5.0  SpaceX相关     （「标题讲的是同一件事」）

`gemini` 的 gram 是 {ge, em, mi, in, ni, i相, 相关}，而 `in` / `em` / `mi` 在
**任何一段英文**里都有 —— 5/7 = 71%，过 70% 门槛。同一批里 `💯`、`Yup`、
`Important` 这些正文几个字的推文也照样命中 `SpaceX相关`（文章越短，gram 集合越
小，反而越容易覆盖满）。

三处修：拉丁串按**整词**取 token（`gemini` 只有整个词出现才算）；页标题不再用
gram 覆盖率判（中文短标题「xx相关」的 gram 只有一两个，天然 100%），只认整串；
正文归一化后不足 50 字直接不判（这么短的信息量不足以支撑「讲同一件事」）。

## 缓存

一篇文章一次，不能让每篇都重读 32 个 md：`pages()` 实测 40–48 ms，展开文章
是高频路径。所以索引按「`wiki/` 下所有 .md 的 (路径, mtime, size) 指纹」缓存。
**指纹按 root 分开存**：测试会 `override_settings(WIKI_DIR=tmp)`，共用一个槽
会让 A 库的索引答 B 库的问题（这类串味在隔离上踩过不止一次）。
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import threading

#: 只看开头这么多个字。正文尾巴通常是相关推荐/版权声明，进了反而制造假命中。
TEXT_LIMIT = 20000

#: 最多显示几页。阅读页是一行提示，不是搜索结果页。
LIMIT = 3

#: 低于这个分不显示：宁可这行不出现，也不要为了「有产出」硬凑一页。
MIN_SCORE = 3.0

#: 正文归一化后不足这么多字直接不判。`💯`、`Yup`、`Important` 这类推文实测
#: 照样能命中「SpaceX相关」—— 字越少，越容易把偶然重合当成主题重合。
MIN_TEXT = 50

#: 页描述的 gram 覆盖门槛，以及参与判断的最少 gram 数。8 个 gram 的页（正文
#: 五十来字）覆盖 60% 太容易，所以门槛压在 40 个 gram（约 40 字正文）以上。
SUM_COV, SUM_MIN_GRAMS = 0.6, 40

#: 标签只在这段长度内找（标题 + 正文开头）。实测：一篇讲持仓的长文，因为正文
#: 某处顺带提了「游戏」和「安全」，同时命中两个不相干的标签主题 —— 两条都是
#: 噪声，而且理由看着还挺像。
#: 文章开头才是它「在讲什么」，尾巴常常是相关推荐、评论区、免责声明。
#: 800 字是个折中：标题 + 两个自然段，再长就回到「顺带提一句」了。
TAG_WINDOW = 800

_CACHE_MAX_ROOTS = 8
_cache = {}
_lock = threading.Lock()

#: 只留汉字与字母数字；标点、空白一律当分隔符。
#: ★ 不做分词：「AI泡沫」和「AI 泡沫」必须能互相命中，而分词器会把它们切成
#:   不同的词。整串归一化后做子串判断最稳，也最容易解释给用户听。
_KEEP = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
_LATIN = re.compile(r"[0-9a-z]+")
_CJK = re.compile(r"[\u4e00-\u9fff]+")
#: Miniflux 存的是 HTML。不剥标签的话 `div` / `class` / `https` 会进 token 集，
#: 既稀释覆盖率（分母变大），也给了「href」「span」这类词意外命中的机会。
_HTML = re.compile(r"<[^>]{0,300}>")


def _strip_html(text) -> str:
    return _HTML.sub(" ", str(text or ""))


def _norm(text) -> str:
    """小写 + 去掉一切非字母数字汉字。用于「整串是否出现」。"""
    return _KEEP.sub("", str(text or "").lower())


def _tokens(norm: str) -> set:
    """归一化串 → 比对用 token 集合。

    ★ 中英必须分开处理：汉字取相邻 2 字（中文无词边界，2-gram 是公认的折中）；
      拉丁字母取**整词** —— `in` / `em` / `mi` 这类 2 字组合在任何英文里都有，
      拿它们比对等于给英文文章发通票（真实库 200 篇实测，22% 命中里大半是
      这种假命中，见模块 docstring）。
    """
    out = set(_LATIN.findall(norm))          # 拉丁整词（长度 1 的 a/i 也留：无妨）
    for run in _CJK.findall(norm):
        if len(run) >= 2:
            out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out


def _index_fp(root: str) -> str:
    """`wiki/` 下所有 .md 的 (相对路径, mtime, size) 摘要。

    为什么不用目录 mtime：改一页正文不改目录 mtime（只改文件），会漏失效。
    为什么不用文件内容 hash：每次展开文章都要读 32 个文件，等于没缓存。
    """
    h = hashlib.sha1()
    base = os.path.join(root, "wiki")
    if not os.path.isdir(base):
        return "empty"
    for dirpath, _dirnames, filenames in os.walk(base):
        for name in sorted(filenames):
            if not name.endswith(".md"):
                continue
            p = os.path.join(dirpath, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            h.update(os.path.relpath(p, root).encode("utf-8"))
            h.update(b"%d:%d;" % (st.st_mtime_ns, st.st_size))
    return h.hexdigest()


def _build(pages) -> list:
    """每页 → 打分用的签名。df（词覆盖页数）在这一步一次算完。"""
    tag_df = {}
    for p in pages or []:
        for t in (p.get("tags") or []):
            t = _norm(t)
            if len(t) >= 2:
                tag_df[t] = tag_df.get(t, 0) + 1

    idx = []
    for p in pages or []:
        summary = _norm(p.get("summary") or "")
        tags = []
        for t in (p.get("tags") or []):
            nt = _norm(t)
            if len(nt) >= 2:
                # ★ 权重随 df 衰减：全库通用的词（AI应用 7 页）不该和「算力」（3 页）
                #   同价。写成 log2 是为了让 df=1 时约等于满分 4，df=7 时降到 1.4。
                weight = 4.0 / math.log2(1 + tag_df.get(nt, 1) + 1)
                tags.append((nt, str(t).strip(), weight))
        idx.append({
            "slug": p.get("slug"),
            "title": p.get("title") or "",
            "title_n": _norm(p.get("title") or ""),
            "summary_tokens": _tokens(summary),
            "tags": tags,
            "page": p.get("page"),
            "domain": p.get("domain"),
            "summary": (p.get("summary") or "").strip(),
        })
    return idx


def _get_index(root: str, pages=None) -> list:
    """按指纹取索引；指纹变了才重建（重建时顺手淘汰别的 root，测试不串味）。"""
    fp = _index_fp(root)
    with _lock:
        hit = _cache.get(root)
        if hit and hit[0] == fp:
            return hit[1]
    from core import wiki as wstore          # 延迟 import：本模块被 views 间接引入
    if pages is None:
        pages = wstore.pages()
    idx = _build(pages)
    with _lock:
        _cache[root] = (fp, idx)
        while len(_cache) > _CACHE_MAX_ROOTS:
            _cache.pop(next(iter(_cache)))
    return idx


def relate(text, title="", pages=None, limit=LIMIT, min_score=MIN_SCORE) -> list:
    """正文 → 相关页列表（按分降序）。`[{slug, title, page, domain, summary, score, why}]`

    `why` 是给界面用的一句人话（如「都提到「算力」」）。**它不会为空**：没有
    任何一条可解释的命中时这一页直接不出现在结果里 —— 一个说不出理由的推荐，
    人第一次点进去就不会有第二次。
    """
    body = _norm(_strip_html(text))[:TEXT_LIMIT]
    if len(body) < MIN_TEXT:
        return []
    head = _norm(title)
    # 标题单独拼在前面：标题里的词是「这篇在讲什么」最可靠的信号，正文可能只是
    # 顺带提一句。
    whole = head + body
    toks = _tokens(whole)
    # ★ 标签只看开头：见 TAG_WINDOW 的注释（长文尾巴里的「游戏」「安全」制造噪声）
    lead = head + body[:TAG_WINDOW]

    from core import wiki as wstore
    idx = _get_index(str(wstore.root()), pages)

    out = []
    for it in idx:
        score, why = 0.0, []
        tn = it["title_n"]
        if len(tn) >= 2:
            # ★ 分两档，且理由文案必须说实话：页标题出现在**文章标题**里是
            #   「这是同一件事」，出现在正文某处只算「提到了」。第一版一律写
            #   「标题里就有」，结果一篇文章讲机房运维，只因正文有一句「实战经验
            #   分享」就被说成「标题里就有实战经验」—— 人点进去会发现被骗。
            if tn in head:
                score += 10.0
                why.append("文章标题就是「%s」" % it["title"].strip())
            elif tn in body:
                score += 6.0
                why.append("文章提到「%s」" % it["title"].strip())
        for ntag, tag, weight in it["tags"]:
            if ntag in lead:
                score += weight
                why.append("都提到「%s」" % tag)
        st = it["summary_tokens"]
        if len(st) >= SUM_MIN_GRAMS:
            cov = len(st & toks) / float(len(st))
            if cov >= SUM_COV:
                score += 3.0
                why.append("描述的内容重合")
        if score >= min_score and why:
            out.append({
                "slug": it["slug"], "title": it["title"], "page": it["page"],
                "domain": it["domain"], "summary": it["summary"],
                "score": round(score, 1), "why": why[:2],
            })
    out.sort(key=lambda r: (-r["score"], r["slug"] or ""))
    if limit is not None:
        out = out[:limit]
    return out


def reset_cache():
    """供测试/命令行用：清掉索引缓存。"""
    with _lock:
        _cache.clear()
