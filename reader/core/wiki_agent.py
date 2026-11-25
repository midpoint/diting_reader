"""审批对话的 wiki 原语：`read` + `set`（第三步，**纯后端、不接 LLM**）。

定位（实现设计 v2 §三）：审批操作的是「候选的分组」，不是正文文本。所以给 agent
的工具收敛成两个：

    read(scope, slug)      只读，看当前 drafts / 某页 / 候选池
    set(target, payload)   声明式设置「什么属于哪里」，落点是 .drafts/ 的
                           frontmatter `candidates` + 分页

★ `set` 同时承担 create / update / delete：拆页、合并、改标题/域/标签、丢弃，
  都是它的不同用法。**不设 split/merge 工具** —— 那是把决策固化进工具，等于替
  agent 决定「什么算一页」。

★ 本模块**不接 LLM、不接对话 loop**（第四步），**不做 polish 重生成**（第四步），
  **不实现 commit 物理移动**（改动 F）。它只是两个能被程序校验的纯函数。

★★ 模块内的工具函数叫 `set`，会遮蔽 builtins.set。所有集合构造一律用
   `builtins.set(...)` 或字面量/推导式，绝不裸写 `set(...)` —— 否则这个模块
   会在自己的函数体里递归调自己，而且症状是「跑起来才炸、还看不出哪行」。
"""
import builtins
import difflib
import logging
import os
import re
from pathlib import Path

from django.utils import timezone

from core import wiki as wstore
from core.models import INGEST_LIVE_STATUS, IngestCandidate

log = logging.getLogger(__name__)

#: `set` **唯一**允许改的 frontmatter 字段（守卫 1）。白名单而非黑名单：
#: 将来新增机器字段时默认被锁住，不会漏进 agent 的可写范围。
SETTABLE_FM = ("title", "domain", "tags", "candidates")

#: payload 里出现这些键一律拒（守卫 1 的入口检查）。它们由编译链 / 审批程序维护，
#: agent 改 `sources` 就等于能编造来源、毒化反向映射 —— 不能靠提示词约束。
#: ★ `edits`（第六步）也在锁里：它标记「这份草稿是某已入库页的替换稿」，决定 commit
#:   走「整页覆盖」分支。让 agent 能写它，等于让它能把普通草稿伪装成覆盖稿，
#:   然后就有一条不存在的覆盖链 —— 这必须是程序（`wiki.pull_page`）的独占动作。
#: ★ 第十步：`retire` / `retire_from` 是删页提案的意图标记。让 agent 能写它们，
#:   等于让它能私自把一条候选从「废弃」改成「搬走」（或反过来），而守恒守卫
#:   只看最终集合、看不出是谁改的 —— 这必须是程序按人点选的结果独占写入。
#: ★ `draft_id` 是草稿的稳定身份编号（程序自增分配）。让 agent 改它等于能让
#:   两份稿撞号 / 把编号跳回旧值 —— 用户正是为了消灭跳变才要的它，必须锁死。
LOCKED_FM = ("sources", "published_at", "updated", "confidence", "facts", "merges",
             "edits", "retire", "retire_from", wstore.STALE_FM, wstore.DRAFT_ID_FM)

#: `set` 落盘时新建页要用到的机器字段，全部**由程序算**，不从 payload 取。
_MACHINE_ON_CREATE = ("sources", "updated", "confidence")

#: ★ 第十三步（形态 A）：分组变更后的「结论待重生成」标记搬进 frontmatter，
#:   不再插进正文（正文是纯 polish，`> ⚠️` 会污染结论）。写/剥/读口径收在
#:   `wstore.STALE_FM` 一处。下面两个老字形只为**读清零前的历史草稿**保留，
#:   任何写路径都不再产出它们。
STALE_FM = wstore.STALE_FM
STALE_MARK = wstore.STALE_MARK
STALE_NOTE = wstore.STALE_NOTE


def _is_stale(fm: dict, body: str) -> bool:
    """这页的结论是不是「还没重编成功」：frontmatter 标记为准，老草稿正文提示兼容。

    ★ 兼容分支是「清零前的历史草稿」，不是新格式的兜底：新格式一律写
      `stale: true`，`set_polish` 成功才剥。出错时谁会知道 = 浮层可见提示。
    """
    raw = str(fm.get(STALE_FM) or "").strip().lower()
    return raw in ("true", "1", "yes") or STALE_MARK in (body or "")


# ---------------------------------------------------------------- 候选明细
def _cand_detail(c) -> dict:
    """候选 → 给 agent 看的字典。字段必须是**真实字段**（quote/claim/发布时间/出处），
    否则 agent 只能靠标题猜分组 —— 那又回到「模型替我判断」的老路。
    ★ 2026-11-09 添加 mark_id/msg_id：来源链接细化到划线/评论级别。
    """
    return {
        "cand_id": c.id,
        "claim": c.claim,
        "quote": c.quote or "",
        "quote_from": c.quote_from,
        "domain": c.domain or "",
        "confidence": c.confidence or "",
        "published_at": c.published_at.isoformat() if c.published_at else None,
        "source": f"{c.source_type}:{c.source_id}",
        "mark_id": c.mark_id,
        "msg_id": c.msg_id,
        "status": c.status,
    }


def live_candidates() -> dict:
    """`{id: 候选对象}`，只含**未丢弃**的。判据与 `compile_backlog` 共用
    `models.INGEST_LIVE_STATUS` 一份 —— 两处各写一遍必然漂成「面板说有、read 说没有」。"""
    return {c.id: c for c in IngestCandidate.objects.filter(
        status__in=INGEST_LIVE_STATUS)}


# ---------------------------------------------------------------- 折叠溯源区（第十三步 A）
#: 「谁说的」按 `quote_from` 分组。★ 这是三块（judge/material/interest）下沉后
#: 的**唯一**替代：`quote_from` 在提炼阶段已由 `verify_quotes` 程序锁死，不靠模型。
#: `kind`（知识类型轴）与此正交，不参与分组。
#: 最后一组是显式可见的「未标注来源」，不让 `quote_from` 为空的候选静默混进素材组。
TRACE_GROUPS = (("mine", "我的判断"),
                ("article", "事实 / 他人的观点"),
                ("highlight", "素材 / 我在意"),
                ("", "未标注来源"))


def trace_rows(ids: list) -> dict:
    """折叠溯源区的数据源：按 frontmatter `candidates` 读**精确**出处。

    ★ 第十三步 A：正文只留一段 polish 导读（措辞可轻微瑕疵）；每条判断 / 素材 /
      在意点的「谁说的、原话是什么、出处在哪、什么时候」全靠这里，**必须精确**。
      数据源是 `IngestCandidate` 结构化字段，不是解析正文三块。
    ★ 幽灵候选（已丢弃 / 不存在）显式进 `unknown`，不静默漏掉一条 ——
      「溯源区没有」会被人当成「这页不含它」。
    """
    live = live_candidates()
    # 局部导入：ingest 顶层不依赖 wiki_agent，这里只为复用那一个深链函数。
    from core.ingest import _link_for
    buckets = {k: [] for k, _ in TRACE_GROUPS}
    unknown = []
    for i in ids or []:
        c = live.get(i)
        if c is None:
            unknown.append(i)
            continue
        row = {
            "cand_id": i,
            "claim": c.claim or "",
            "quote": c.quote or "",
            "source": f"{c.source_type}:{c.source_id}",
            "published_at": c.published_at.isoformat() if c.published_at else "",
            "kind": c.kind or "",
            "link": _link_for(c),
        }
        qf = c.quote_from or ""
        buckets[qf if qf in buckets else ""].append(row)
    groups = [{"key": k, "label": label, "rows": buckets[k]}
              for k, label in TRACE_GROUPS if buckets[k]]
    return {"groups": groups, "unknown": unknown,
            "total": sum(len(g["rows"]) for g in groups)}


def _published_slugs() -> set:
    """已入库页的 slug 集合（只扫 `wiki/`，不含 `.drafts/`）。"""
    return {str(p["slug"]).strip() for p in wstore.pages() if p.get("slug")}


