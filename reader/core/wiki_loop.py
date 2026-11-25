"""审批对话的 agent loop（第四步）：让 LLM 循环调 read/set，set 后增量重编 polish。

定位（实现设计 v2 §三 / 第四步规格 §四）：`conversation.ask` 是单轮、无工具，
审批对话的 source 是「当前 .drafts/ 全量」，不能复用。本模块与它并列，不改造它。

★ 花钱的出口只有一处：`_call()`。loop 对话与 polish 重编都从这里出去，且在
  **被调用点的命名空间**（`core.wiki_loop._call`）—— 测试桩打这里，不必追到
  `core.citeguard._call`。`tools` 参数区分两种调用：带 TOOLS = loop 对话，
  None = polish 文本生成。

四道程序守卫（不靠 agent 自觉）：
  1. 工具白名单 `ALLOWED_TOOLS`：只认 read / set / polish / merge_into，
     其它一律回「未知工具」；
  2. set 结果如实回填（含守卫拒绝的具体原因），不加工、不吞错；
  3. `MAX_TURNS` 轮次上限 + POOL_WIKI 预算闸（`_call` 自动继承）双保险；
  4. 动作日志：`actions` 结构化返回 + 一条带 question 上下文的 `agent-loop`。

★ 不改 `wiki_agent` 的 read/set 语义；不写视图/URL/模板/浮层（第五步）；
  不实现 commit 物理动作（改动 F）；不做 memory 偏好层（第八步）。

★ `wiki_agent.set` 内部已对每个成功动作落 `agent-set`（**文件粒度**：哪个
  .drafts/ 被改了）。loop 层不逐个 set 再落一条几乎一样的 —— 那会「两条对不上」。
  本模块只在整轮结束时落**一条** `agent-loop`，把「哪句话（question）驱动了
  这一串动作」串起来（**因果粒度**），两者互补、不重复。见 §九实现备注。
"""
import json
import logging

from django.conf import settings

from core import citeguard
from core import costreport as cr
from core import wiki as wstore
from core import wiki_agent as wa

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- 常量（判据收一处）
#: agent loop 轮次上限（守卫 3：防死循环）。规格 §四.2 建议 6 轮。
MAX_TURNS = 6
#: 工具白名单（守卫 1）。read/set 与 wiki_agent 的函数同名，polish 是本模块
#: 自己的（重新生成结论正文，2026-11-11 前没有它，模型靠伪造 set(candidates)
#: 触发重编，把真页归档了）。
ALLOWED_TOOLS = frozenset(("read", "set", "polish", "merge_into"))
#: loop 对话记账 kind（已在 POOL_WIKI 内）。
KIND_CHAT = "wiki_chat"
#: polish 重编复用 `wiki_compile`：不新增 LlmUsage.KINDS choice ⇒ 不触发
#: migration、不动 POOL_KINDS / KIND_ORDER / test_wiki_pool 的写死元组。见 §九。
KIND_POLISH = "wiki_compile"


# ---------------------------------------------------------------- 工具 schema（给 LLM）
READ_TOOL = {
    "type": "function",
    "function": {
        "name": "read",
        "description": "只读地查看当前审批工作台：草稿分组、某页正文与候选明细、候选池、已入库页。",
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["drafts", "draft", "page", "candidates"],
                    "description": "drafts=全部草稿分组摘要；draft=某页草稿全文；"
                                   "page=某已入库页；candidates=未丢弃候选池",
                },
                "slug": {"type": "string", "description": "scope 为 draft/page 时的页 slug"},
            },
            "required": ["scope"],
        },
    },
}

SET_TOOL = {
    "type": "function",
    "function": {
        "name": "set",
        "description": "声明式设置「哪些候选属于哪一页」。落点是草稿 frontmatter 的 "
                       "candidates 与分页；通顺话重编时，正文的候选清单由程序按"
                       "frontmatter 重新生成，受影响页的通顺话随之重写。",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "enum": ["candidates", "merge", "title", "domain", "tags",
                             "discard", "frontmatter"],
                    "description": "candidates=拆页/挪候选；merge=合并；title/domain/tags="
                                   "改字段；discard=丢弃；frontmatter=一次改多个允许字段",
                },
                "payload": {
                    "type": "object",
                    "description": "动作参数，按 target 取用：move/from/to、pages/to、"
                                   "slug、fields、title/domain/tags 等。"
                                   "★ candidates/merge 的 `to` 是**待审草稿**的 slug，"
                                   "不能写已入库页；`fields` 只认 title/domain/tags/candidates，"
                                   "其余机器字段由程序维护，传了会被拒。",
                },
            },
            "required": ["target", "payload"],
        },
    },
}

