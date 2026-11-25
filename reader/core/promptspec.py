"""分级配置的分层合并 + 指纹 + 规则拼装（M4 的兜底基线）。

★ 四级职责，**不要越层**（2026-10-15 的真实事故就是越层造成的）：
  1. 底座  BASE_SYSTEM / BASE_RULES —— 代码，全局唯一。锁死输出格式 / 四级通用语义 /
     投票 / anti-FOMO / fail-open。**不受任何配置影响**。
  2. 全局  读者画像（`Meta/interest-profile.md`）—— **只描述读者是谁、什么对他有价值**。
     它回答的是「谁在读」，**不回答「算哪级」**：
     · 给分级用：判定一条内容命中读者哪一条需求（投进每个源的 prompt）；
     · 给日报用：札记只抽「对我最值得关注的 3 件事」（每件带引用），
       全是**读者相关**的判断，没有画像只能排出泛泛的"重要度"
       （入口 `profile_notes_block()`）。
     ★★ 画像被拼进**每一个**源的 prompt：一旦在这里写「L2 = 投资」这类判据，
     它立刻变成所有源的判据（旧画像就是这么污染了兼职社区的）。用 `profile_lint()` 兜底。
  3. 分类  CategoryTriageConfig —— 投资 / AI / 教育各自的 L0–L3 判据放这里。
  4. 本源  FeedTriageConfig —— 这个源自己的体裁判据 + 文本结构备注（`note`，非判据）。
     没有第 5 层「全局默认分级」：大部分分类和 feed 根本不配判据，那正是「不调 LLM」的场景。

★ 底座 / 口径 分离：底座全局唯一、手工维护；口径段（2/3/4 层）才可配置，
  由 AI 润色生成、规则拼装兜底。
"""
import hashlib
import logging
import re

from django.conf import settings

from core.models import CategoryTriageConfig, FeedTriageConfig

log = logging.getLogger(__name__)

TEMPLATE_VERSION = "3"   # v3：L1 文案改「低价值/普通」；日报分层改用 S 前缀（改底座必须 bump）
LEVEL_ZH = {0: "L0 忽略", 1: "L1 低价值/普通", 2: "L2 关注", 3: "L3 重点"}

# ------------------------------------------------------------------ 底座
# ★ 手工维护，改这里要 bump TEMPLATE_VERSION（会触发所有 feed 的 prompt 重建）
# ★★ 这里**不放任何单一来源的文本结构规则**：底座会进每一个源的 prompt，写在这里等于全局生效。
#    单一来源特有的文本结构规则下沉到 FeedTriageConfig(feed_id=...).note；
#    具体来源与规则以实际配置为准，不写死在底座里。
BASE_SYSTEM = """你是一个信息筛选助手，为特定读者判定条目的价值。只输出 JSON，不要任何解释文字。"""

BASE_RULES = """★ 输出格式：严格返回 JSON **数组**，每个元素形如
{{"id": <整数>, "level": "<L0|L1|L2|L3>", "one_line": "<这条在讲什么，20字内>", "why": "<为什么给这个级别，12字内>"}}
不要 markdown 代码块，不要前后缀文字。

★ 四级通用语义（本源自己的判据在下面，是这些语义的具体化）：
- L0 忽略：对读者完全没有信息价值。拿不准一律不要给 L0。
- L1 低价值/普通：不属于核心关切，保留价值有限；或信息不足无法判断（正文只有
  标题 + 链接是来源方没给全文，**不是内容没价值**）。默认 L1 的源通常大部分是这类。
- L2 关注：与读者的核心关切相关，值得花时间读。
- L3 重点：值得立刻看，通常带有明确、可行动的信息。

★ 投票与 anti-FOMO（硬约束，不可被任何配置改写）：
- 你会被要求独立判定多次，每次结果都是一张独立的票。
- 有效票少于 2 张时不要直接判 L0（证据不足；最终由系统聚合）。
- 全票 L0 才判 L0；多数说 L0 但有分歧 → 降为 L1 保留。
- 平票取更高级别。宁可多留，不可误杀。
"""

ARBITER = """★ 每一级别可能包含两段判据：先给的是**分类口径**（宽），后给的是**本源口径**（更具体）。
两者冲突时**以本源口径为准**；不冲突时**都算命中条件**。"""

