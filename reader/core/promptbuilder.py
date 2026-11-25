"""分级 prompt 构建（M4）：AI 润色生成 + 规则拼装兜底 + 版本落库。

★ 不可人工编辑 —— 所以新版本直接 activate、旧版 archived，不存在"被覆盖"问题。
★ AI 抽风时最坏情况是退回规则版，不会拿一个坏 prompt 去烧钱跑几百条。
"""
import logging
import re

from django.conf import settings
from django.db import transaction

from core import costreport as cr
from core import promptspec
from core.models import TriagePrompt

log = logging.getLogger(__name__)

# 结构校验：AI 产出必须全部通过，否则重试 1 次 → 仍不过就回退规则版
FORBIDDEN = ("忽略投票", "不要投票", "跳过投票", "直接给 L0", "一律 L0", "忽略上述", "忽略以上")
MIN_LEN, MAX_LEN = 500, 6000

# 画像越界检测：窗口长度（去空白后）。
# ★ 取 24 是为了只抓「成段搬运」——模型把画像的一句判据抄进判据区时，
#   共享片段远超 24 字；而"能改变判断""可复用"这类 4–6 字短语不会误伤。
_LEAK_WIN, _LEAK_STRIDE = 24, 8

# 「整行就是判据标题」：===== 各级判据 ===== / 四、各级判据 / 判据 —— 
# ★ 不能放宽成"行内含关键词"，否则模板自己的作用域声明会被当成标题（踩过）。
_HEAD_CRIT = re.compile(
    r"^\s*[=#\-\*\s]*(?:[一二三四五六七八九十]+\s*[、.．)]\s*)?(?:各级|分级)?判据\s*[=#\-\*\s]*$")

# ★ 只认**真正的占位符残留**（{{...}} / {profile} 之类）。
#   早先的检查是"出现任何 { 或 } 就拒绝"，会把模型写的 JSON 示例误判成残留。
_PLACEHOLDER = re.compile(
    r"\{\{|\}\}|\{(?:profile|levels|note|feed_title|default_level|default_zh|arbiter|hard|prev|change|min_len|max_len)\}")


def _criteria_region(text: str) -> str:
    """截出「各级判据」段及其之后的正文——只在这一区域检测画像越界。

    画像摘要在 prompt 前半段被**允许**原样转述，所以不能在全文上扫。
    ★ 踩过的坑：正则不能直接 `find("各级判据")` —— 模板自己的作用域声明里就写着
      「级别的具体判据一律在下方『各级判据』段」，`find` 会命中那句话，把整篇都当成判据区。
      所以必须**按行**匹配「整行就是判据标题」或「第一个级别块」。
    """
    lines = (text or "").splitlines()
    for i, ln in enumerate(lines):
        if _HEAD_CRIT.match(ln):
            return "\n".join(lines[i:])
    for i, ln in enumerate(lines):
        if re.match(r"^\s*\**\s*L0\b", ln):
            return "\n".join(lines[i:])
    i = (text or "").find("判据")
    return (text or "")[i:] if i >= 0 else (text or "")


def profile_leak(text: str) -> list:
    """模型是否把「读者画像」扩写成了级别判据。

    ★ 为什么需要它：画像被拼进**每一个**源的 prompt。2026-10-15 的事故就是
    画像里的投资 L2/L3 判据被模型搬进了兼职社区的判定 prompt。
    画像本身已经清干净了，这条护栏是防它再被写回去的回归测试。
    """
    prof = re.sub(r"\s+", "", promptspec.profile_text())
    reg = re.sub(r"\s+", "", _criteria_region(text or ""))
    if not prof or not reg:
        return []
    hits = []
    for i in range(0, max(0, len(prof) - _LEAK_WIN + 1), _LEAK_STRIDE):
        seg = prof[i:i + _LEAK_WIN]
        if seg in reg:
            hits.append(seg)
    return hits


def _levels_defined(spec: dict) -> str:
    """本配置「有判据」的级别清单（含默认兜底级），供 META_USER 明确作用域。"""
    have = [f"L{i}" for i in range(4) if spec.get(f"l{i}")]
    dl = int(spec.get("default_level", 2))
    head = "、".join(have) if have else "（没有任何级别配置了判据，全靠兜底）"
    if f"L{dl}" not in have:
        head += f"；L{dl}（默认兜底，未命中任何判据时归此级）"
    return head


def _unused_levels(spec: dict) -> str:
    """本配置「不使用」的级别（没判据、也不是默认兜底级）。让 META_USER 能明说，而不是靠模型自觉。"""
    dl = int(spec.get("default_level", 2))
    un = [f"L{i}" for i in range(4) if not spec.get(f"l{i}") and i != dl]
    return "、".join(un) if un else "无（四级判据齐全）"


# 「L3（特别关注，本源口径细化）」这类写法 = 给该级别写了判据
_LEVEL_PAREN = re.compile(r"\*{0,2}L([0-3])\*{0,2}\s*[（(]([^）)]{0,28})[）)]")