def _published_dst_error(dst: str, before: dict) -> str:
    """`set` 想新建一份**与已入库页同名**的草稿 → 拒绝理由。收一处，两处调用。

    ★ 2026-11-13 实测：模型把「#217 并入 AI泡沫」硬凑成
      `set(candidates, to="ai-pao-mo")`，`_set_split` 只查 `.drafts/` 快照，
      于是新建了 `.drafts/ai-pao-mo.md`。那份草稿没有 `merges` 声明，
      `_validate_draft` 会判「已存在同名页」永远拒收 —— 一次「成功」的 set
      造出一份卡住的死草稿，而模型还以为自己办完了。
    ★ 改标题路径早有 `wstore.read_page()` 撞名检查（本文件 `_set_field`），
      拆页 / 合并路径漏了。这就是「修完一处要问同类还有几处」。
    """
    if dst in before or dst not in _published_slugs():
        return ""
    target, _why = wstore.find_page(dst)
    shown = (target.relative_to(wstore.root()).as_posix()
             if target is not None else f"wiki/**/{dst}.md")
    return (f"{dst} 是已入库页（{shown}），set 不能新建同名草稿 —— "
            "普通新页撞同名页会被 approve 拒收，变成一份卡住的草稿。"
            "要把候选并入它，请用 merge_into 工具或审批浮层的『改为并入』。")


def _draft_path(slug: str) -> Path:
    return wstore.root() / ".drafts" / f"{slug}.md"


def _check_slug(slug):
    return (wstore._safe_slug(slug), f"非法 slug：{slug!r}")


# ---------------------------------------------------------------- read
def read(scope: str, slug: str = "") -> dict:
    """只读。scope ∈ {drafts, draft, page, candidates}，返回 dict。

    一律返回 `ok` / `err`，不抛异常给 agent —— 工具调用失败要能被人读懂。
    """
    if scope == "drafts":
        # ★ 展示入口才补编号（幂等）：浮层与 briefing 都从这里读草稿，D<draft_id>
        #   在被人 / agent 看见之前就已落定。read 的「只读」指不改分组 / 正文，
        #   补一个机器身份字段是例外，判据与写盘纪律见 wstore.ensure_draft_ids。
        wstore.ensure_draft_ids()
        rows = wstore.list_drafts()
        return {
            "ok": True, "scope": "drafts", "count": len(rows),
            "approval_active": wstore.approval_active(),
            "drafts": [{k: d.get(k) for k in
                        ("slug", "draft_id", "title", "domain", "candidates",
                         "complete", "missing", "merges", "edits", "delete")}
                       for d in rows],
        }

    if scope == "draft":
        ok, why = _check_slug(slug)
        if not ok:
            return {"ok": False, "err": why}
        p = _draft_path(slug)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            return {"ok": False, "err": f"草稿不存在或读不到：{slug}"}
        fm, body = wstore.parse_front(text)
        ids = wstore._cand_ids(fm)
        live = live_candidates()
        return {
            "ok": True, "scope": "draft", "slug": slug,
            "fm": fm, "body": body, "text": text,
            "candidates": ids,
            "candidate_detail": [_cand_detail(live[i]) for i in ids if i in live],
            "unknown_candidates": [i for i in ids if i not in live],
            "body_stale": _is_stale(fm, body),
            "missing": wstore.missing_fm(fm),
            # ★ 第六步：替换稿要能让浮层说清「这是在改哪一页」，不能显示成新草稿。
            "edits": str(fm.get("edits") or "").strip(),
            # ★ 第八步：`edits` 的空形态 = 删页。判据在 `wiki.is_delete_fm` 一处，
            #   浮层据此显示「删除」而不是「编辑」（两者不可逆程度差很远）。
            "delete": wstore.is_delete_fm(fm),
            "merges": str(fm.get("merges") or "").strip(),
        }

    if scope == "page":
        pg = wstore.read_page(f"wiki/{slug}.md") if slug else None
        if pg is None:
            # slug 不含 domain 时按 stem 全局找（find_page 已处理重名）
            p, why = wstore.find_page(slug)
            if p is None:
                return {"ok": False, "err": why}
            pg = wstore.read_page(p.relative_to(wstore.root()).as_posix())
        if pg is None:
            return {"ok": False, "err": f"读不到已入库页：{slug}"}
        ids = wstore._cand_ids(pg["fm"])
        live = live_candidates()
        return {
            "ok": True, "scope": "page", "slug": pg["slug"], "page": pg["page"],
            "fm": pg["fm"], "body": pg["body"], "candidates": ids,
            "candidate_detail": [_cand_detail(live[i]) for i in ids if i in live],
        }

    if scope == "candidates":
        live = live_candidates()
        return {"ok": True, "scope": "candidates", "count": len(live),
                "status_filter": list(INGEST_LIVE_STATUS),
                "candidates": [_cand_detail(c) for c in
                               sorted(live.values(), key=lambda x: x.id)]}

    return {"ok": False,
            "err": f"未知的 scope：{scope!r}（只能是 drafts / draft / page / candidates）"}


# ---------------------------------------------------------------- frontmatter 落盘
#: 只认 `key:` 开头的行；列表续行 `- item` 归上一个 key。
_FM_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:")

#: 一页最多几个标签。与 `_compose_page` 的 `tags[:8]` 同口径（那边是机器生成时截断，
#: 这边是**拒收**：人刚敲的标签静默消失，比一句「最多 8 个」难查得多）。
TAG_MAX_N = 8
#: 单个标签的最长字符数。与编译链建议 `str(t).strip()[:20]` 同口径。
TAG_MAX_CHARS = 20

#: 标签的分隔符：半/全角逗号、顿号、分号（人敲「现金流,估值」「现金流、估值」都认）。
_TAG_SPLIT_RE = re.compile(r"[,，、;；]")


def clean_tags(val) -> tuple:
    """把「人敲的 / 模型给的」标签收敛成能安全写进 `tags: [a, b]` 的字符串列表。

    返回 `(list, err)`；`err` 非空时 list 为 None。

    ★ 为什么必须在写盘前收敛：frontmatter 的列表是**逗号分隔的单行**，标签里带
      逗号或方括号，读回来时会被拆成两个词、或把方括号切坏 —— 这种损坏不报错，
      只会在某天发现「标签筛选少了一页」。
    ★ 字符串入参也认（按 `,，、;；` 拆）：审批页的输入框、模型偶尔给的
      `"现金流, 估值"` 都走这一条，判据只有一份。
    ★ 超限**拒收**而不是截断：截断是静默失败的一种（人以为第 9 个标签生效了）。
    ★ 空列表是合法的（`tags: []` 就是合法的无标签页，见 `FM_KEY_ONLY`）。
    """
    if isinstance(val, str):
        items = _TAG_SPLIT_RE.split(val)
    elif isinstance(val, (list, tuple)):
        items = []
        for x in val:
            items.extend(_TAG_SPLIT_RE.split(str(x)))
    else:
        return None, (f"tags 必须是列表或逗号分隔的字符串，收到 "
                      f"{type(val).__name__}")
    out, seen = [], builtins.set()
    for raw in items:
        t = str(raw).strip().strip("'\"")
        if not t:
            continue
        if any(ch in t for ch in "[]\n\r"):
            return None, (f"标签「{t}」里有方括号或换行，会破坏 frontmatter 的"
                          "列表格式")
        if len(t) > TAG_MAX_CHARS:
            return None, (f"标签「{t[:10]}…」太长（{len(t)} 字，最多 "
                          f"{TAG_MAX_CHARS} 字）")
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    if len(out) > TAG_MAX_N:
        return None, f"标签最多 {TAG_MAX_N} 个（现在 {len(out)} 个）"
    return out, ""