POLISH_TOOL = {
    "type": "function",
    "function": {
        "name": "polish",
        "description": "重新生成一页草稿的结论正文（按 frontmatter 的 candidates 重排候选明细后重写通顺话）。"
                       "分组不变；候选有增删/挪动时用 set。",
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "要重新生成结论的草稿 slug"},
            },
            "required": ["slug"],
        },
    },
}

MERGE_INTO_TOOL = {
    "type": "function",
    "function": {
        "name": "merge_into",
        "description": "把一份草稿里的**指定候选**并入一个已入库 wiki 页。"
                       "程序生成（或更新）一份与该页同名的 merges 替换稿（候选只增不减），"
                       "自动进入审批队列；提交时会显示改动 diff 并备份旧版后覆盖那页。"
                       "同一目标已有并入稿时就地追加，不会再生出第二份。"
                       "源草稿保留其余候选。会重编两份结论（各一次模型调用）。",
        "parameters": {
            "type": "object",
            "properties": {
                "from": {"type": "string",
                         "description": "源草稿 slug；省略时用工作台当前选中的草稿"},
                "cand_ids": {
                    "type": "array", "items": {"type": "integer"},
                    "description": "要并入的候选 id 列表，如 [217]。必须都在这份草稿里",
                },
                "to": {"type": "string",
                       "description": "目标**已入库页**的 slug（也接受精确标题，如「AI泡沫」）；"
                                      "可用 read(page) 查。待审草稿不能当目标"},
            },
            "required": ["cand_ids", "to"],
        },
    },
}

TOOLS = [READ_TOOL, SET_TOOL, POLISH_TOOL, MERGE_INTO_TOOL]

# ★ 只写「要做什么」，不写「不要/禁止做什么」（项目提示词铁律）。
SYSTEM = """你是「谛听」wiki 的审批助手。当前工作台是 `.drafts/` 里的一批草稿，
每页 frontmatter 的 `candidates` 就是「这页含哪些候选」的分组真值。

你有四个工具：
- `read`：只读地看草稿分组、某页正文与候选明细、候选池、已入库页。
- `set`：声明式设置「哪些候选属于哪一页草稿」。它改的是**草稿之间**的分组
  （frontmatter 的 `candidates` 与分页），结论由程序按最新分组重生成。
  拆页、草稿间合并、改标题/域/标签、丢弃都是它的不同用法。
- `merge_into`：把某几条候选**并入已入库 wiki 页**。给 `from`（源草稿，省略
  即当前选中）、`cand_ids`、`to`（目标页 slug 或精确标题）。程序生成（或就地
  更新）一份 `merges` 替换稿并自动进入审批队列，提交时显示 diff、备份旧版后
  覆盖那页；同一目标已有并入稿时就追加候选，不会再生成第二份。
  我说「#217 并入 AI泡沫」「把某条候选放进已入库页」时用它 —— 这一步会
  重编两份结论，告诉我写进了哪份草稿、源页还剩几条候选。
- `polish`：只重新生成某页草稿的结论正文（分组不变）。我说「重新 polish / 重编 /
  润色」某页时用它；它不接受候选变化，候选有变化才用 set。

工作方式：先用 read 看清当前分组和候选，再按我的要求用 set 调整草稿分组、
用 merge_into 处理「并入已入库页」；返回会告诉你成功写了哪些页、哪些正文
待重编，或被拒的具体原因。每轮可以调一个或多个工具；把要办的事都办完后，
用一句话回复我你做了什么。中文、先结论后依据、简洁。

★ 当前选中的草稿在 briefing 里标了「← 当前选中」。我没指定哪页时，默认就是
改这页；不要猜、不要列一堆候选页让我选。

★ 草稿编号 D<n> 是稳定 id（briefing 里每行 D 后面就是 slug），不随列表顺序变。
我说「D2」就指 briefing 里标 D2 的那份，把它映射成对应 slug 再操作；合并/拆分后
编号也不跳变，别按位置重新数。

★ 改标题时 slug 同步更新（重要）：
  - **不要传 new_slug 字段**，程序会自动处理：
    - 英文标题：从标题生成 slug（如 "Understanding the Business" → "understanding-the-business"）
    - 中文标题：用拼音生成 slug（如 "AI泡沫" → "ai-pao-mo"，"投资的心法" → "tou-zi-de-xin-fa"）
  - 程序会**忽略**不达标的 new_slug（单个词如 "ai"、或等于原 slug 的），
    仍自动生成 —— slug 永远跟着标题走，不会留在原地
  - 只有在我明确要求指定某个特定 slug 时才传 new_slug，且必须是描述性英文
    （至少 3 个词或 15+ 字符，如 "ai-bubble-backlash"）
  - 例：set("title", {"slug": "old-name", "title": "投资心法"})  ← 不要传 new_slug
"""

