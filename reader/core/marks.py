"""划线（Highlight）与批注（Annotation）的存储层（阶段 1，零 LLM）。

定位：人对「内容块」留下的判断 —— 划线 = 摘录原文，批注 = 我的看法。
两者都挂 `(source_type, source_id)` 软引用（见 core/models.py 的 SOURCE_TYPES），
**不建 FK**：真相在 Miniflux / 报告文件 / 未来的剪藏与对话，本地只存「我的东西」。

★ 为什么不放进 core/wiki.py：那个模块「只碰文件、不碰 DB」（它的 docstring 是
  纪律），而 Highlight/Annotation 是 Django 模型。文件 vs DB 是两种事实源，
  混在一个模块里会让「哪些进 wiki 页、哪些进数据库」这条边界重新变模糊。

★ source_id 规范化是**有真实规则**的地方，不能只在视图里随手 `.strip()`：
  - entry  → 必须是十进制正整数（= Miniflux entry_id）；
  - report → 必须形如 `daily-YYYY-MM-DD` / `weekly-YYYY-MM-DD`（与 reportstore 文件名、
    SummaryRun.kind 同构），否则一句话就能伪造一个不存在的报告 id；
  - chat   → 必须是**已存在**的 ChatMessage id（2026-10-31 启用）：对话里的总结/回复
    也要能划线批注。判据是「这条消息真的在库里」，不是「id 长得像数字」—— 否则
    前端能凭空造一个指向不存在消息的引用，页面上永远看不见也删不掉。
  - web    → 阶段 4 才启用，现在**拒绝写入**（枚举预留 ≠ 现在就能写）。
  这里统一 `validate_source()` 一份实现，视图只转发，不复制判据。
"""
import json
import re
import logging

from django.utils import timezone

from core.models import Annotation, Highlight, IngestCandidate, SOURCE_TYPES

log = logging.getLogger(__name__)

_VALID_TYPES = {k for k, _ in SOURCE_TYPES}

# report 的 source_id 形状：daily-2026-10-31 / weekly-2026-11-01
_REPORT_RE = re.compile(r"^(daily|weekly)-\d{4}-\d{2}-\d{2}$")
_ENTRY_RE = re.compile(r"^\d{1,19}$")          # 正整数，上限对齐 BigInteger


def validate_source(source_type: str, source_id: str):
    """校验并规范化 (source_type, source_id)。

    返回 (source_type, source_id, err)：
      - 合法时 err 为空串，source_id 已规范化（entry 去前导零、report 转小写）；
      - 非法时 source_type/source_id 原样返回、err 是人话原因。

    ★ 这是唯一实现：任何要写 Highlight/Annotation 的入口（现在只有 hx 端点，
      将来 chrome 插件 / 对话回写）都过这里，别再各自手写判据。
    """
    t = (source_type or "").strip()
    sid = (source_id or "").strip()

    if t not in _VALID_TYPES:
        return t, sid, f"未知来源类型：{t or '（空）'}"
    if t == "web":
        # ★ 枚举预留 ≠ 现在能写：剪藏对象阶段 4 才出现。
        return t, sid, f"来源类型 {t} 尚未启用（阶段后续版本支持）"

    if t == "chat":
        # ★ 2026-10-31 启用：对话的总结/回复也要能划线批注。
        #   判据是「这条消息真的在库里」，不是「id 是数字」—— 否则能造出
        #   指向不存在消息的引用，页面永远看不见也就永远删不掉。
        if not _ENTRY_RE.match(sid):
            return t, sid, "chat 的 source_id 必须是消息 id（正整数）"
        sid = str(int(sid))
        from core.models import ChatMessage
        if not ChatMessage.objects.filter(pk=int(sid)).exists():
            return t, sid, "这条对话消息不存在（可能已被清空）"
        return t, sid, ""

    if t == "entry":
        if not _ENTRY_RE.match(sid):
            return t, sid, "entry 的 source_id 必须是正整数（= 条目 id）"
        sid = str(int(sid))                     # 去前导零，统一形状
        return t, sid, ""

    if t == "report":
        if not _REPORT_RE.match(sid.lower()):
            return t, sid, "report 的 source_id 必须形如 daily-2026-10-31 / weekly-2026-11-01"
        return t, sid.lower(), ""

    # ★ 审批对话（第三步）**不是内容块**：它没有正文，不能挂划线 / 批注。
    #   明确拒绝并说清去哪，别让它落到下面那句「缺少写入实现」—— 那会让人
    #   以为只是还没实现、顺手补个分支就放行，语义就乱了。审批对话的建/取
    #   直接走 `Conversation.objects`，见 core.wiki_agent。
    if t == "draftbatch":
        return t, sid, ("审批对话不是内容块，不能挂划线 / 批注；"
                        "要交流请在审批浮层的对话里说")
    # 新增 type 忘了补分支时宁可拒绝也不要静默放行。
    return t, sid, f"来源类型 {t} 缺少写入实现"