def _fmt_value(key: str, val) -> str:
    if key in ("tags", "candidates"):
        if not isinstance(val, (list, tuple)):
            raise ValueError(f"{key} 必须是列表")
        return "[" + ", ".join(str(x).strip() for x in val) + "]"
    s = str(val).strip()
    if "\n" in s or "\r" in s:
        raise ValueError(f"{key} 不能含换行")
    return s


def _rewrite_fm(text: str, changes: dict) -> str:
    """保留原 frontmatter 的**所有其它行与顺序**，只替换 `changes` 里的键。

    ★ 为什么不重新序列化：草稿里还有 `facts:` / `merges:` 等编译链写下的行，
      一整段重写会把它们洗掉，而且不报错。按行替换让「机器字段由程序维护」
      成为结构事实 —— set 连碰都碰不到那些行。
    """
    m = wstore._FM_RE.match(text or "")
    if not m:
        raise ValueError("文件没有合法的 frontmatter")
    lines = m.group(1).splitlines()
    out, seen, skip_list = [], builtins.set(), False
    for raw in lines:
        s = raw.strip()
        mm = _FM_KEY_RE.match(s)
        if mm:
            # ★ 只在遇到**下一个 key** 时重设：空行 / 注释不该打断 `- item`
            #   续行的归属，否则旧的多行列表值会残留半截在文件里。
            key = mm.group(1).lower()
            skip_list = key in changes
            if skip_list:
                seen.add(key)
                out.append(f"{key}: {_fmt_value(key, changes[key])}")
                continue
        elif skip_list and s.startswith("- "):
            continue                       # 旧的多行列表值，跟着旧 key 一起换掉
        out.append(raw)
    for key, val in changes.items():
        if key not in seen:
            out.append(f"{key}: {_fmt_value(key, val)}")
    return "---\n" + "\n".join(out) + "\n---\n" + text[m.end():]


def _write_fm(slug: str, changes: dict) -> tuple:
    """守卫 1 的**唯一落点**：任何 frontmatter 写入都过这里。"""
    bad = [k for k in changes if k not in SETTABLE_FM]
    if bad:
        return False, ("set 不能改 %s —— 这些是程序维护的机器字段；"
                       "只允许 %s" % ("、".join(bad), "、".join(SETTABLE_FM)))
    ok, why = _check_slug(slug)
    if not ok:
        return False, why
    p = _draft_path(slug)
    try:
        text = p.read_text(encoding="utf-8")
        new = _rewrite_fm(text, changes)
    except (OSError, ValueError) as e:
        return False, f"写草稿 {slug} 失败：{e}"
    return _atomic_write(p, new)


def _atomic_write(p: Path, text: str) -> tuple:
    """tmp + os.replace：审批 panel 随时可能在读，读到半截文件会被当成空页。"""
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)
    except OSError as e:
        return False, f"写文件失败：{e}"
    return True, ""


def _new_page_text(cands: list, *, title: str, domain: str,
                   tags=None, candidates=None) -> str:
    """程序**新建**一页草稿时的完整 frontmatter + **空正文**（形态 A）。

    ★ `sources` / `updated` / `confidence` 由程序从候选算，绝不从 payload 取
      （守卫 1 的正面：机器字段不是「agent 不能改」，是「程序负责填」）。
    ★ 第十三步 A：正文**留空**，等 `wiki_loop._polish_all` 写纯 polish 一段。
      分组的唯一真值是 frontmatter `candidates`；精确出处由折叠溯源区读
      `IngestCandidate` 字段负责，不再往正文里写候选清单。
    ★★ 与 `wstore.render_polish_only()` 是「同口径拼正文两处」：改一处必 grep
      另一处，否则新建页仍写清单，只有真审批才暴露分叉。
    ★ 「结论还没生成」是**可见状态**（读侧会给说明），不许塞占位字进正文 ——
      那会污染结论，还会让 `body_stale` 判据失灵。
    """
    conf_rank = {"high": 3, "medium": 2, "low": 1}
    ranked = [c.confidence for c in cands if c.confidence in conf_rank]
    conf = max(ranked, key=lambda x: conf_rank[x]) if ranked else "medium"
    sources = sorted({f"{c.source_type}:{c.source_id}" for c in cands})
    ids = [c.id for c in cands] if candidates is None else list(candidates)
    fm = ["---", f"title: {title}", f"domain: {domain}",
          f"sources: [{', '.join(sources)}]",
          f"updated: {timezone.localdate().isoformat()}",
          f"confidence: {conf}",
          "tags: [" + ", ".join(str(t).strip() for t in (tags or [])) + "]",
          "candidates: [" + ", ".join(str(i) for i in ids) + "]", "---", ""]
    # ★ 形态 A：正文留空（见 docstring）。`candidate_list_text` 退役为「纯喂模型
    #   的素材」，只在 `wiki_loop._polish_one` 里按 frontmatter 现算，不落盘。
    return "\n".join(fm) + "\n"


def _mark_body_stale(text: str) -> str:
    """分组改了、结论还没重编：在 frontmatter 写下**可见的瞬时标记**。

    ★ 第十三步 A：不再插 `> ⚠️` 进正文 —— 正文是纯 polish，插进去就污染结论。
      标记落在 frontmatter（`stale: true`），由 `wstore.set_polish()` 在**真重编
      成功**时剥掉；失败时保留，审批浮层照旧给出可见提示。
      「警告消失 ≠ 内容一致」：只有程序真的写完新结论，警告才消失。
    ★ 原子写盘前调用，改的是内存里的文本；同一份草稿重复标记不叠加。
    """
    if wstore._FM_RE.match(text or "") is None:
        return text
    fm, _ = wstore.parse_front(text)
    if _is_stale(fm, ""):
        return text
    return wstore._set_fm_line(text, STALE_FM, "true")


# ---------------------------------------------------------------- 守卫 2
def _check_candidates(planned: dict, before: dict, *, allow_shrink=False) -> str:
    """写盘**之前**校验候选一致性。返回空串 = 通过，否则是可直接显示的原因。

    planned / before = `{slug: [cand_id, ...]}`（写盘后 / 写盘前的全部草稿页）。
    三条判据：
      1. 幽灵候选：声明的 id 不在「真实存在且未丢弃」的候选表里；
      2. 重复归属：同一 id 出现在两页（拆页写一半会留下这种残局）；
      3. 守恒：并集不增不减（`discard` 允许减少 —— 被丢的候选随退稿一并废弃，
         见 `wstore.discard`；改判回待审才会再编）。
    ★ 为什么在写盘前而不是写盘后回滚：文件系统没有跨文件事务，「先写完再发现
      少了候选」就已经产生了孤儿，只能靠日志补救。先在内存算完再落盘，拒写
      时一个字节都没动。
    ★★ **作用域：草稿之间**。本条的守恒基准是 `_snapshot()` —— 只扫
      `.drafts/*.md`，所以「拆 / 并」这种「草稿 → 草稿」的搬运归它管。
      第十步删页提案是「**已入库页 → 草稿**」，跨了两个世界：原页候选不在
      `before` 里，新增的 id 会被本条判成「分组操作凭空多出候选」＝ 假红。
      那一条另在 `wiki.check_retire_conservation()`（集合守恒，读真实文件），
      **不要放宽本条去迁就它**，否则第七步拆页的保护会一起被拆掉。
      两处「同形但不同域」的关系写在这里，省得后人以为漏了一处。
    """
    live = live_candidates()
    ghost = sorted({i for ids in planned.values() for i in ids if i not in live})
    if ghost:
        return ("候选 %s 不存在或已丢弃，不能写进页面（幽灵候选）"
                % "、".join(str(i) for i in ghost))

    seen, dup = builtins.set(), builtins.set()
    for ids in planned.values():
        for i in ids:
            (dup if i in seen else seen).add(i)
    if dup:
        return ("候选 %s 同时出现在多页（分组不能重复归属）"
                % "、".join(str(i) for i in sorted(dup)))

    b_all = [i for ids in before.values() for i in ids]
    p_all = [i for ids in planned.values() for i in ids]
    b_set, p_set = builtins.set(b_all), builtins.set(p_all)
    added, lost = p_set - b_set, b_set - p_set
    if added:
        return ("分组操作凭空多出候选 %s（拆页/合并只能在已有候选之间搬）—— 已拒绝，"
                "旧页未改动" % "、".join(str(i) for i in sorted(added)))
    if lost and not allow_shrink:
        return ("分组操作会弄丢候选 %s（既不在旧页也不在任何新页）—— 已拒绝，"
                "旧页未改动" % "、".join(str(i) for i in sorted(lost)))
    return ""