# ★ polish prompt：基于候选写正文，严谨性由左栏候选明细保证，这里只负责可读性。
POLISH_SYSTEM = """基于候选明细为 wiki 页写正文。

精炼陈述候选的核心内容，不额外展开、不发散、不加背景解释。
候选少则一两句话，候选多则分要点简述。不以"结论"/"核心判断"开头。
[[slug]] 链接只在正文自然需要时带出，不强行塞入。
"""

#: 一次最多把多少页列进 polish prompt（防页多了把素材挤爆）。
POLISH_MAX_PAGES = 60

#: ★ 带反馈的重试（2026-11-22）：校验失败时把**这次的具体原因**回喂模型再编一次。
#:   提示词里写「slug 逐字照抄」只是承诺，程序事实是这次拒写的原因进了下一次 messages。
#:   只重试一次：校验失败是模型可修正的；生成错误（网络/预算/空正文）不重试 ——
#:   citeguard 已对 truncated 翻过倍，预算闸是确定性的，再调只是白花钱。
POLISH_FEEDBACK = ("上一次结论重编被拒写，原因如下。请据此重写整段结论，只输出结论正文：\n"
                   "%s\n\n"
                   "★ [[slug]] 引用必须从上面「可引用的页」清单里逐字复制；"
                   "清单里没有的页一律不要引用。")
POLISH_RETRY_MAX = 1


# ---------------------------------------------------------------- 唯一 LLM 出口（被调用点）
def _call(kind, messages, *, tools=None, tag="", label=""):
    """本模块唯一的 LLM 调用出口，返回 (text, tool_calls, err)。

    ★ 桩打在 `core.wiki_loop._call`（被调用点），不是 `core.citeguard._call`。
    ★ tool_calls 由 `citeguard._call` 写进 `stats["tool_calls"]` 带出；
      `tools=None` 时是纯文本调用（polish），带 TOOLS 时是工具调用（loop）。
    """
    stats = citeguard.fresh_stats()
    text, err = citeguard._call(
        kind, "", "", stats,
        messages=messages, tools=tools, max_calls=1,
        max_tokens=int(settings.WIKI_CHAT_MAX_TOKENS),
        temperature=float(settings.WIKI_CHAT_TEMPERATURE),
        pool=cr.POOL_WIKI, tag=tag, label=label)
    return text, list(stats.get("tool_calls") or ()), err


def _call_stream(kind, messages, on_delta, *, tools=None, tag="", label=""):
    """流式版 `_call`（2026-11-19）：最后一轮纯文本答复逐字吐。

    ★ 加法式：不碰 `_call()`（测试桩 `core.wiki_loop._call` 钉死）。
    ★ 工具轮也走这里：`run()` 首轮就带 TOOLS 调本函数（事先不知道模型要不要调
      工具），所以 tool_calls 由下层 `citeguard._call_stream` 写回 stats，再由
      本函数带出给 loop 执行。2026-11-22 之前此处误以为「带工具的轮次不走流式」，
      下层也漏写 stats ⇒ 审批对话里说「合并」只回一句「模型没有返回内容」。
    """
    stats = citeguard.fresh_stats()
    text, err = citeguard._call_stream(
        kind, "", "", stats, on_delta=on_delta,
        messages=messages, tools=tools, max_calls=1,
        max_tokens=int(settings.WIKI_CHAT_MAX_TOKENS),
        temperature=float(settings.WIKI_CHAT_TEMPERATURE),
        pool=cr.POOL_WIKI, tag=tag, label=label)
    return text, list(stats.get("tool_calls") or ()), err