def _anchor_json(anchor) -> str:
    """把 anchor 规范成落库字符串。

    ★ anchor 有两种合法形态（阶段 1 的「选中即标注」交互引入）：
      - 空串：整篇级（历史遗留 / 没选中文本直接写批注）；
      - JSON：文本锚点 `{"text", "prefix", "suffix"}`，用于渲染时回填 inline 高亮。
    前端选中文本时传 JSON 字符串过来，这里校验后原样存 —— 不做二次解析，
    因为「存什么」与「渲染时怎么用」是两层，别把判据复制到这里。
    """
    a = (anchor or "").strip()
    if not a:
        return ""
    # 只有 JSON 才收：其它随意字符串（历史误传）不留，避免渲染层拿到垃圾。
    try:
        obj = json.loads(a)
    except (ValueError, TypeError):
        return ""
    if not isinstance(obj, dict):
        return ""
    return json.dumps(obj, ensure_ascii=False)


def add_highlight(source_type, source_id, quoted_text, note="", anchor=""):
    """存一条划线。返回 (ok, payload)。payload = {'id', 'err', 'restored'}。

    ★ quoted_text 空则拒：划线的最小语义是「摘了一句原文」，空摘录没有意义。
    ★ 复活（2026-11-05）：`(source_type, source_id, 归一化摘录)` 三者全同的
      **软删**记录，直接把 `deleted_at` 置空复用旧 id，不新建一条。理由：删了又
      划回同一句，人认为那是「同一条痕迹」，新建会让 `mark_id` 指向的溯源断掉。
      任一不同（换了摘录 / 换了来源）就是独立痕迹，照常新建。
    ★ 复活时以这次输入的 note/anchor 为准 —— 否则用户重新写了一句备注，页面上
      看到的还是旧备注，会以为自己白写了。
    """
    t, sid, err = validate_source(source_type, source_id)
    if err:
        return False, {"err": err}
    quoted = (quoted_text or "").strip()
    if not quoted:
        return False, {"err": "摘录内容不能为空"}
    note_s, anchor_s = (note or "").strip(), _anchor_json(anchor)
    old = (Highlight.objects
           .filter(source_type=t, source_id=sid, quoted_text=quoted,
                   deleted_at__isnull=False)
           .order_by("-id").first())
    if old is not None:
        old.deleted_at = None
        old.note, old.anchor = note_s, anchor_s
        old.save(update_fields=["deleted_at", "note", "anchor"])
        return True, {"id": old.id, "restored": True}
    row = Highlight.objects.create(
        source_type=t, source_id=sid, quoted_text=quoted,
        note=note_s, anchor=anchor_s)
    return True, {"id": row.id}


def add_annotation(source_type, source_id, body, anchor=""):
    """存一条批注。返回 (ok, payload)。复活判据同 `add_highlight`（比归一化 body）。"""
    t, sid, err = validate_source(source_type, source_id)
    if err:
        return False, {"err": err}
    text = (body or "").strip()
    if not text:
        return False, {"err": "批注内容不能为空"}
    anchor_s = _anchor_json(anchor)
    old = (Annotation.objects
           .filter(source_type=t, source_id=sid, body=text,
                   deleted_at__isnull=False)
           .order_by("-id").first())
    if old is not None:
        old.deleted_at = None
        old.anchor = anchor_s
        old.save(update_fields=["deleted_at", "anchor"])
        return True, {"id": old.id, "restored": True}
    row = Annotation.objects.create(
        source_type=t, source_id=sid, body=text, anchor=anchor_s)
    return True, {"id": row.id}


def list_marks(source_type, source_id):
    """某个内容块的划线 + 批注（按时间升序，划线在前批注在后两组分开）。

    返回 {'highlights': [...], 'annotations': [...], 'err': ''}。
    不校验 source（只读，跨类型的枚举预留也不拦），但把每条规范成字典。
    """
    out = {"highlights": [], "annotations": [], "err": ""}
    t = (source_type or "").strip()
    sid = (source_id or "").strip()
    if not t or not sid:
        out["err"] = "缺少 source_type / source_id"
        return out
    # ★ 软删 = 不想要。这一处排除同时作用于「页面展示」和「提炼素材」
    #   （ingest.signal_texts / _material 都走本函数），不做两套口径：不想要的
    #   东西既不该看见，也不该再喂给模型溯源。
    hs = Highlight.objects.live().filter(source_type=t, source_id=sid).order_by("created_at")
    an = Annotation.objects.live().filter(source_type=t, source_id=sid).order_by("created_at")
    out["highlights"] = [{
        "id": h.id, "quoted_text": h.quoted_text, "note": h.note,
        "anchor": _anchor_obj(h.anchor), "created_at": h.created_at,
    } for h in hs]
    out["annotations"] = [{
        "id": a.id, "body": a.body, "anchor": _anchor_obj(a.anchor),
        "created_at": a.created_at,
    } for a in an]
    return out