# ------------------------------------------------------------------ 规则拼装模板（兜底）
# ★ 提示词正文里**不留任何占位符**：条目由 TriageWorker 在调用时拼在末尾，
#   这样"无占位符残留"既是一条校验，也确实成立。
RULE_TEMPLATE = """===== 读者画像 =====
（本段**只**说明读者是谁、什么对他有价值；**级别的具体判据一律在下方「各级判据」段**，
本段不提供、也不得被推导出任何级别的判据。）
{profile}

===== 本源 =====
{feed_title}
{note}
★ 默认级别：{default_level}（{default_zh}）—— 没命中下面任何判据、或无法判断时，一律归到这里。

===== 各级判据 =====
{levels}
{arbiter}

===== 输出 =====
对下面给出的每一条，严格按 JSON 数组返回，元素形如
{{"id": <条目 id，整数>, "level": "<L0|L1|L2|L3>", "one_line": "<这条在讲什么，20字内>", "why": "<为什么给这个级别，12字内>"}}
只回 JSON，不要 markdown 代码块，不要前后缀文字。

★ 待判定的条目由系统在调用时附在本提示词之后，形如
--- id=123 | 2026-10-14 09:30 ---
标题：……
正文：……
请逐条给出判定，id 必须原样带回。
"""


# ------------------------------------------------------------------ 画像
def profile_text() -> str:
    p = settings.PROJECT_ROOT / "Meta" / "interest-profile.md"
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        log.warning("兴趣画像缺失：%s", p)
        return ""


def profile_hash() -> str:
    return hashlib.sha256(profile_text().encode()).hexdigest()[:16]


# ------------------------------------------------------------------ 本体
#: 「知识组织骨架」，与画像对称但**不参与分级**。
#: ★ 三层分工别混（2026-10-30 定稿）：
#:   画像 = 读者是谁、什么有价值（**判断读不读**）；
#:   本体 = 受控 domain 清单（**落到哪个域**）；
#:   schema.md = 页面格式规范（怎么写）。三者不互相替代。
#: ★★ 本体里**只有「领域」表会被程序解析**（`core.wiki.ontology_domains()`）；
#:   本函数的文本目前只喂 `ontology_hash()`，没有任何 prompt 注入点。
#:   2026-11-23 据此删掉了「概念」「关系」两段：它们写着「供编译归类参考」
#:   「AI 建链接时参考」，而程序从不读取 —— 分类实际由 domain + tags 两套承担，
#:   链接由正文 `[[slug]]` 自算。要新增「供模型参考」的段落，必须同时接进
#:   `ingest.py` 的 prompt 并加守卫断言，否则别写（写了不生效，只会让人以为改了有用）。
#: ★ 与画像不同，本体**不做软兜底**：假定用户已 cp example 或提前编辑。读不到
#:   只打一条 warning（= 配置丢了/路径错了的唯一运维提示），编译侧不设「无本体
#:   降级」分支 —— 少一条分支就少一处静默分叉。
#: ★ 不设长度上限：本体是受控清单，截断会砍掉域行（画像那边 2000 字上限是分级
#:   场景的历史约束，不适用于这里）。真嫌贵应该在编译层做「按域取用」，不是砍半截。
def ontology_text() -> str:
    p = settings.PROJECT_ROOT / "Meta" / "ontology.md"
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        log.warning("本体缺失：%s（cp Meta/ontology.example.md 一份再改）", p)
        return ""


def ontology_hash() -> str:
    """本体指纹（整份文本的 sha 前 16 位）。

    ★ 现状：`promptspec` 内部唯一消费者，产品链路**暂无调用方** —— 留着是给将来
      「页级标记基于哪版本体」用的，别把它当成已生效的能力（改了本体也不会自动
      重算任何页）。真要启用，得先有写入点 + 守卫，再写进这里。
    """
    return hashlib.sha256(ontology_text().encode()).hexdigest()[:16]


#: 画像注入日报的上限（与分级侧 `profile_text()[:2000]` 同一口径）
PROFILE_LIMIT = 2000