# ---------------------------------------------------------------- 工作台摘要 / 提示拼接
def _briefing(selected_slug: str = None) -> str:
    """system 消息里的「当前工作台」：草稿全量摘要 + 候选池摘要（read 只读）。
    
    ★ selected_slug：当前选中的草稿，标注在列表里让 agent 知道用户在说哪页。
    ★ 草稿编号 D<draft_id>：稳定自增 id（不是列表位置），合并/拆分/改名/重排都不
      跳变；用户在对话里可直接说「把 #203 挪到 D2」，D2 永远指同一份稿。
    ★ 候选用 cand_id（#201/#202）：跨草稿的全局唯一 ID，不是每页重新编号。
    """
    drafts = wa.read("drafts")
    cands = wa.read("candidates")
    lines = ["# 当前审批工作台", "",
             f"草稿共 {drafts.get('count', 0)} 页；审批中：{drafts.get('approval_active')}"]
    if selected_slug:
        lines.append(f"★ 当前选中：{selected_slug}")
    for d in drafts.get("drafts", []):
        marker = " ← 当前选中" if d['slug'] == selected_slug else ""
        lines.append(f"- D{d['draft_id']} {d['slug']}｜{d['title']}｜domain={d['domain']}｜"
                     f"candidates={d['candidates']}｜complete={d['complete']}{marker}")
    lines += ["", f"候选池共 {cands.get('count', 0)} 条（未丢弃）"]
    for c in cands.get("candidates", [])[:20]:
        lines.append(f"- #{c['cand_id']} {c['claim']}｜{c['source']}｜{c['published_at']}")
    return "\n".join(lines)


def _authoritative_body(info: dict) -> str:
    """按 frontmatter `candidates` 重排出的**候选明细素材**（纯喂模型，不落盘）。

    ★ 第十三步 A：正文只留 polish 结论，这份清单不再写进文件。它同时是
      `wstore.set_polish` 守卫的比对底本 —— 素材与写回共用这一份，别一处算一处。
    """
    details = {c["cand_id"]: c for c in (info.get("candidate_detail") or [])}
    return wstore.candidate_list_text(info.get("candidates") or [], details)


def _wikilinks(text: str) -> list:
    """正文里的 `[[slug]]`（去重、保序）。判据复用 `wstore._WIKILINK_RE` 一份。"""
    return list(dict.fromkeys(wstore._WIKILINK_RE.findall(text or "")))


def _available_pages(slug: str) -> list:
    """可引用的页：已入库页 + 待审批草稿，排除自己。

    ★ 不喂页目录时模型会凭空造 slug（要等 `broken_links()` 事后报警，太晚）。
      上限 `POLISH_MAX_PAGES` 防页多了把候选明细挤爆。
    """
    out, seen = [], set()
    for p in list(wstore.pages()) + list(wstore.list_drafts()):
        s = str(p.get("slug") or "").strip()
        if not s or s == slug or s in seen:
            continue
        seen.add(s)
        out.append({"slug": s, "title": str(p.get("title") or s)})
        if len(out) >= POLISH_MAX_PAGES:
            break
    return out


def _polish_material(material: str, *, pages: list, old_links: list) -> str:
    """polish 素材：重排后的候选明细 + 可引用页 + 本页已有引用。

    ★ 三样缺一不可：明细是内容依据，页目录让 `[[slug]]` 有可引用对象，
      老引用清单让重编守住「已有页间关联不丢」（§2.5B/C）。
    """
    lines = ["# 本页候选明细（按 frontmatter `candidates` 重排）", "", material, "",
             "# 读法",
             "每条 `#id` 行是本页的理解，缩进的「原话」是支撑它的证据；「来自」"
             "标注这条是谁说的，写正文时按这个层次组织。", "",
             "# 可引用的页（slug 逐字照抄，只在真正相关时引用）"]
    lines += ([f"- {p['slug']}｜{p['title']}" for p in pages]
              or ["（暂时没有别的页）"])
    lines += ["", "# 本页已有的引用（如仍相关则保留）"]
    lines += ([f"- [[{s}]]" for s in old_links] or ["（暂无）"])
    return "\n".join(lines)


def _check_links(text: str, pages: list, old_links: list) -> tuple:
    """产出 `[[slug]]` 的命中校验。返回 (ok, err)。

    ★ 只校验越界（引用不存在的页），不强制保留老引用：
      正文应该自然流畅，链接只在真正相关时出现，不相关的可以省略。
    """
    available = {p["slug"] for p in pages}
    made = _wikilinks(text)
    stray = [s for s in made if s not in available]
    if stray:
        return False, ("正文里引用了不存在的页：%s（可引用清单里没有，已拒写）"
                       % "、".join(stray))
    return True, ""