def mark_counts(source_type, source_id):
    """某个内容块上的划线/批注**条数**（软删不计）。

    与 `list_marks` 同一口径（`live()`），只是不取正文 —— 重跑前的告警只要数。
    """
    t = (source_type or "").strip()
    sid = (source_id or "").strip()
    if not t or not sid:
        return {"highlights": 0, "annotations": 0, "total": 0}
    h = Highlight.objects.live().filter(source_type=t, source_id=sid).count()
    a = Annotation.objects.live().filter(source_type=t, source_id=sid).count()
    return {"highlights": h, "annotations": a, "total": h + a}


def rerun_warning(source_type, source_id, *, what="这份内容"):
    """重跑（覆盖原文）前的二次确认材料：条数 + 一句人话。

    ★ 为什么必须由存储层给这句话：日报、周报、（将来）对话都可能被重跑覆盖，
      措辞散在各视图里必然漏一处（同口径两处必漏一处）。判据只有一条：
      **原文被换掉，锚在原句上的标记就落不了位**。
    ★ 落不了位 ≠ 被删除：记录还在（`list_marks` 照旧返回），只是页面上掉进
      底部「未落位」兜底列表。这句必须写进 msg，否则用户会以为数据没了。
    ★ 返回 `total == 0` 时 msg 为空串 —— 调用方据此决定不打扰。
    """
    c = mark_counts(source_type, source_id)
    if not c["total"]:
        return dict(c, msg="")
    parts = []
    if c["highlights"]:
        parts.append(f"{c['highlights']} 条划线")
    if c["annotations"]:
        parts.append(f"{c['annotations']} 条批注")
    msg = (f"{what}上有 " + "、".join(parts) + "。\n\n"
           "重跑会用新正文替换原文，这些标记锚的是原句，替换后大概率无法再落位"
           "（记录不会丢，但会掉进页面底部的「未落位」列表）。\n\n仍然重跑？")
    return dict(c, msg=msg)


def _anchor_obj(anchor: str):
    """把落库的 anchor 字符串解析回 dict（渲染层 / 模板用）。空串 → None。"""
    if not anchor:
        return None
    try:
        obj = json.loads(anchor)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def annotate_html(html_str: str, data: dict, undisplayed=None) -> str:
    """把一份 `list_marks()` 的结果回填到正文 HTML（inline 高亮）。

    data 就是 list_marks 返回的 {'highlights': [...], 'annotations': [...]}，
    这里把它整理成 markrender 需要的 [{id, kind, anchor}] 再交出去。
    ★ 视图层只要 `mdrender.render(…)` 之后包一句 `marks.annotate_html(html, marks)`
      就能让正文里的划线/批注原位置高亮 —— 匹配逻辑全在 core.markrender，
      这里只做「列表 → 锚点流」的薄转换，不复制匹配判据。

    undisplayed: 可选 list，收集**没能在正文里落位**的标记 id（anchor 缺失 / 锚点失配 /
      正文为空）。
      ★ 这是「底部列表能不能删」的判据：正文落位成功的标记不需要任何列表（hover 就能
        改/删）；落位失败的必须有个出口，否则数据还在库里、页面上却看不见也删不掉。
        所以底部列表改成「只显示 undisplayed」而不是整块删掉。
    """
    items = []
    # ★ text 是「hover 时显示的文字」：划线取备注（note），批注取正文（body）。
    #   没有文字就留空 —— 前端据此决定 hover 只出按钮组、还是出文字卡片。
    for h in (data or {}).get("highlights", []):
        items.append({"id": h["id"], "kind": "highlight", "anchor": h.get("anchor"),
                      "text": (h.get("note") or "").strip()})
    for a in (data or {}).get("annotations", []):
        items.append({"id": a["id"], "kind": "annotation", "anchor": a.get("anchor"),
                      "text": (a.get("body") or "").strip()})
    if not items:
        return html_str
    if not html_str:           # 正文取不到 → 全部算没落位，别让它们凭空消失
        if undisplayed is not None:
            undisplayed.extend(i["id"] for i in items)
        return html_str
    from core import markrender
    resolved = []
    out = markrender.apply_marks(html_str, items, undisplayed, resolved)
    _backfill_anchors(resolved)
    return out