def profile_notes_block() -> str:
    """日报「札记」用的读者画像（**与分级共用同一份文件，但用在不同地方**）。

    ★ 为什么日报要读画像：札记的活是「对我而言值不值得看」—— 抽 3 件最值得
      关注的事并各带引用，判据全是读者侧的。没有画像，模型只会
      输出"某某发布新模型，很重要"这种谁都适用的正确废话。
    ★ 画像在这里**不是判据**：不决定哪条进日报（那是分级的事），只决定
      「进了日报之后，哪条更值得我先看」。
    ★ 越界（画像里写了级别判据）由 `profile_lint()` 在分级链路报警，
      日报不重复告警 —— 级别语义对札记无害。
    ★ 画像缺失时给一句兜底，而不是静默空串：模型至少知道按什么口径判断。
    """
    t = profile_text()[:PROFILE_LIMIT]
    if not t:
        return ("（未配置 Meta/interest-profile.md，按「广谱技术人 + 投资者」判断"
                "值得关注与否）")
    return t


# 画像越界的粗筛句式：只在「某级 + 冒号/等号 + 一段描述」或「级别定义表」时命中。
# ★ 刻意不做关键词黑名单（"投资"这个词本身没错，读者确实是投资者），只认句式。
_LINT_LEVEL_RULE = re.compile(r"\*{0,2}L([0-3])\*{0,2}\s*[:：=]\s*([^\n]{6,})")
_LINT_LEVEL_TABLE = re.compile(r"^\s*\|[^\n]*\*{0,2}L[0-3]\*{0,2}[^\n]*\|", re.M)


def profile_lint() -> list:
    """检查画像是否越界写了「级别判据」。

    ★ 画像被拼进**每一个**源的 prompt，写在这里的判据就等于全局判据
    （2026-10-15 事故：画像里的投资 L2/L3 判据泄漏进了兼职社区的分级 prompt）。
    返回警示文案列表；空 = 干净。
    """
    t = profile_text()
    warns = []
    for m in _LINT_LEVEL_RULE.finditer(t):
        warns.append(f"疑似在某级上直接写了判据：L{m.group(1)} → {m.group(2).strip()[:36]}")
    if _LINT_LEVEL_TABLE.search(t):
        warns.append("疑似用表格定义 L0–L3 的含义（级别语义属底座 BASE_RULES，不属画像）")
    return warns


def note_block(note: str) -> str:
    """本源备注的渲染：**不是判据**，只是这个源的文本结构说明。空串 = 不渲染该段。"""
    n = (note or "").strip()
    if not n:
        return ""
    return f"\n★ 本源备注（这个源的文本结构 / 格式说明，**不是判据**，不要据此改写判据）\n{n}\n"


# ------------------------------------------------------------------ 合并
def dedup_lines(text: str) -> str:
    """拼接后按行去重（保留顺序），避免分类与 feed 写了同一句时重复计数。"""
    seen, out = set(), []
    for line in (text or "").splitlines():
        key = re.sub(r"\s+", "", line).strip("。-—*· ")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(line.rstrip())
    return "\n".join(out)


def merge_level(cat_text: str, feed_text: str):
    """★ 拼接而非覆盖：覆盖会丢掉分类的判据，而分类那条往往是主要判据。"""
    parts = []
    for source, text in (("category", cat_text), ("feed", feed_text)):
        text = (text or "").strip()
        if text:
            parts.append({"from": source, "text": text})
    if not parts:
        return None
    return {"text": dedup_lines("\n".join(p["text"] for p in parts)),
            "parts": parts}


def build_spec(feed_id: int, category_id: int = None, feed_title: str = "") -> dict:
    """分类 → feed 两级合并。返回 spec（含每级来源，可追溯）。"""
    cat = CategoryTriageConfig.objects.filter(category_id=category_id).first()
    feed = FeedTriageConfig.objects.filter(feed_id=feed_id).first()

    for w in profile_lint():
        log.warning("画像越界（interest-profile.md）：%s", w)

    spec = {
        "feed_id": feed_id,
        "category_id": category_id,
        "feed_title": feed_title,
        "template_version": TEMPLATE_VERSION,
        "profile_hash": profile_hash(),
        # ★ 本源备注不是判据，单独一段注入；不参与 has_any（只有备注、没判据时依然不调 LLM）
        "note": ((feed.note or "").strip() if feed else ""),
    }
    has_any = False
    for i in range(4):
        merged = merge_level(
            getattr(cat, f"l{i}_text", "") if cat else "",
            getattr(feed, f"l{i}_text", "") if feed else "",
        )
        spec[f"l{i}"] = merged
        has_any = has_any or bool(merged)

    default_level = 2
    if feed and feed.default_level is not None:
        default_level = feed.default_level
    # ★ 分类不参与默认级别（分类没有 default_level），feed 不配即全局 L2
    spec["default_level"] = int(default_level)
    spec["has_any"] = has_any
    return spec