def _check_numbers(text: str, material: str) -> tuple:
    """G4 数字守卫按形态 A 重写：守**结论正文**里的数字，不再守三块列表。

    ★ 口径沿用 `reporttext.number_audit`（只核 ≥3 位 / 带小数的数字），不为
      「更严」改口径 —— 天天误报的守卫比没有更糟。底本是同一份候选明细素材。
    ★ 三块退役后这是唯一还在守「精确数据别被模型改写」的地方，别删。
    """
    from core.reporttext import number_audit
    bad = number_audit(text, material)
    if bad:
        return False, ("结论里出现素材里没有的数字：%s（已拒写，先核候选）"
                       % "、".join(bad))
    return True, ""


# ---------------------------------------------------------------- polish 增量重编（花钱）
def _affected_slugs(set_payload) -> list:
    """★ 增量重编的判据收在这一处：只重编**本次 set 影响**的页，不是整库。

    = `body_stale`（正文变旧的旧页）∪ `written`（本次写入的页，含新建页），
    排除已归档页。交互文档 §5.3 的闭环要求「拆页后两个文件的 polish 都重生成」：
    只认 `body_stale` 会让新建页永远没有通顺话，而工具描述却承诺会重生成。
    反证 4 就是把这个判据放宽成「所有 draft」看测试是否变红。
    ★ 顺序即契约（2026-11-11 用户拍板）：`body_stale` 排在 `written` 前，
    且拆页类动作内部把「候选减少的」来源页排在「候选新增的」目标页前 ——
    **先 polish 减少的，后 polish 新增的**。顺序反了时，目标页的 polish 会
    引用来源页还没收缩的旧正文，引用跟着旧内容乱掉。
    """
    if not isinstance(set_payload, dict):
        return []
    archived = {str(s) for s in (set_payload.get("archived") or [])}
    out = []
    for s in (list(set_payload.get("body_stale") or [])
              + list(set_payload.get("written") or [])):
        s = str(s)
        if s and s not in archived and s not in out:
            out.append(s)
    return out


def _polish_one(slug: str) -> tuple:
    """重编一页**结论**：候选明细 → LLM → 写回纯 polish 正文（形态 A）。

    ★ 写回走 `wstore.set_polish(slug, text, material)`，它校验「素材里的 `#id`
      == frontmatter candidates」，并**成功才剥 `stale`**。
    ★ 写回前先过 `_check_links`：引用不存在页 / 丢老引用一律拒写。
    ★★ 带反馈的重试（2026-11-22）：校验被拒时，把**具体原因**回喂模型再编一次
      （上限 `POLISH_RETRY_MAX`）；上次失败原因落在草稿 frontmatter
      `polish_error`，下次点按钮/审批对话重编时也会作为反馈带上 —— 盲重试
      会照样犯同一个错（11-22 实测：模型按标题猜了旧 slug，没照抄清单）。
    ★ 出错分支一律 log.error + 落盘原因 + 返回人能看懂的话，不静默吞掉：
      此时 `stale` 标记仍在，审批浮层看得见「结论还没重生成」和失败原因。
    """
    info = wa.read("draft", slug)
    if not info.get("ok"):
        why = info.get("err") or "读不到草稿"
        log.error("结论重编读不到草稿 %s：%s", slug, why)
        return False, why
    material = _authoritative_body(info)
    pages = _available_pages(slug)
    # ★ 死引用不进「已有的引用（如仍相关则保留）」清单：改名/归档的残留链接
    #   若喂给模型，它会照保留，然后被 _check_links 拒写，重新 polish 空转
    #   （2026-11-11 实测：旧服务改名没同步正文链接，polish 反复被拒）。
    available = {p["slug"] for p in pages}
    old_links = [s for s in _wikilinks(info.get("body") or "") if s in available]
    # ★ 上次失败原因（跨请求/跨重启）：写入 messages 而不是只写进日志。
    prior = str((info.get("fm") or {}).get(wstore.POLISH_ERR_FM) or "").strip()
    messages = [{"role": "system", "content": POLISH_SYSTEM},
                {"role": "user", "content": _polish_material(
                    material, pages=pages, old_links=old_links)}]
    if prior:
        messages.append({"role": "user", "content": POLISH_FEEDBACK % prior})

    def _fail(why: str, *, tried: int) -> tuple:
        # ★ 原因和「正文没改 / stale 仍在」同一笔落盘（save_draft 机器字段通道）：
        #   分开写会留下「标记说待重编、原因却丢了」的半截真相。
        try:
            wstore.save_draft(slug, info.get("body") or "",
                              set_fm={wstore.POLISH_ERR_FM: why})
        except Exception as exc:  # noqa: BLE001  写盘失败不吞：日志明说，返回值仍给人看
            log.warning("结论失败原因落盘失败 %s：%s", slug, exc)
        log.error("结论重编失败 %s（尝试 %d 次）：%s（该页仍带待重编标记）",
                  slug, tried, why)
        return False, why

    def _validate(text: str) -> tuple:
        ok, why = _check_links(text, pages, old_links)
        if not ok:
            return False, why
        return _check_numbers(text, material)

    text, _, err = _call(KIND_POLISH, messages, tools=None,
                         tag="wiki 结论重编", label=slug)
    if err or not (text or "").strip():
        return _fail(err or "模型没有返回结论", tried=1)
    ok, why = _validate(text)
    # ★ 只有「可修正的校验失败」才带反馈重试；生成错误不重试（理由见常量注释）。
    tried = 1
    if not ok and not prior and POLISH_RETRY_MAX > 0:
        log.warning("结论校验未通过 %s：%s —— 带反馈重试 1 次", slug, why)
        messages.append({"role": "user", "content": POLISH_FEEDBACK % why})
        retried, _, err2 = _call(KIND_POLISH, messages, tools=None,
                                 tag="wiki 结论重编（带反馈）", label=slug)
        tried = 2
        if err2 or not (retried or "").strip():
            return _fail(err2 or "模型没有返回结论", tried=tried)
        text = retried
        ok, why = _validate(text)
    if not ok:
        return _fail(why, tried=tried)
    ok, why = wstore.set_polish(slug, text, material)
    if not ok:
        return _fail(str(why), tried=tried)
    return True, ""