def _snapshot() -> dict:
    """操作前全部草稿页的 candidates。只读 `.drafts/*.md`，与 list_drafts 同口径。"""
    return {d["slug"]: list(d["candidates"]) for d in wstore.list_drafts()}


# ---------------------------------------------------------------- set
def set(target: str, payload: dict) -> tuple:
    """声明式设置「什么属于哪里」。返回 `(ok, payload_or_err)`。

    target ∈ {candidates, merge, title, domain, tags, discard, frontmatter}。
    所有动作都先过守卫 1（`_guard_payload`）、再过守卫 2（`_check_candidates`），
    **全通过才落盘**。守卫 3：`set` 从不调用 `approve()` / 从不写 `wiki/`，
    入库是改动 F 的程序动作。
    """
    payload = payload or {}
    why = _guard_payload(payload)
    if why:
        return False, why

    before = _snapshot()
    action = (target or "").strip().lower()

    if action == "candidates":
        return _set_split(payload, before)
    if action == "merge":
        return _set_merge(payload, before)
    if action in ("title", "domain", "tags"):
        return _set_field(action, payload, before)
    if action == "frontmatter":
        return _set_frontmatter(payload, before)
    if action == "discard":
        return _set_discard(payload, before)
    return False, (f"未知的 set 动作：{target!r}（只能是 candidates / merge / "
                   "title / domain / tags / discard / frontmatter）")


def _guard_payload(payload: dict) -> str:
    """守卫 1 的入口：payload 里不许夹带机器字段，`fields` 只认白名单。

    ★ 也要拦 `fields` 里的越界键：agent 最常用的通用动作是 `frontmatter`，
      不查这一层就等于给它开了一道改写 `sources` 的后门。
    """
    bad = [k for k in payload if k in LOCKED_FM]
    if bad:
        return ("set 不能改 %s —— 这些字段由程序维护（来源/发布时间/更新时间/"
                "置信度/事实清单/合并意图/草稿编号），改它们会毒化溯源" % "、".join(bad))
    fields = payload.get("fields") or {}
    if not isinstance(fields, dict):
        return "fields 必须是字典"
    bad2 = [k for k in fields if k not in SETTABLE_FM]
    if bad2:
        return ("set 不能改 %s；只允许 %s"
                % ("、".join(bad2), "、".join(SETTABLE_FM)))
    return ""


def _update_slug_references(old_slug: str, new_slug: str):
    """同步更新其他草稿对旧 slug 的引用：merges/edits 字段 + 正文 wikilink。

    ★ 草稿 A 的 merges/edits 指向草稿 B，B 改名后 A 的引用也要跟着改，
      否则审批时显示「编辑 wiki/old-slug.md」但文件已经叫 new-slug.md。
    ★ 正文里的 [[old-slug]] / [[old-slug|别名]] 同理（2026-11-10 实测：
      只改了 frontmatter，正文链接还指着旧 slug，入库后全是断链）。
    ★ 只认后面紧跟 ]] 或 | 的完整 slug：[[old-slug-extra]] 这种前缀相同的
      其它页不能被误伤。
    """
    link_re = re.compile(r"\[\[" + re.escape(old_slug) + r"(?=\]\]|\|)")
    for d in wstore.list_drafts():
        if d["slug"] == old_slug:
            continue
        updates = {}
        if d.get("merges") == old_slug:
            updates["merges"] = new_slug
        if d.get("edits") == old_slug:
            updates["edits"] = new_slug
        if updates:
            _write_fm(d["slug"], updates)
            wstore.log_append("agent-set", f".drafts/{d['slug']}.md",
                              f"slug 引用 {old_slug} → {new_slug}")
        # 正文 wikilink：与 frontmatter 分开写（_write_fm 只动 frontmatter 区）
        p = _draft_path(d["slug"])
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            log.error("读草稿 %s 失败：%s（wikilink 未更新）", d["slug"], e)
            continue
        new_text = link_re.sub(f"[[{new_slug}", text)
        if new_text != text:
            ok, why = _atomic_write(p, new_text)
            if not ok:
                log.error("正文 wikilink 更新失败 %s：%s", d["slug"], why)
                continue
            wstore.log_append("agent-set", f".drafts/{d['slug']}.md",
                              f"正文 wikilink {old_slug} → {new_slug}")


# ★ 2026-11-13：归档 / 退稿时的 wikilink 解包已收口到
#   `core.wiki.discard()` → `sync_discard_references()`（唯一落点，四条退稿路径
#   全覆盖，且顺带处理已入库页）。原先这里的 `_unwrap_slug_references` /
#   `_archived_title` 只解包「其他草稿」、只在 `_commit_writes(archive=)` 一处
#   被调用，用户手点退稿完全没同步 —— 不要再在本层另写一份。


def _set_field(action: str, payload: dict, before: dict) -> tuple:
    """改 title / domain / tags —— 单页、candidates 不动。
    
    ★ title 改动时同步改 slug：
      - payload 里的 `new_slug` 只有是「描述性英文」（≥3 词或 15+ 字符）且
        不等于原 slug 时才被采用（agent 明确按用户要求指定）
      - 否则从标题自动生成（英文照标题、中文走拼音），slug 永远跟着标题走
      - 检查不与现有草稿/已入库页冲突，冲突则直接失败
      - 然后重命名文件 + 更新 frontmatter + 同步其他草稿的 merges/edits 引用
    """
    slug = str(payload.get("slug") or "").strip()
    val = payload.get(action)
    if val is None:
        return False, f"缺少 {action} 的值"
    if action == "tags":
        # ★ 收敛/校验只有一份（`clean_tags`）：输入框、模型、`frontmatter` 动作
        #   都走它。标签里带逗号会写坏 frontmatter 的列表，而这种损坏不报错。
        tags, why = clean_tags(val)
        if why:
            return False, why
        val = tags
    if action == "domain":
        domains = wstore.ontology_domains()
        if str(val).strip() not in domains:
            return False, (f"domain {val!r} 不在受控清单里（Meta/ontology.md 的"
                           f"「领域」表）：{'、'.join(domains) or '清单为空'}")
    if slug not in before:
        return False, f"草稿不存在：{slug}（set 只改 .drafts/ 里的草稿）"
    
    # ★ title 改动：同步改 slug
    if action == "title":
        # ★ agent 传的 new_slug 只认「描述性英文」（≥3 个词或 15+ 字符），
        #   且不能等于原 slug —— slug 必须跟着标题走（2026-11-09 用户拍板）。
        #   提示词说一百遍也拦不住模型传 "ai" 或旧 slug，只能在这里拦：
        #   不达标的 new_slug 一律忽略，走自动生成。
        wanted = str(payload.get("new_slug") or "").strip()
        if wanted and wanted != slug and (len(wanted) >= 15 or wanted.count("-") >= 2):
            new_slug = wanted
        else:
            from core.ingest import _slugify
            has_cjk = any('\u4e00' <= c <= '\u9fff' for c in str(val))
            if has_cjk:
                # ★ 含中文的标题**先拼音再 slugify**：直接 _slugify 会把中文
                #   丢掉、只剩拉丁部分（"AI泡沫" → "ai"，2026-11-09 实测）。
                try:
                    from pypinyin import lazy_pinyin
                    new_slug = _slugify("-".join(lazy_pinyin(str(val))), slug)
                except ImportError:
                    new_slug = slug
            else:
                new_slug = _slugify(str(val), slug)
            # 拼音也拿不到新 slug（纯符号标题等），加时间戳兜底
            if new_slug == slug:
                import time
                new_slug = f"{slug}-{int(time.time())}"[:60].strip("-")
        
        if new_slug != slug:
            # 检查新 slug 是否冲突
            if new_slug in before:
                return False, f"slug 冲突：{new_slug} 已是现有草稿"
            if wstore.read_page(f"wiki/{new_slug}.md"):
                return False, f"slug 冲突：{new_slug} 已是已入库页"
            # 检查 new_slug 格式
            if not wstore._safe_slug(new_slug):
                return False, f"slug 格式非法：{new_slug}"
            # 重命名文件
            old_path = _draft_path(slug)
            new_path = _draft_path(new_slug)
            try:
                old_path.rename(new_path)
            except OSError as e:
                return False, f"重命名草稿失败：{e}"
            # 更新 frontmatter（只改 title，slug 从文件名派生）
            ok, err = _write_fm(new_slug, {action: val})
            if not ok:
                # 回滚重命名
                try:
                    new_path.rename(old_path)
                except OSError:
                    pass
                return False, err
            # ★ 同步更新其他草稿对旧 slug 的引用（merges/edits 字段）
            _update_slug_references(slug, new_slug)
            # ★ 审批标记跟着页走（2026-11-09：不搬的话 approved.json 里留旧 slug，
            #   列表「已审 2/9」但一个圈都没勾）
            wstore.rename_approved(slug, new_slug)
            wstore.log_append("agent-set", f".drafts/{new_slug}.md",
                              f"title → {val}、slug {slug} → {new_slug}")
            return True, {"action": action, "slug": new_slug, "old_slug": slug,
                          "value": val, "body_stale": [],
                          "note": "title 改了，slug 同步更新"}
    
    ok, err = _write_fm(slug, {action: val})
    if not ok:
        return False, err
    wstore.log_append("agent-set", f".drafts/{slug}.md", f"{action} → {val}")
    return True, {"action": action, "slug": slug, "value": val,
                  "body_stale": [], "note": "只改了 frontmatter，正文不受影响"}