def invented_levels(text: str, spec: dict) -> list:
    """模型为「没配判据、也不是默认级别」的级别自己编了判据。

    信号（二者其一）：该级别在判据区出现 ≥2 次；或紧跟一段 ≥6 字的括号说明。
    ★ 只查这两个信号是为了不误伤：默认兜底级、以及只在输出格式里被提一次的级别都不算。
    """
    reg = _criteria_region(text or "")
    dl = int(spec.get("default_level", 2))
    out = []
    for n in range(4):
        if spec.get(f"l{n}") or n == dl:
            continue
        paren = [m for m in _LEVEL_PAREN.finditer(reg)
                 if int(m.group(1)) == n and len(m.group(2).strip()) >= 6]
        if paren or len(re.findall(rf"L{n}(?!\d)", reg)) >= 2:
            out.append(n)
    return out


def validate(text: str, spec: dict):
    """返回 (ok, [未通过的原因])。"""
    reasons = []
    t = text or ""
    if not any(f"L{i}" in t for i in range(4)):
        reasons.append("没有级别标记 L0–L3")
    leak = profile_leak(t)
    if leak:
        reasons.append(f"把读者画像扩写成了判据（越界）：「{leak[0]}」")
    bogus = invented_levels(t, spec)
    if bogus:
        reasons.append("为未配置判据的级别自己编了判据："
                       + "、".join(f"L{n}" for n in bogus))
    if "JSON" not in t.upper() or "level" not in t:
        reasons.append("缺少 JSON / level 输出要求")
    dl = f"L{spec.get('default_level', 2)}"
    if dl not in t and "默认级别" not in t:
        reasons.append(f"缺少默认级别 {dl} 的兜底表述")
    if not (MIN_LEN <= len(t) <= MAX_LEN):
        reasons.append(f"长度 {len(t)} 不在 {MIN_LEN}–{MAX_LEN}")
    if _PLACEHOLDER.search(t):
        reasons.append("存在模板占位符残留（{ }）")
    for bad in FORBIDDEN:
        if bad in t:
            reasons.append(f"试图覆写底座硬约束：{bad}")
    return (not reasons), reasons


META_SYSTEM = ("你是一位提示词工程师。你的任务是把用户给的「分级口味」改写成一份"
               "可以直接用于批量判定的中文提示词。**只输出提示词正文**，不要解释、"
               "不要 markdown 代码块、不要前后缀客套话。")

META_USER = """下面是某个信息源的分级要求，请把它们整理成一份**可直接复用的判定提示词**。

【读者画像】（全局口径，所有信息源共用。★ 它**只**说明"读者是谁、什么对他有价值"——
画像里没有级别判据是**故意的**。你可以在"读者画像摘要"一段里忠实转述它，
但**各级判据只能来自下面的【各级判据】段**，不得从画像里推导、扩写或补全任何级别的判据。）
{profile}
{note}
【本源】{feed_title}

【默认级别】L{default_level}（{default_zh}）—— 没命中任何判据时归到这里。
提示词里必须明确写出这条兜底规则。

【本配置涉及的级别】{levels_defined}
★★ **只有上面列出的级别才有判据**。其余级别（{unused}）在**本配置里根本不使用**：
不要为它们写任何判据、不要展开说明、也不要给任何条目判到这些级别 —— 输出的 level 只能取上面列出的值。
（实测教训：兼职社区的配置里没有 L3，模型却自己编了一段「L3 特别关注：与读者能力吻合…」，
把来源放进了不该有的级别。）

【各级判据】（★ 可能由 分类口径 + 本源口径 拼接而成，已标注来源）
{levels}

【仲裁规则】（必须写进提示词）
{arbiter}

【必须保留的硬约束】（这些由系统底座保证，你的提示词**不得与之冲突**，但可以换个说法重申）
{hard}

【上一版提示词】（若有，请在其基础上改进，不要推倒重来）
{prev}

【本次配置变化】
{change}

【输出要求】
- 一份完整的中文提示词，结构：读者画像摘要 → 本源说明 → 默认级别兜底 → 各级判据（分级列出，标注来源）→ 仲裁规则 → 输出格式（JSON 数组，含 id/level/one_line/why）。
- **只为【各级判据】里出现过的级别写判据**。没出现的级别只提它的通用语义，不要替它编判据。
- ★★ **严禁引入本配置之外的判据**：不得出现其它分类（如投资、教育）或其它信息源的专属口径。
  只有【各级判据】里写了的内容才算判据；"读者画像"里的一切都不算判据。
- 每条判据要**可操作**：写清"什么样的条目算这一级"，避免空话。
- 不要出现 {{ }} 这类模板占位符；条目列表由系统在调用时附加，你只需说明格式。
- 长度控制在 {min_len}–{max_len} 字符。
"""


def _levels_block(spec: dict) -> str:
    out = []
    for i in range(4):
        seg = spec.get(f"l{i}")
        if not seg:
            continue
        src = " + ".join({"category": "分类", "feed": "本源"}[p["from"]] for p in seg["parts"])
        out.append(f"- L{i}（{src}）：{seg['text']}")
    return "\n".join(out) or "（未配置任何判据）"