# ---------------------------------------------------------------- 工具执行 + 守卫 1/2/4
def _exec_tool(tc: dict, actions: list, *, selected_slug: str = None) -> dict:
    """执行一条 tool_call，把结果如实回填成 tool 消息，并记进 actions。

    ★ selected_slug：当前选中的草稿。set 因「草稿不存在」被拒时，把当前选中
      附在原因后面 —— 模型传错 slug 时下一轮能自己纠回来，而不是再猜一个。
    """
    fn = tc.get("function") or {}
    name = (fn.get("name") or "").strip()
    try:
        args = json.loads(fn.get("arguments") or "{}")
        if not isinstance(args, dict):
            args = {}
    except (ValueError, TypeError):
        args = {}

    # 守卫 1：工具白名单。未知名字不执行任何函数，回一条人能看懂的结果。
    if name not in ALLOWED_TOOLS:
        result = {"ok": False,
                  "err": f"未知工具：{name!r}（只允许 read / set / polish / merge_into）"}
        actions.append({"tool": name, "args": args, "ok": False,
                        "result": result, "repolished": []})
        return result

    if name == "merge_into":
        # ★ 落点在 core 层，视图/loop 都不复制「目标是不是已入库页」的判据。
        #   `from` 省略时回落到当前选中（与 SYSTEM 的承诺一致），模型不用猜 slug。
        from core import ingest
        src = str(args.get("from") or "").strip() or (selected_slug or "")
        ok, payload = ingest.attach_candidates_to_page(
            src, args.get("cand_ids") or [], str(args.get("to") or "").strip())
        logged = dict(args)
        logged["from"] = src
        actions.append({"tool": "merge_into", "args": logged, "ok": ok,
                        "result": payload,
                        "repolished": (payload.get("polished") or []) if ok else []})
        return {"ok": ok, "result": payload}

    if name == "read":
        res = wa.read(str(args.get("scope") or ""), str(args.get("slug") or ""))
        actions.append({"tool": "read", "args": args, "ok": bool(res.get("ok")),
                        "result": res, "repolished": []})
        return {"ok": True, "result": res}

    if name == "polish":
        # 只重编一页结论，不改分组。slug 传错时把当前选中附在原因后面，
        # 模型下一轮能自己纠回来（与 set 的「草稿不存在」处理同一理由）。
        slug = str(args.get("slug") or "").strip()
        good, why = _polish_one(slug)
        if not good and selected_slug and "草稿不存在或读不到" in why:
            why = f"{why}（当前选中的草稿是 {selected_slug}）"
        actions.append({"tool": "polish", "args": args, "ok": good,
                        "result": why, "repolished": [slug] if good else []})
        if good:
            return {"ok": True, "result": f"已重新生成 {slug} 的结论正文"}
        return {"ok": False, "result": why}

    # set：守卫 2 —— (ok, payload_or_err) 如实回填，不加工、不吞错（守卫拒了，
    # LLM 下一轮会从 tool 消息里看到具体原因自行重试）。
    ok, payload = wa.set(str(args.get("target") or ""), args.get("payload") or {})
    if not ok and selected_slug and isinstance(payload, str) \
            and "草稿不存在" in payload:
        payload = f"{payload}（当前选中的草稿是 {selected_slug}）"
    result = {"ok": ok, "result": payload}
    # 守卫 4：成功时增量重编「本次 set 影响」的页（旧的 + 新建的），不含其它页。
    affected = _affected_slugs(payload) if ok else []
    repolished = []
    for s in affected:
        good, why = _polish_one(s)
        if good:
            repolished.append(s)
        else:
            log.error("结论重编 %s 未完成：%s（该页仍带待重编标记）", s, why)
    actions.append({"tool": "set", "args": args, "ok": ok, "result": payload,
                    "affected": affected, "repolished": repolished})
    return result