def _set_frontmatter(payload: dict, before: dict) -> tuple:
    """通用动作：一次改多个**允许**字段。`fields` 的键已由守卫 1 查过。"""
    slug = str(payload.get("slug") or "").strip()
    fields = dict(payload.get("fields") or {})
    if not fields:
        return False, "fields 为空，没有要改的字段"
    if slug not in before:
        return False, f"草稿不存在：{slug}"
    if "candidates" in fields:
        return False, "改 candidates 请用 candidates / merge 动作（要过一致性守卫）"
    if "tags" in fields:
        # ★ 与 `_set_field` 同一道收敛：这是第二条能写 tags 的路，漏一处就等于
        #   留了个绕过校验的后门（模型恰好用 fields 写法时会写坏列表）。
        tags, why = clean_tags(fields["tags"])
        if why:
            return False, why
        fields["tags"] = tags
    ok, err = _write_fm(slug, fields)
    if not ok:
        return False, err
    wstore.log_append("agent-set", f".drafts/{slug}.md",
                      "frontmatter " + "、".join(fields))
    return True, {"action": "frontmatter", "slug": slug, "fields": fields,
                  "body_stale": []}


def _set_split(payload: dict, before: dict) -> tuple:
    """拆页：`{"move":[3,5], "from":"旧slug", "to":"新slug", "title":..., "domain":...}`。"""
    src = str(payload.get("from") or "").strip()
    dst = str(payload.get("to") or "").strip()
    move = payload.get("move") or []
    if src not in before:
        return False, f"来源草稿不存在：{src}"
    if src == dst:
        # ★ 同页移动是 no-op，但会踩「来源候选清空→归档」分支把整页归档
        #   （2026-11-11 实测：模型想触发重新 polish，伪造 X→X 的移动，
        #   结果真把页归档了）。要重编正文走 polish 工具，不是 set。
        return False, (f"来源与目标相同（{src}），候选没有变化；"
                       "若要重新润色正文请用 polish 工具")
    if not dst or not wstore._safe_slug(dst):
        return False, f"目标 slug 非法：{dst!r}"
    if collision := _published_dst_error(dst, before):
        return False, collision
    try:
        ids = [int(i) for i in move]
    except (TypeError, ValueError):
        return False, f"move 必须是候选 id 列表：{move!r}"
    if not ids:
        return False, "move 为空，没有要挪的候选"
    if len(ids) != len(builtins.set(ids)):
        return False, f"move 里有重复 id：{ids}"
    missing = [i for i in ids if i not in before[src]]
    if missing:
        return False, (f"候选 %s 不在草稿 {src} 里，挪不了"
                       % "、".join(str(i) for i in missing))

    live = live_candidates()
    cands = [live[i] for i in ids if i in live]
    title = str(payload.get("title") or f"从 {src} 拆出的页").strip()[:60]
    domain = str(payload.get("domain") or
                 (wstore.parse_front(_draft_path(src).read_text(encoding="utf-8"))[0]
                  .get("domain") or "")).strip()
    if domain not in wstore.ontology_domains():
        return False, f"新页 domain {domain!r} 不在受控清单里"

    # 目标已存在 = 并入它（追加候选），不是覆盖 —— 覆盖等于静默删一页人的知识
    dst_exists = dst in before
    new_src_ids = [i for i in before[src] if i not in builtins.set(ids)]
    new_dst_ids = list(dict.fromkeys(
        (before[dst] if dst_exists else []) + ids))
    planned = dict(before)
    if new_src_ids:
        planned[src] = new_src_ids
    else:
        # ★ 候选全移走 = 来源页不剩任何内容：归档而不是留一个缺 candidates 的
        #   空壳（2026-11-11 实测：空壳在浮层里报「缺 frontmatter 字段：
        #   candidates」、还占着草稿计数）。归档后它不在可用页清单里，
        #   dst 的 polish 引用它会被链接守卫拒掉，不会写出指向死页的引用。
        del planned[src]
    planned[dst] = new_dst_ids
    why = _check_candidates(planned, before)
    if why:
        return False, why

    # ---- 全通过才落盘：先在内存备好全部内容，再原子替换
    writes = []
    if new_src_ids:
        src_text = _draft_path(src).read_text(encoding="utf-8")
        writes.append((_draft_path(src),
                       _mark_body_stale(_rewrite_fm(
                           src_text, {"candidates": new_src_ids}))))
    if dst_exists:
        dst_text = _draft_path(dst).read_text(encoding="utf-8")
        writes.append((_draft_path(dst),
                       _mark_body_stale(_rewrite_fm(
                           dst_text, {"candidates": new_dst_ids}))))
    else:
        all_cands = [live[i] for i in new_dst_ids if i in live]
        writes.append((_draft_path(dst),
                       _new_page_text(all_cands, title=title, domain=domain,
                                      candidates=new_dst_ids)))
    # ★ polish 顺序契约（2026-11-11 用户拍板）：先重编「候选减少的」页，再重编
    #   「候选新增的」页 —— body_stale 排 src 在前。顺序反了时，dst 的 polish
    #   会引用**还没收缩**的 src 旧正文，引用跟着旧内容乱掉。src 已归档的
    #   情况只有 dst 需要重编（可用页清单里没有 src，引用自然被拒）。
    return _commit_writes(
        writes, "candidates",
        f"{src} → {dst}" + ("" if new_src_ids else "（来源候选清空，已归档）"),
        archive=[] if new_src_ids else [src],
        archive_reason="候选全部移出，自动归档",
        body_stale=([src] if new_src_ids else []) + ([dst] if dst_exists else []))