def _backfill_anchors(resolved):
    """把老锚点（只有 text/prefix/suffix）就地补成五字段：一次性写回 offset/nth。

    为什么要在**渲染**时补：老数据的指纹是 16 字、且没有位置先验，正文里一旦出现
    重复，它就退回"第一处"——正是用户报的那个缺陷。手工补要另写迁移脚本，而这里
    已经有"这条标记落在哪"的确切答案（markrender 回报的 resolved），顺手落库最省事，
    且只在缺 offset 时写一次，之后每次渲染都是零写入。

    失败不抛：这是渲染路径上的增值动作，写不进去也不能让详情页打不开。
    """
    if not resolved:
        return
    for it in resolved:
        model = Annotation if it.get("kind") == "annotation" else Highlight
        try:
            row = model.objects.get(id=it["id"])
            obj = _anchor_obj(row.anchor)
            if not obj or "offset" in obj:
                continue
            obj["offset"] = int(it["offset"])
            obj["nth"] = int(it["nth"])
            row.anchor = json.dumps(obj, ensure_ascii=False)
            row.save(update_fields=["anchor"])
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "补全锚点失败 id=%s kind=%s: %s", it.get("id"), it.get("kind"), exc)


def delete_highlight(mark_id) -> bool:
    """软删一条划线 —— 保留行，`deleted_at` 打时间戳。返回是否真的删掉一条。

    ★ 不物理删：候选的 `mark_id` 是软引用，物理删会把「这句话出自哪条痕迹」
      的溯源一起抹掉，而人删的时候只是想让它别再出现。
    ★ 只认未删的（`deleted_at__isnull=True`）：已经软删的再删一次返回 False，
      别把「重复点删除」伪装成一次成功。
    """
    try:
        n = (Highlight.objects.live()
             .filter(id=int(mark_id))
             .update(deleted_at=timezone.now()))
    except (ValueError, TypeError):
        return False
    return bool(n)


def delete_annotation(mark_id) -> bool:
    """软删一条批注，判据同 `delete_highlight`。"""
    try:
        n = (Annotation.objects.live()
             .filter(id=int(mark_id))
             .update(deleted_at=timezone.now()))
    except (ValueError, TypeError):
        return False
    return bool(n)


def mark_ref_count(kind: str, mark_id) -> int:
    """这条痕迹被多少条候选引用（删之前给人一个提示）。

    ★ `mark_id` 只是个整数软引用，而 `Highlight` 与 `Annotation` 是两张表、
      id 各自独立 —— 同一个 id 完全可能一条是划线、另一条是批注。只按
      `mark_id` 数会把另一张表同 id 的候选误算进来（人会以为删错了东西）。
      所以先按 kind 取到痕迹本身，再用「候选的 quote 对得上这条痕迹的文本」
      做二次确认。
    ★ 比对用「相等或互为子串」：提取时 quote 可能是摘录里的一句，也可能带
      了我写的备注。这是**提示用的保守计数**，宁可多提醒一句，也不要静默
      断掉溯源；不参与任何拦截或落库判断。
    """
    model = Highlight if kind == "highlight" else Annotation
    try:
        row = model.objects.filter(id=int(mark_id)).first()
    except (ValueError, TypeError):
        return 0
    if row is None:
        return 0
    if kind == "highlight":
        texts = [row.quoted_text, row.note]
    else:
        texts = [row.body]
    texts = [(x or "").strip() for x in texts]
    texts = [x for x in texts if x]
    if not texts:
        return 0
    n = 0
    for c in IngestCandidate.objects.filter(mark_id=row.id).only("quote"):
        q = (c.quote or "").strip()
        if any(t == q or t in q or q in t for t in texts):
            n += 1
    return n


def edit_mark(kind: str, mark_id, body) -> tuple:
    """编辑一条划线（改备注）或批注（改正文）。返回 (ok, err)。

    ★ 划线编辑的是 `note`（备注），不碰 quoted_text（摘录是「选中了什么」，
      改它等于换了锚点，语义已不是「编辑」而是「重新划」）；批注编辑的是 `body`。
    ★ 空内容拒：批注 body 空、划线 note 空都无意义（划线 note 空 = 清备注，
      允许，故划线只校验 kind 合法，不拒空 note）。
    """
    text = (body or "").strip()
    # ★ 软删的记录不可编辑：页面上看不见的东西，不该有个端点能悄悄改它。
    if kind == "highlight":
        try:
            n = Highlight.objects.live().filter(id=int(mark_id)).update(note=text)
        except (ValueError, TypeError):
            return False, "划线 id 非法"
        return (True, "") if n else (False, "划线不存在")
    if kind == "annotation":
        if not text:
            return False, "批注内容不能为空"
        try:
            n = Annotation.objects.live().filter(id=int(mark_id)).update(body=text)
        except (ValueError, TypeError):
            return False, "批注 id 非法"
        return (True, "") if n else (False, "批注不存在")
    return False, "未知标记类型"