# ---------------------------------------------------------------- 人可读汇总 / 日志
def _action_lines(actions: list) -> str:
    lines = []
    for a in actions:
        if a["tool"] == "read":
            lines.append(f"- read({a['args'].get('scope', '')}) → "
                         f"{'成功' if a['ok'] else '失败'}")
        elif a["tool"] == "set":
            line = f"- set({a['args'].get('target', '')}) → " \
                   f"{'成功' if a['ok'] else '被拒'}"
            # 被拒原因如实带上：只有「被拒」两个字时，人没法知道模型错在哪
            # （2026-11-11 实测：X→X 的假移动被归档守卫放过后又撞轮次上限，
            # 汇总里只看到「被拒」×2，追日志才找到原因）。
            if not a["ok"] and a.get("result"):
                line += "：" + str(a["result"])[:120]
            lines.append(line)
            if a.get("repolished"):
                lines.append(f"  · 结论已重编：{'、'.join(a['repolished'])}")
        elif a["tool"] == "polish":
            lines.append(f"- polish({a['args'].get('slug', '')}) → "
                         f"{'成功' if a['ok'] else '失败'}")
            if not a["ok"] and a.get("result"):
                lines.append("  · " + str(a["result"])[:120])
        elif a["tool"] == "merge_into":
            ar, res = a["args"], a.get("result")
            line = ("- merge_into(#%s：%s → %s) → %s" % (
                "、".join(str(i) for i in (ar.get("cand_ids") or [])),
                ar.get("from") or "", ar.get("to") or "",
                "成功" if a["ok"] else "被拒"))
            if not a["ok"] and res:
                line += "：" + str(res)[:160]
            lines.append(line)
            if a["ok"] and isinstance(res, dict):
                # ★ 结果草稿不一定叫目标页：已有并入稿可能是旧版「原地改判」
                #   留下的源稿名（2026-11-13）。报 payload 的 draft，别让日志
                #   指向一份不存在的 `.drafts/<to>.md`。
                verb = "已更新" if res.get("existing") else "已生成"
                line = ("  · %s .drafts/%s（并入 %s，提交时显示 diff）；" % (
                    verb, res.get("draft") or res.get("to"),
                    res.get("to") or res.get("target") or ""))
                if res.get("kept") is not None:
                    line += "源页留 %d 条候选" % len(res.get("kept") or [])
                else:
                    line += "源稿已归档"
                lines.append(line)
                if res.get("polish_failed"):
                    lines.append("  · 结论重编未完成：%s（草稿仍带待重编标记）"
                                 % "；".join(res["polish_failed"]))
        else:
            lines.append(f"- 未知工具 {a['tool']} → 已忽略")
    return "\n".join(lines)


def _log_note(q: str, actions: list) -> str:
    sets = [a for a in actions if a["tool"] == "set"]
    ok_n = sum(1 for a in sets if a["ok"])
    repolished = sorted({s for a in actions for s in (a.get("repolished") or [])})
    bits = [f"问：{q[:80]}", f"set {ok_n}/{len(sets)} 成功", f"动作 {len(actions)} 条"]
    mi = [a for a in actions if a["tool"] == "merge_into"]
    if mi:
        bits.append("merge_into %d/%d 成功"
                    % (sum(1 for a in mi if a["ok"]), len(mi)))
    if repolished:
        bits.append("结论重编：" + "、".join(repolished))
    return "；".join(bits)