def _set_merge(payload: dict, before: dict) -> tuple:
    """合并：`{"pages":["A","B"], "to":"C", "title":..., "domain":...}`。

    ★ 2026-11-13（晚）：目标复用来源稿的 slug 时，必须保住那份稿的「身份」——
      覆盖声明（merges/edits/retire_from）、标题、tags、域。旧实现一律
      `_new_page_text` 整份新建，把替换稿的 merges 洗掉、标题被 pages[0] 顶掉：
      入库时当成「撞同名页的新页」拒收（实测：D3 并入 D2 后提交失败）。
      口径与编译链合并（ingest._compose_page）对齐：老标题 / 老 tags / 老域优先。
    ★ 覆盖声明从来源稿继承：全部来源里每个键至多一个目标；出现两个不同目标
      = 结果页该覆盖谁不可判定，拒（猜一个 = 静默改错知识）。
    """
    pages = [str(p).strip() for p in (payload.get("pages") or [])]
    dst = str(payload.get("to") or "").strip()
    if len(pages) < 2:
        return False, "merge 至少需要两页"
    absent = [p for p in pages if p not in before]
    if absent:
        return False, "草稿不存在：" + "、".join(absent)
    if not dst or not wstore._safe_slug(dst):
        return False, f"目标 slug 非法：{dst!r}"
    if collision := _published_dst_error(dst, before):
        return False, collision
    if dst in before and dst not in pages:
        return False, (f"目标 {dst} 已存在且不在合并列表里 —— 会静默覆盖它；"
                       "要并入请用 candidates 动作")

    fms = {p: wstore.parse_front(
        _draft_path(p).read_text(encoding="utf-8"))[0] for p in pages}
    direction = {}
    for p in pages:
        for key in ("merges", "edits", "retire_from"):
            val = str(fms[p].get(key) or "").strip()
            if not val:
                continue
            if key in direction and direction[key] != val:
                return False, (f"合并来源带着两个不同的 {key} 目标"
                               f"（{direction[key]} / {val}），结果页该覆盖谁"
                               "不可判定；请分两步合并")
            direction[key] = val

    ids = list(dict.fromkeys(i for p in pages for i in before[p]))
    live = live_candidates()
    dst_fm = fms.get(dst) or {}
    title = str(payload.get("title") or dst_fm.get("title")
                or fms[pages[0]].get("title") or dst).strip()[:60]
    domain = str(payload.get("domain") or "").strip()
    if dst in pages:
        old_domain = str(dst_fm.get("domain") or "").strip()
        if domain and domain != old_domain:
            return False, (f"合并目标 {dst} 已在域 {old_domain}，不能改成 {domain}"
                           "（覆盖目标页的域是固定的）")
        domain = old_domain
    elif not domain:
        domain = str(fms[pages[0]].get("domain") or "").strip()
    if domain not in wstore.ontology_domains():
        return False, f"合并页 domain {domain!r} 不在受控清单里"
    # tags：目标复用老稿时老标签优先（人手填过的优先，同编译链）；
    # 老稿没标签或目标是新页才取来源并集 —— 整份新建曾把两边 tags 全洗成 []。
    tags = [str(t).strip() for t in (dst_fm.get("tags") or []) if str(t).strip()]
    if not tags:
        tags = list(dict.fromkeys(
            str(t).strip() for p in pages for t in (fms[p].get("tags") or [])
            if str(t).strip()))
    tags = tags[:8]

    planned = {k: v for k, v in before.items() if k not in pages}
    planned[dst] = ids
    why = _check_candidates(planned, before)
    if why:
        return False, why

    if dst in pages:
        # ★ 保身份：按行只换机器字段，merges 等声明行原样留着；正文保留并标
        #   stale 等 polish 重写（整份新建会把旧结论洗成空页）。
        sources = sorted({str(s).strip() for p in pages
                          for s in (fms[p].get("sources") or []) if str(s).strip()})
        text = _mark_body_stale(_rewrite_fm(
            _draft_path(dst).read_text(encoding="utf-8"),
            {"candidates": ids,
             "sources": "[" + ", ".join(sources) + "]",
             "title": title, "tags": tags,
             "updated": timezone.localdate().isoformat()}))
        body_stale = [dst]
    else:
        text = _new_page_text([live[i] for i in ids if i in live],
                              title=title, domain=domain, tags=tags,
                              candidates=ids)
        body_stale = []
    for key, val in direction.items():
        text = wstore._set_fm_line(text, key, val)
    writes = [(_draft_path(dst), text)]
    # ★ 目标允许复用某个旧页的 slug（「都并进 A」），但它刚被写入，
    #   绝不能再归档 —— 否则新页当场被移进 .discarded/，合并结果凭空消失。
    archive = [p for p in pages if p != dst]
    # 旧页归档而不是删除（复用 wiki.discard，留证据好回看）
    return _commit_writes(writes, "merge", "+".join(pages) + " → " + dst,
                          archive=archive, body_stale=body_stale,
                          archive_reason="审批对话合并归档")


def _set_discard(payload: dict, before: dict) -> tuple:
    """丢弃一页：候选一并废弃（内容级否定），草稿移进 .discarded/ 留证据。

    ★ 2026-11-13（晚）语义从「回池重编」改为「一并废弃」，判据与误杀防护
      都在 wstore.discard（仍被其他草稿/页引用的候选不废弃）。
    """
    slug = str(payload.get("slug") or "").strip()
    if slug not in before:
        return False, f"草稿不存在：{slug}"
    planned = {k: v for k, v in before.items() if k != slug}
    why = _check_candidates(planned, before, allow_shrink=True)
    if why:
        return False, why
    sync = {}
    ok, dest = wstore.discard(slug, "审批对话丢弃（候选一并废弃）", sync_report=sync)
    if not ok:
        return False, str(dest)
    wstore.log_append("agent-set", f".drafts/{slug}.md", "discard → " + str(dest))
    out = {"action": "discard", "slug": slug, "archived": dest,
           "released_candidates": sync.get("released_candidates") or [],
           "body_stale": []}
    if sync.get("release_error"):
        out["release_error"] = sync["release_error"]
    return True, out