def criteria_index() -> tuple:
    """(有判据的分类 id 集, 有判据的源 id 集) —— 与 build_spec.has_any 同一口径。

    ★ 树上那个 L 标记（「已配置分级判据」）和「是否真的调 LLM」必须由同一份
      实现决定。此前视图层自己摸 `l{i}_text` 字段再判非空，与 build_spec 的
      逐级合并各写一套 —— 任何一边演进（比如 note 参与判据）就会静默分叉。
    ★ 判据 = L0–L3 任一段非空；只有 note（备注）或只设 default_level 不算 ——
      那种情况依然走「不调 LLM 的零成本默认路径」，不该在树上标 L。
    ★ 两次查询拿全量集合，供树渲染一次 annotate（build_spec 是逐 feed 两次，
      只适合单点判定）。
    """
    def _has(row) -> bool:
        return any((getattr(row, f"l{i}_text", "") or "").strip() for i in range(4))

    cats = {r.category_id for r in CategoryTriageConfig.objects.all() if _has(r)}
    feeds = {r.feed_id for r in FeedTriageConfig.objects.all() if _has(r)}
    return cats, feeds


def source_hash(spec: dict) -> str:
    """配置指纹：变了才重建 prompt。"""
    payload = [spec.get("template_version", ""), spec.get("profile_hash", ""),
               str(spec.get("default_level")), spec.get("note", "")]
    for i in range(4):
        seg = spec.get(f"l{i}")
        payload.append(seg["text"] if seg else "")
    return hashlib.sha256("\n".join(payload).encode()).hexdigest()


def change_note(old: dict, new: dict) -> str:
    """结构化 diff：哪一级从什么变成什么（prompt 不可人工编辑，靠它做审计）。"""
    if not old:
        return "首版"
    notes = []
    if old.get("default_level") != new.get("default_level"):
        notes.append(f"default: L{old.get('default_level')} → L{new.get('default_level')}")
    if (old.get("note") or "") != (new.get("note") or ""):
        notes.append(f"本源备注: «{(old.get('note') or '')[:20]}» → «{(new.get('note') or '')[:20]}»")
    for i in range(4):
        a = (old.get(f"l{i}") or {}).get("text", "")
        b = (new.get(f"l{i}") or {}).get("text", "")
        if a == b:
            continue
        if not a:
            notes.append(f"L{i}: 新增「{b[:40]}」")
        elif not b:
            notes.append(f"L{i}: 清空（原「{a[:40]}」）")
        else:
            notes.append(f"L{i}: 「{a[:30]}」→「{b[:30]}」")
    return "；".join(notes) or "无实质变化"


# ------------------------------------------------------------------ 规则拼装（兜底）
def render_rule(spec: dict) -> str:
    """0 token 的朴素版本。AI 产出不合格时自动回退到它。"""
    blocks = []
    for i in range(4):
        seg = spec.get(f"l{i}")
        if not seg:
            continue
        src = " + ".join({"category": "分类", "feed": "本源"}[p["from"]] for p in seg["parts"])
        blocks.append(f"【L{i}（{src}）】\n{seg['text']}")
    if not blocks:
        blocks.append("（本源未配置任何判据）")
    dl = int(spec.get("default_level", 2))
    return RULE_TEMPLATE.format(
        profile=profile_text(),
        feed_title=spec.get("feed_title") or f"feed:{spec.get('feed_id')}",
        note=note_block(spec.get("note", "")),
        default_level=f"L{dl}",
        default_zh=LEVEL_ZH.get(dl, ""),
        levels="\n\n".join(blocks),
        arbiter=ARBITER,
    )