# ---------------------------------------------------------------- 入口
def run(question: str, *, selected_slug: str = None, on_delta=None) -> dict:
    """审批对话里说一句话，跑一轮 agent loop。返回 {ok, err, reply, actions}。

    ★ on_delta（2026-11-19，SSE）：给了就边生成边回调。工具轮的 content 通常为空
      （模型只回 tool_calls），吐出来是无害的空串；真正有字的是最后一轮纯文本答复。
      默认 None 走原 `_call`，测试桩 `core.wiki_loop._call` 逐字节不变。

    - 复用 `wiki_agent.get_or_create_approval_conversation()` 取/建对话；
    - question 先落库为 user 消息（模型失败也要留，与 conversation.ask 同理由）；
    - 循环调 LLM：要么调 read/set（结果回填成 tool 消息再问一轮），要么给最终回复；
    - 撞 MAX_TURNS / 预算时如实汇总已执行动作返回，不静默。
    - selected_slug：当前选中的草稿，用于在 briefing 中标注，让 agent 知道用户在说哪页。
    """
    q = (question or "").strip()
    if not q:
        return {"ok": False, "err": "问题不能为空", "reply": "", "actions": []}
    q = q[:max(1, int(settings.WIKI_CHAT_QUESTION_CHARS))]

    conv = wa.get_or_create_approval_conversation()
    # 先取历史再落库本条，避免「历史里含本条 + 本次又发一次」的重复。
    limit = max(0, int(settings.WIKI_CHAT_HISTORY))
    hist = list(conv.messages.order_by("-id")[:limit]) if limit else []
    hist = list(reversed(hist))
    conv.messages.create(role="user", body=q)

    messages = [{"role": "system", "content": SYSTEM + "\n\n" + _briefing(selected_slug)}]
    messages += [{"role": m.role, "content": m.body} for m in hist]
    # ★ 当前选中附在**本轮 user 消息**尾部（只发给模型，不落库）：system 里的
    #   briefing 标了「← 当前选中」，但历史里上一轮的动作会盖过它的显著性 ——
    #   实测「第二次对话改错草稿」就是模型跟着自己上一轮的工具参数走。
    #   附在本轮提问上，让「选中的是这页」成为最新一条指令。
    user_text = q
    if selected_slug:
        user_text = f"{q}\n\n（工作台当前选中的草稿：{selected_slug}）"
    messages.append({"role": "user", "content": user_text})

    actions, reply, err = [], "", ""
    for _turn in range(MAX_TURNS):
        if on_delta:
            text, tool_calls, call_err = _call_stream(
                KIND_CHAT, messages, on_delta, tools=TOOLS,
                tag="wiki 审批对话", label=q[:40])
        else:
            text, tool_calls, call_err = _call(
                KIND_CHAT, messages, tools=TOOLS,
                tag="wiki 审批对话", label=q[:40])
        if call_err:                       # 预算 / 模型失败：不静默，原因回显
            err = call_err
            break
        if tool_calls:
            # assistant(tool_calls) + 每条 tool 结果回填进消息体（给下一轮 LLM）。
            messages.append({"role": "assistant", "content": text or "",
                             "tool_calls": tool_calls})
            for tc in tool_calls:
                result = _exec_tool(tc, actions, selected_slug=selected_slug)
                messages.append({"role": "tool",
                                 "tool_call_id": tc.get("id") or "",
                                 "content": json.dumps(result, ensure_ascii=False,
                                                       default=str)})
            continue
        reply = (text or "").strip()
        if not reply:
            err = "模型没有返回内容"
        break
    else:
        # for 正常耗尽 = 撞轮次上限（守卫 3）：如实汇总，不静默。
        err = f"达到轮次上限 {MAX_TURNS}，已停止（防死循环）"
        # ★ 动作明细只出一份：下面的 parts 已 append「【本轮动作】」的全量
        #   `_action_lines`，这里再列一遍会把同一串动作打印两次（2026-11-13 实测）。
        reply = ("本轮已执行的动作见上；这一轮没能收尾，请继续说明或换个说法。"
                 if actions else "本轮没有执行任何动作")

    # 落库：一条 user（已落）+ 一条 assistant（含工具动作说明 + 最终回复/失败原因）。
    parts = []
    if actions:
        parts.append("【本轮动作】\n" + _action_lines(actions))
    if reply:
        parts.append(reply)
    if err:
        parts.append(f"（{err}）")
    body = "\n\n".join(p for p in parts if p)
    if body:
        conv.messages.create(role="assistant", body=body)
    conv.save(update_fields=["updated_at"])

    # 守卫 4 的动作日志：一条带 question 上下文的 agent-loop（不重复落 agent-set）。
    if actions:
        wstore.log_append("agent-loop", ".drafts/", _log_note(q, actions))

    return {"ok": not err, "err": err, "reply": reply, "actions": actions}