def _commit_writes(writes: list, action: str, note: str, *,
                   archive=(), body_stale=(), archive_reason="审批对话归档") -> tuple:
    """所有内容先在内存备好 → 逐个写 tmp → 全部成功才 os.replace。

    ★ 不跨文件事务，但把「写坏一个、另一个没写」的窗口压到最小：tmp 全写
      成功之前不动任何正式文件；replace 是同目录原子操作，失败会留 tmp 并在
      日志里报错（**不静默**：出错时谁会知道，是加兜底前必须先回答的问题）。
    """
    for p, text in writes:
        tmp = p.with_suffix(p.suffix + ".tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
        except OSError as e:
            log.error("agent-set 写 tmp 失败 %s：%s（正式文件未改动）", p, e)
            return False, f"写草稿失败：{e}（本动作没有落盘）"
    for p, text in writes:
        tmp = p.with_suffix(p.suffix + ".tmp")
        try:
            os.replace(tmp, p)
        except OSError as e:
            log.error("agent-set 落盘失败 %s：%s", p, e)
            return False, f"落盘失败：{e}（请检查 .drafts/ 里的 .tmp 残留）"
    for slug in archive:
        ok, dest = wstore.discard(slug, archive_reason)
        if not ok:
            log.error("agent-set 归档旧页失败 %s：%s", slug, dest)
            return False, f"合并页已写入，但归档旧页 {slug} 失败：{dest}"
        # ★ 页没了之后，其他草稿 / 已入库页对它的 [[wikilink]] 同步由
        #   `core.wiki.discard()` 统一做（唯一落点，2026-11-13），本层不重复。
    wstore.log_append("agent-set", ".drafts/", f"{action}：{note}")
    return True, {"action": action, "note": note,
                  "written": [p.stem for p, _ in writes],
                  "archived": list(archive), "body_stale": list(body_stale),
                  "polish": "结论待重生成；分组的真相是 frontmatter candidates"}


# ---------------------------------------------------------------- 第十步：删页提案的候选去向编排
#: 「留 → 新建页」在表单里的取值。用常量而不是空串：空串和「没选」在 POST 里
#: 长得一模一样，分不开「人忘了选」和「人明确要新建」。
RETIRE_NEW = "__new__"
_RETIRE_CUTOFF = 0.8


def _retire_fmt(ids) -> str:
    """`retire: [1, 2]` 的字面值。格式化只此一处，避免多处手写拼出脏 frontmatter。"""
    return "[" + ", ".join(str(i) for i in ids) + "]"


def _retire_state(slug: str) -> dict:
    """读一份删页提案的当前真相（原页 + 删除稿 + 全部搬迁稿）。

    ★ 权威清单只有**一个来源**：被删页 frontmatter 的 `candidates:`
      （页还在，这正是把编排前移到提案阶段的全部理由）。删除稿的 `retire`
      是它的落盘副本，搬迁稿的 `candidates` 是「留」的那一半。
    ★ 搬迁稿靠 `retire_from` 认，**扫全集**：只看刚改的那一份，会在「两条
      候选搬去同一页」时算漏。
    """
    p = _draft_path(slug)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return {"err": f"删页提案不存在或读不到：.drafts/{slug}.md"}
    fm, _body = wstore.parse_front(text)
    if not wstore.is_delete_fm(fm):
        return {"err": f".drafts/{slug}.md 不是删页提案（edits + 空候选）"}
    target = str(fm.get("edits") or "").strip()
    page, why = wstore.find_page(target)
    if page is None:
        return {"err": f"要删的页已经不在库里：{why}"}
    old_text = page.read_text(encoding="utf-8")
    old_fm, _ = wstore.parse_front(old_text)
    drafts = wstore.list_drafts()
    source = wstore._cand_ids(old_fm)
    movers = {d["slug"]: d for d in drafts
              if d.get("retire_from") == target and d["slug"] != slug}
    moved_to = {}
    for ms, d in movers.items():
        for cid in d["candidates"]:
            # ★ 搬迁稿里还带着目标页**原有**的候选；只有与 source 相交的那些
            #   才是「从被删页搬来的」。不收窄会把目标页旧候选误标成已搬走，
            #   面板显示一条根本不在被删页上的候选（同守卫的口径）。
            if cid in source:
                moved_to[cid] = ms
    return {
        "path": p, "text": text, "fm": fm, "target": target, "page": page,
        "page_rel": page.relative_to(wstore.root()).as_posix(),
        "old_fm": old_fm, "domain": page.parent.name,
        "source": source,
        "old_format": "retire" not in fm,
        "retired": wstore._retire_ids(fm) if "retire" in fm else list(source),
        "drafts": drafts, "movers": movers, "moved_to": moved_to,
    }


def retire_proposal(slug: str) -> dict:
    """删页提案面板的**只读**上下文：逐条候选 + 推荐去向 + 当前守恒状态。

    返回 `{"ok": False, ...}` 而不是抛异常：面板读不到提案时要能显示一句
    人能懂的原因，不能变成 500 → htmx 不 swap → 整个浮层静默消失。

    ★ 默认全废 = 第八步现状，行为逐字兼容；人只要不动，「留」的集合为空。
    ★ 推荐去向用确定性相似度（同 domain、cutoff 0.8），**零 LLM**：让模型
      猜「该挪去哪页」会编造不存在的 slug，而这里的目标必须真能 `find_page()`。
    ★ `has_items` 是显式布尔：模板里缺上下文变量会被当成空值（记忆里
      「模板缺变量静默走进空兜底」的坑），不能用「变量是否为空」判。
    """
    ok, why = _check_slug(slug)
    if not ok:
        return {"ok": False, "has_items": False, "guard_rings": False, "err": why}
    st = _retire_state(slug)
    if st.get("err"):
        return {"ok": False, "has_items": False, "guard_rings": False,
                "err": st["err"]}

    live = live_candidates()
    pages = [pg for pg in wstore.pages()
             if pg["slug"] != st["target"] and pg["domain"] == st["domain"]]
    targets = []
    for pg in pages:
        targets.append({
            "slug": pg["slug"], "title": pg["title"], "summary": pg["summary"],
            "has_draft": (wstore.root() / ".drafts" / f"{pg['slug']}.md").exists(),
            "is_mover": pg["slug"] in st["movers"],
        })

    items = []
    for cid in st["source"]:
        c = live.get(cid)
        d = _cand_detail(c) if c else {}
        claim = (d.get("claim") or "").strip()
        scored = []
        for t in targets:
            # ★ 分别比 title 与 summary 取**最大值**，不能拼成一个长串再比：
            #   摘要往往比 claim 长得多，拼起来会把「标题完全命中」稀释到
            #   cutoff 以下，结果是所有候选都建议新建页 —— 推荐形同虚设，
            #   而且没有任何一处会报错（设计稿写的是 title/claim 相似度）。
            ratio = max(
                difflib.SequenceMatcher(None, claim, t["title"] or "").ratio(),
                difflib.SequenceMatcher(None, claim, t["summary"] or "").ratio(),
            )
            if ratio >= _RETIRE_CUTOFF:
                scored.append((ratio, t["slug"]))
        recommend = [s for _r, s in sorted(scored, reverse=True)][:3]
        items.append({
            "id": cid,
            "claim": claim or "（候选不存在或已丢弃）",
            "quote": (d.get("quote") or "")[:120],
            "source": d.get("source") or "出处未知",
            "published_at": d.get("published_at") or "发布时间未知",
            "state": "moved" if cid in st["moved_to"] else "retired",
            "target": st["moved_to"].get(cid, ""),
            "recommend": recommend,
            "suggest_new": not recommend,
            "live": c is not None,
        })

    conservation = wstore.check_retire_conservation(
        st["drafts"], live_ids=builtins.set(live))
    return {
        "ok": True, "has_items": True,
        "guard_rings": bool(conservation), "conservation": conservation,
        "slug": slug, "target": st["target"], "page": st["page_rel"],
        "title": st["old_fm"].get("title") or st["target"],
        "domain": st["domain"], "source_ids": st["source"],
        "items": items, "targets": targets,
        "new_slug": f"{st['target']}-kept",
        "retired_n": len([x for x in items if x["state"] == "retired"]),
        "moved_n": len([x for x in items if x["state"] == "moved"]),
        "old_format": st["old_format"],
        "err": "",
    }


def apply_retire_action(slug: str, cand_id, action: str, target: str = "",
                        title: str = "", new_slug: str = "") -> tuple:
    """人在删页提案里点一下「留 → 去 X / 新建页」或「撤回 → 废弃」。

    返回 `(ok, payload_or_err)`。**写盘前一处在内存算完全部内容并过守恒守卫，
    拒写一个字节不动**（`wstore.check_retire_conservation(overlay=...)`）。

    ★ 不复用 `_set_split` / `_check_candidates`：它们的作用域是「草稿之间」，
      本步是「已入库页 → 草稿」（见 `_check_candidates` docstring 的互指）。
      目标为已入库页时先按 `pull_page` 的形状生成 `edits:` 替换稿 —— 直接
      新建一份无 `edits` 的同名稿会撞 `_validate_draft`。
    ★ 三种拒绝分开说（同 `_check_split`）：目标已有待审草稿 / 目标跨域 /
      slug 非法，修法各不相同，合成一句「无法处理」等于把脏活推回给人。
    ★ 零 LLM、只写 `.drafts/`、人 commit 才生效。这里没有花钱端点，所以
      测试里**不需要打桩**（是「没有出口」，不是漏打桩）。
    """
    ok, why = _check_slug(slug)
    if not ok:
        return False, why
    try:
        cand_id = int(cand_id)
    except (TypeError, ValueError):
        return False, f"候选 id 非法：{cand_id!r}"
    st = _retire_state(slug)
    if st.get("err"):
        return False, st["err"]
    # ★ 旧格式删除稿**不静默兜底**：显式回退会发生在本可以逐条编排的提案里，
    #   人以为自己留了一条，实际 commit 按整页全废 —— 比直接拒绝贵得多。
    if st["old_format"]:
        return False, (f".drafts/{slug}.md 是旧格式删除稿（没有 retire 字段），"
                       "逐条去向未生效；请退掉它再重新发起删除请求")
    source, retired, movers = st["source"], st["retired"], st["movers"]
    moved_to = st["moved_to"]
    live = live_candidates()
    action = (action or "").strip().lower()
    want = (target or "").strip()
    writes, archives = [], []

    if action == "move":
        if cand_id in moved_to:
            return False, (f"候选 {cand_id} 已搬去 .drafts/{moved_to[cand_id]}.md，"
                           "先撤回再改去向")
        if cand_id not in retired:
            return False, (f"候选 {cand_id} 不在 .drafts/{slug}.md 的待废弃清单里"
                           "（也不在原页），没有可搬的东西")
        new_retire = [i for i in retired if i != cand_id]
        if not want or want == RETIRE_NEW:
            ns = (new_slug or f"{st['target']}-kept").strip()
            if not wstore._safe_slug(ns):
                return False, f"新页名非法：{ns!r} —— 换个名字（不能有 / 或点开头）"
            if _draft_path(ns).exists():
                return False, f"已有一份同名草稿：.drafts/{ns}.md —— 换个名字"
            if any(pg["slug"] == ns for pg in wstore.pages()):
                return False, f"库里已有同名页 {ns} —— 换个名字"
            c = live.get(cand_id)
            if c is None:
                return False, (f"候选 {cand_id} 已丢弃，不能挂进新页"
                               "（幽灵候选；本动作没有落盘）")
            nt = _new_page_text(
                [c], title=(title or f"从 {st['target']} 留存的判断").strip()[:60],
                domain=st["domain"], candidates=[cand_id])
            nt = wstore._set_fm_line(nt, "retire_from", st["target"])
            # ★★ 搬迁到**新建页**与搬迁到**已有页**必须同口径打 stale（2026-11-09 补）：
            #   两支都产「候选已定、结论还没重编」的稿，只给 else 分支打标记会让
            #   新建页永久 `body_stale=False` —— `_affected_slugs()` 只挑 stale 页
            #   重编，这页于是**永远不会被自动重编**，读侧只剩「结论还没生成」，
            #   而没人知道它还需要重编。标签少一处 = 静默失败一处。
            nt = _mark_body_stale(nt)
            writes.append((_draft_path(ns), nt))
            note = f"候选 {cand_id} 留 → 新建页 .drafts/{ns}.md"
        else:
            if not wstore._safe_slug(want):
                return False, f"目标页名非法：{want!r} —— 换个名字"
            dst_page, why = wstore.find_page(want)
            if dst_page is None:
                return False, f"库里没有这一页：{want} —— 要新建请选「新建页」"
            if dst_page.parent.name != st["domain"]:
                return False, (f"不跨域：被删页在 {st['domain']}，{want} 在 "
                               f"{dst_page.parent.name} 下 —— 可改成新建页")
            dp = _draft_path(want)
            if dp.exists() and want not in movers:
                return False, (f"先处理 .drafts/{want}.md：那页已有一份待审草稿，"
                               "不能悄悄覆盖")
            if want in movers:
                dt = dp.read_text(encoding="utf-8")
                ids = list(dict.fromkeys(
                    wstore._cand_ids(wstore.parse_front(dt)[0]) + [cand_id]))
            else:
                dt = dst_page.read_text(encoding="utf-8")
                ids = list(dict.fromkeys(
                    wstore._cand_ids(wstore.parse_front(dt)[0]) + [cand_id]))
                # ★ 目标为已入库页：先按 pull_page 的形状打上 edits，否则
                #   commit 会把它当「新建一页」并撞「替换目标位置已存在另一页」。
                dt = wstore._set_fm_line(dt, "edits", want)
            dt = wstore._set_fm_line(dt, "retire_from", st["target"])
            dt = _mark_body_stale(_rewrite_fm(dt, {"candidates": ids}))
            writes.append((dp, dt))
            note = f"候选 {cand_id} 留 → .drafts/{want}.md"
    elif action == "retire":
        if cand_id not in moved_to:
            return False, f"候选 {cand_id} 没有搬去任何页，无需撤回"
        # ★ 模块头写明：本模块的工具函数叫 `set`，会遮蔽 builtins.set。
        #   所有集合构造一律 `builtins.set(...)`，裸写会「跑起来才炸」。
        ms, new_retire = moved_to[cand_id], sorted(
            builtins.set(retired) | {cand_id})
        dp = _draft_path(ms)
        dt = dp.read_text(encoding="utf-8")
        ids = [i for i in wstore._cand_ids(wstore.parse_front(dt)[0])
               if i != cand_id]
        if ids:
            dt = _mark_body_stale(_rewrite_fm(dt, {"candidates": ids}))
            writes.append((dp, dt))
        else:
            # ★ 搬迁稿空了就归档，不留一份「覆盖别人页却什么都没改」的无用稿。
            #   第九步最贵的返工就是无关 commit 凭空生成草稿。
            archives.append(ms)
        note = f"候选 {cand_id} 撤回 → 废弃"
    else:
        return False, f"未知的去向动作：{action!r}（只能是 move / retire）"

    delete_text = wstore._set_fm_line(st["text"], "retire", _retire_fmt(new_retire))
    writes.append((st["path"], delete_text))

    # ★ 写盘前把「拟写入的全部文本」当 overlay 交给同一份守恒判据预演。
    #   归档掉的稿要从全集里排除，否则守卫会读到磁盘上的旧内容，判成
    #   「既被废弃又被搬走」—— 守卫自己变假红。
    overlay = {p.stem: t for p, t in writes}
    why = wstore.check_retire_conservation(
        st["drafts"], live_ids=builtins.set(live),
        overlay=overlay, exclude=archives)
    if why:
        log.warning("删页提案动作被守恒守卫拒绝：%s", why)
        return False, why

    ok, res = _commit_writes(writes, "retire", note, archive=archives,
                             body_stale=[p.stem for p, _ in writes
                                         if p.stem != slug])
    if not ok:
        return False, res
    res.update({"drafts": list(res.get("written") or []),
                "moved_n": len(source) - len(new_retire),
                "retired_n": len(new_retire), "note": note})
    return True, res


# ---------------------------------------------------------------- 审批对话（数据模型落地）
APPROVAL_SOURCE_TYPE = "draftbatch"
APPROVAL_SOURCE_ID = "current"


def approval_conversation():
    """取（但不建）当前审批对话。★ 判「审批中」不要用这个，用
    `wstore.approval_active()` —— 对话只代表**当前这一轮**，commit 后会清空。"""
    from core.models import Conversation
    return Conversation.objects.filter(
        source_type=APPROVAL_SOURCE_TYPE, source_id=APPROVAL_SOURCE_ID).first()


def get_or_create_approval_conversation():
    """建/取当前审批对话。

    ★ **不走 `conversation.get_or_create()`**：那个走 `marks.validate_source()`，
      校验的是「能不能给这个内容块挂痕迹」，而 draftbatch 不是内容块（在那里
      被明确拒绝）。审批对话直接落 `Conversation`，语义边界干净。
    ★ source_id 固定 "current" + 表的唯一约束 ⇒ 同时刻只有一条，刷新页面
      不会开出第二条（「有没有草稿待审批」的判据靠文件，不靠这条对话）。
    """
    from core.models import Conversation
    conv, _ = Conversation.objects.get_or_create(
        source_type=APPROVAL_SOURCE_TYPE, source_id=APPROVAL_SOURCE_ID)
    return conv


def clear_approval_conversation() -> bool:
    """清空当前审批对话（连消息一起删）。

    ★ 两个调用点：手动「清空」按钮，以及 commit 成功后的轮次重置
      （`wiki._reset_approval_chat`）—— 2026-11-22 起**每轮审批从零开始**，
      不再往同一条对话里追加批次边界消息。批次记录看 `data/wiki/log.md`。
    """
    from core.models import Conversation
    n, _ = Conversation.objects.filter(
        source_type=APPROVAL_SOURCE_TYPE, source_id=APPROVAL_SOURCE_ID).delete()
    return bool(n)