def _generate(spec: dict, prev_text: str, change: str):
    """调 AI 生成一版。返回 cr.ChatResult（预算闸与记账都在 chat_recorded 里）。

    ★ 花的是分级池的钱（kind=prompt_build 归 POOL_TRIAGE），闸门同池：
      分级预算用尽时 prompt 构建也停 —— 反正没有额度跑判定了，
      直接走规则版兜底。
    """
    dl = int(spec.get("default_level", 2))
    user = META_USER.format(
        profile=promptspec.profile_text()[:2000],
        note=promptspec.note_block(spec.get("note", "")),
        feed_title=spec.get("feed_title") or f"feed:{spec.get('feed_id')}",
        default_level=dl,
        default_zh=promptspec.LEVEL_ZH.get(dl, ""),
        levels_defined=_levels_defined(spec),
        unused=_unused_levels(spec),
        levels=_levels_block(spec),
        arbiter=promptspec.ARBITER,
        hard=promptspec.BASE_RULES,
        prev=(prev_text or "（无，这是首版）")[:4000],
        change=change or "（首版）",
        min_len=MIN_LEN, max_len=MAX_LEN,
    )
    return cr.chat_recorded(
        "prompt_build",
        [{"role": "system", "content": META_SYSTEM},
         {"role": "user", "content": user}],
        pool=cr.POOL_TRIAGE,
        temperature=0.3, max_tokens=4096, retries=2, timeout=120,
        feed_id=spec.get("feed_id"), category_id=spec.get("category_id"))


@transaction.atomic
def build(feed_id: int, category_id: int = None, feed_title: str = "", force: bool = False):
    """为该 feed 构建/刷新 prompt。返回 TriagePrompt 或 None（四段全空，不需要 prompt）。"""
    spec = promptspec.build_spec(feed_id, category_id=category_id, feed_title=feed_title)
    if not spec["has_any"]:
        # 四段全空 = 不调 LLM，一律 default_level —— 连 prompt 都不需要
        TriagePrompt.objects.filter(feed_id=feed_id, status="active").update(status="archived")
        return None, spec

    digest = promptspec.source_hash(spec)
    active = (TriagePrompt.objects.filter(feed_id=feed_id, status="active")
              .order_by("-version").first())
    if active and active.source_hash == digest and not force:
        return active, spec

    # ★ 画像换了 ⇒ 不能在「上一版」基础上改进：上一版正是按旧画像写的，
    #   带着它改会让旧画像的判据以"沿用"的方式活下来（2026-10-15 兼职社区泄漏的成因之一）。
    profile_changed = bool(active) and (
        (active.spec_json or {}).get("profile_hash") != spec.get("profile_hash"))
    prev_text = "" if profile_changed else (active.prompt_text if active else "")
    if profile_changed:
        log.info("feed %s：读者画像已变更，本次 prompt 从零重写（不继承上一版）", feed_id)
    note = promptspec.change_note(active.spec_json if active else None, spec)

    # ★ 每次生成调用都由 chat_recorded 单独落账（旧实现把两次校验重试记成一行、
    #   且把校验次数记成传输重试 —— 账本口径已随统一出口修正）。
    #   校验不合格重试的是「再调一次模型」，各记各的账；usage_row 取最后一次。
    text, err, last_usage_row = "", "", None
    if settings.LLM_PROVIDER and (settings.DEEPSEEK_API_KEY or settings.AGNES_API_KEY):
        for attempt in range(2):
            res = _generate(spec, prev_text if attempt == 0 else "", note)
            if res.usage_row is not None:
                last_usage_row = res.usage_row
            text, err = res.text, res.err
            if not err:
                ok, reasons = validate(text, spec)
                if ok:
                    break
                err = "结构校验未通过：" + "；".join(reasons)
            if attempt == 0:
                log.warning("prompt 生成第 1 次不合格（%s），重试", err)

    built_by = "rule"
    validated = False
    if text and not err:
        built_by, validated = "llm", True
    else:
        text = promptspec.render_rule(spec)
        if err:
            log.warning("feed %s prompt 回退规则版：%s", feed_id, err)

    version = (active.version + 1) if active else 1
    TriagePrompt.objects.filter(feed_id=feed_id, status="active").update(status="archived")
    row = TriagePrompt.objects.create(
        feed_id=feed_id, version=version, spec_json=spec, prompt_text=text,
        source_hash=digest, change_note=note, built_by=built_by,
        validated=validated, status="active",
        llm_usage_id=last_usage_row.id if last_usage_row else None,
    )
    return row, spec


def rebuild_category(category_id: int, feed_nodes):
    """分类配置变更 → 逐个比对 hash，只重建真变化的 feed（§6.2.5）。"""
    rebuilt = []
    for f in feed_nodes:
        spec = promptspec.build_spec(f.id, category_id=category_id, feed_title=f.title)
        if not spec["has_any"]:
            TriagePrompt.objects.filter(feed_id=f.id, status="active").update(status="archived")
            continue
        active = (TriagePrompt.objects.filter(feed_id=f.id, status="active")
                  .order_by("-version").first())
        if active and active.source_hash == promptspec.source_hash(spec):
            continue
        build(f.id, category_id=category_id, feed_title=f.title)
        rebuilt.append(f.id)
    return rebuilt
