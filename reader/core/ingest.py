"""入库分诊 + 提取候选（阶段 2.2）。

命题：**对话之后才沉淀，且只有「我敲的字」才算我的判断。**

这条链的位置：划线/批注/对话都是「痕迹」，本模块把痕迹里**值得沉淀的内容**挑出来
变成候选（`IngestCandidate`），人审过了 2.3 才编译成原子概念页。

★★ 分诊不是「打分排序」，是**排除**。判据只有一句话：
   《正文》= 作者的；《我划出的原文》= 我在意（不是我的结论，但可以沉淀）；
   《我的原话》= 我敲的（唯一算我的判断的一层）。
   候选可以来自后两层：「我的原话」产出原创判断，「我划出的原文」产出在意的事实/观点。

★★ 「我敲的字」必须排除**预设话术**：快捷问题按钮和「点开即总结」也是 role=user
   的 user 消息，但那是**我点出来的字，不是我想的字**。不排除的话，「总结这篇文章」
   会被当成我的判断，整条沉淀链从第一个字就错了。文案的单一来源在
   `core.conversation.PRESET_TEXTS`，模板从模板标签取（不抄第二份）。

关键机制（按重要性）：

1. **零字不调模型**：`my_words()` 返回的字数低于 `WIKI_INGEST_MIN_CHARS` 直接拒。
   没有我的话就没有可沉淀的判断；让模型从「总结这篇文章」里提炼我的观点，
   得到的必然是它替我编的观点。这是确定性的省钱闸，不是风格参数。
2. **引用守卫用程序，不用 prompt**：每条候选的 `quote` 必须**逐字**出现在我的原话里
   （归一化后子串匹配），对不上就**丢**并计数。光在 prompt 里写「必须逐字引用」
   挡不住模型改写 —— 而改写过的「我的原话」正是最危险的东西：它看起来像我的话，
   其实不是。丢弃不静默：返回值里有 `dropped`，UI 会显示「N 条因引用不上被丢」。
3. **空结果合法**：一篇没有可沉淀判断的材料，正确输出就是 0 条。硬凑候选比不提取更糟。
4. **一次调用**：分诊与提取合并（设计 §2.6）——分诊判据离不开材料，拆两次多花一遍正文钱。

记账：`kind=wiki_extract`、`pool=POOL_WIKI`（走 `citeguard._call` 自动继承预算闸 +
记账 + 推理吃光预算翻倍重试）。

2.3 编译（`compile_candidates`，2026-11-02）在本模块尾部：未丢弃候选 → S1 分组 →
S2 逐页生成 → 守卫 G1–G5 → `.drafts/<slug>.md`。★ 编译**不生成「我的」连贯正文**，只归类
+挂出处；每一条正文都带 `cand_id` 指回候选，指不回去的不进来。
"""
try:
    import fcntl
except ImportError:  # Windows 无 fcntl，用跨平台兼容层
    from core import flock_compat as fcntl
import hashlib
import json
import logging
import os
import re
import time
import uuid
from functools import lru_cache

from django.conf import settings
from django.db.models import Count
from django.utils import timezone

from core import citeguard
from core import conversation as conv_mod
from core import costreport as cr
from core import jsontool
from core import marks
from core import reporttext
from core import sourceblock
from core import wiki as wstore
from core.models import (INGEST_KINDS, INGEST_LIVE_STATUS, INGEST_STATUS,
                         Annotation, ChatMessage, Conversation, Highlight,
                         IngestCandidate)
from core.reporttext import number_audit
from typing import Optional

log = logging.getLogger(__name__)

#: 指纹算法版本。★ 算法一变，旧台账里的 sig 就全部失效 —— 若不认版本，升级后
#: 每个来源都会显示「提炼后又写了」，诱导人把已经提炼过的内容再花一遍钱。
#: 版本不匹配时退回字数比较（粗但无害），下一次提炼写入新版本。
SIG_VERSION = 2

KIND_KEYS = tuple(k for k, _ in INGEST_KINDS)
def domains_for_prompt() -> tuple:
    """提炼 / 分组时给模型的**可选域**。唯一真值仍是 `Meta/ontology.md`。

    ★ 每次现取，不用模块级常量：改了 ontology.md 却要重启服务才生效，属于
      「静默不生效」—— 人以为新域已经放开，模型其实还在按旧清单填。
    ★ `life` 与别的域一样是合法域：wiki 里确有 life 页，落库校验也一直放行它。
      2026-11-23 起不再单独排除 —— 原先「不进本体、只用于分级」的注释与事实
      相反（life 在本体里、且真的进了 wiki），只让 prompt 不推荐、校验却放行，
      属于半吊子，行为不可预测。
    """
    return tuple(wstore.ontology_domains())


CONFIDENCE = ("high", "medium", "low")

#: 我的字的三种来源（给人看的标签；也是 `my_words()` 的 kind）
WORD_KINDS = {"question": "对话里我问的", "note": "划线备注", "annotation": "批注"}


# ------------------------------------------------------------------ 归一化 / 引用校验
_PUNCT = re.compile(r"[\s\u3000，。、；：？！“”‘’（）《》〈〉【】…—·,.;:?!\"'()\[\]<>/\\|~`@#$%^&*+=\-_]+")


def norm(text: str) -> str:
    """归一化：去空白与标点、全角→半角、小写。

    ★ 只用于**比对**，不用于展示。模型抄原话时常把「，」写成「,」或吃掉空格，
      严格逐字比对会把好候选全判死；但只允许标点/空白层面的差异 ——
      去掉标点后仍然必须逐字相同，改写过的（换词、缩句）照样过不了。
    """
    t = (text or "").translate(str.maketrans(
        "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
        "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"))
    return _PUNCT.sub("", t).lower()


#: 预设话术的归一化集合（懒算一次）。★ 不写死内容，从 conversation 取单一来源。
_PRESET_NORM = None


def _preset_norm() -> tuple:
    global _PRESET_NORM
    if _PRESET_NORM is None:
        _PRESET_NORM = tuple(norm(t) for t in conv_mod.PRESET_TEXTS if norm(t))
    return _PRESET_NORM


def is_preset(text: str) -> bool:
    """这条 user 消息是不是「我点出来的字」（快捷问题 / 自动总结）。"""
    n = norm(text)
    if not n:
        return True
    return any(n == p or (p in n and len(n) <= len(p) + 20) for p in _preset_norm())


# ------------------------------------------------------------------ 我的字
def my_words(source_type: str, source_id: str) -> dict:
    """收集「我敲的字」。返回 {items, chars, sections, err}。

    items = [{"kind": "question|note|annotation", "text": ..., "at": dt,
              "msg_id": ... | "mark_id": ...}]
    ★ 划线的 `quoted_text`（原文摘录）**不进这里**：那是别人的字。
      只有它的 `note`（我自己写的备注）才进。
    ★★ 每条 item 带**来源 id**（2026-11-02 加）：question→`msg_id`（那条消息）、
      note/annotation→`mark_id`（那条划线/批注）。这是深链精确定位的唯一事实源：
      提取阶段（verify_quotes 判命中的那一刻）知道「来自哪一条」，在这存下来，
      编译阶段直接读，不再靠文本反查猜（反查有两死穴：同话说过多次会配错、
      quote 是子串会误配到别的划线）。
    """
    out = {"items": [], "chars": 0, "err": ""}
    st = (source_type or "").strip()
    sid = (source_id or "").strip()
    if not st or not sid:
        out["err"] = "缺少 source_type / source_id"
        return out

    conv = Conversation.objects.filter(source_type=st, source_id=sid).first()
    if conv is not None:
        for m in conv.messages.filter(role="user").order_by("id"):
            if is_preset(m.body):
                continue          # ★ 我点的字，不是我想的字
            out["items"].append({"kind": "question", "text": m.body.strip(),
                                 "at": m.created_at, "msg_id": m.id})

    # ★ `.live()`：软删 = 不想要，不想要的东西不再喂给模型。判据只有
    #   `LiveMarkQuerySet.live()` 一份，这里与 marks.list_marks 共用。
    for h in Highlight.objects.live().filter(source_type=st, source_id=sid).order_by("created_at"):
        note = (h.note or "").strip()
        if note:
            out["items"].append({"kind": "note", "text": note, "at": h.created_at,
                                 "mark_id": h.id})

    for a in Annotation.objects.live().filter(source_type=st, source_id=sid).order_by("created_at"):
        body = (a.body or "").strip()
        if body:
            out["items"].append({"kind": "annotation", "text": body, "at": a.created_at,
                                 "mark_id": a.id})

    # ★ 2026-11-02 修复：对话里的划线备注和批注也要收集
    #   ★★ `at` 必须是真时间：下面要按它排序，None 与 datetime 比较会直接
    #      TypeError（2026-11-02 实测：对话项与文章项同时存在时整条提炼链炸）。
    if st == "entry":
        chat_hs = chat_highlights(st, sid)
        for h in chat_hs:
            note = (h.get("note") or "").strip()
            if note:
                out["items"].append({"kind": "note", "text": note, "at": h["at"],
                                     "mark_id": h["id"], "msg_id": h["msg_id"]})

        chat_ans = _chat_annotations(st, sid)
        for a in chat_ans:
            body = (a.get("body") or "").strip()
            if body:
                out["items"].append({"kind": "annotation", "text": body, "at": a["at"],
                                     "mark_id": a["id"], "msg_id": a["msg_id"]})

    out["items"].sort(key=lambda x: x["at"])
    out["chars"] = sum(len(x["text"]) for x in out["items"])
    return out


def chat_highlights(source_type: str, source_id: str) -> list:
    """收集这篇文章的对话里的划线。

    对话里的划线存在 source_type='chat' + source_id=<消息 id> 下，
    和文章正文的划线（source_type='entry'）分开。提取时需要把两者合并，
    否则对话里划的重要段落会被漏掉。

    返回 [{id, quoted_text, note, msg_id}, ...]
    """
    if source_type != "entry":
        return []  # 只有文章才有对话

    conv = Conversation.objects.filter(source_type=source_type, source_id=source_id).first()
    if conv is None:
        return []

    msg_ids = list(conv.messages.values_list("id", flat=True))
    if not msg_ids:
        return []

    hs = (Highlight.objects.live()
          .filter(source_type="chat", source_id__in=msg_ids).order_by("created_at"))
    return [{"id": h.id, "quoted_text": h.quoted_text, "note": h.note,
             "msg_id": h.source_id, "at": h.created_at} for h in hs]


def _chat_annotations(source_type: str, source_id: str) -> list:
    """收集这篇文章的对话里的批注。

    和 chat_highlights 同理，对话里的批注存在 source_type='chat' 下，
    需要单独收集并合并到「我的原话」里。

    返回 [{id, body, msg_id}, ...]
    """
    if source_type != "entry":
        return []

    conv = Conversation.objects.filter(source_type=source_type, source_id=source_id).first()
    if conv is None:
        return []

    msg_ids = list(conv.messages.values_list("id", flat=True))
    if not msg_ids:
        return []

    ans = (Annotation.objects.live()
           .filter(source_type="chat", source_id__in=msg_ids).order_by("created_at"))
    return [{"id": a.id, "body": a.body, "msg_id": a.source_id, "at": a.created_at}
            for a in ans]


# ------------------------------------------------------------------ 提炼台账（meta/ingest.json）
def _ledger_path():
    return wstore.root() / "meta" / "ingest.json"


def read_ledger() -> dict:
    try:
        return json.loads(_ledger_path().read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}


def _write_ledger(data: dict) -> None:
    p = _ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                 encoding="utf-8")


def _key(source_type: str, source_id: str) -> str:
    return f"{source_type}:{source_id}"


def words_sig(words: dict) -> str:
    """「信号」的内容指纹（归一化后拼接再 sha1）。

    ★ 为什么不用**字数**判断「提炼后又写了」：字数是粗糙代理，两个坑都踩得到 ——
      ① 我重写了一句、字数没变 ⇒ 状态仍是「已提炼」，新写的判断**永远不会被提炼**
      （静默漏，比多花钱糟得多）；② 空白/标点变化让字数差 1 ⇒ 状态变成「又写了」，
      点一次白花一次钱（2026-10-31 实测就出现了 24 vs 25 的误报）。
      指纹没有这两种模糊地带：**内容一样就是一样**。

    ★★ 必须**先排序再拼**：同一批字在两条路径上的顺序不同 ⇒ 哈希不同 ⇒ 永远判
      「又写了」，而且第一次实现就踩了（日报明明刚提炼过，页面却一直催着再花一次钱）。
      对「集合」算指纹，顺序本来就不该有意义。

    ★★ 入参的「是哪批字」比哈希算法更容易错：这里的 texts 必须来自 `signal_texts()`
      这一处（台账写入与收件箱判定共用），别再各自拼一份 —— 2026-11-03 就是因为
      两边拼的集合差一类字，12 条来源永久卡在「提炼后又写了」。
    """
    joined = "\n".join(sorted(norm(it["text"]) for it in words["items"]))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


def signal_texts(source_type: str, source_id: str, words: Optional[dict] = None) -> list:
    """一个来源的**全部「信号」原文** = 我的原话 + 我划出的原文。**只有这一份定义。**

    ★★ 2026-11-03 实测（entry 6670）：台账按 `my_words()` 算指纹，收件箱却另外
      把「没写备注的划线原文」也算进信号 —— 同一件事写了两遍，两遍还不一样。
      结果：提炼完再看，状态仍是「提炼后又写了」，人只当没生效、再点一次再花一次钱。
      库里的 12 条「又写了」全是这样来的（含 4 条 0 字纯划线的）。
      **判据要么不写，要么只写一份**；台账与收件箱都从这里取。

    规则（与收件箱那一列「N 字 · M 条」逐字对应）：
      - 我的原话：对话提问 / 划线备注 / 批注（含对话里的），见 `my_words()`；
      - 我划出的原文：**只有没写备注的划线**才单算一条 —— 有备注时那条备注已经
        代表我的态度，同一段原文不再重复计。
    """
    w = words if words is not None else my_words(source_type, source_id)
    texts = [it["text"] for it in w["items"]]
    for h in (marks.list_marks(source_type, source_id)["highlights"]
              + chat_highlights(source_type, source_id)):
        if (h.get("note") or "").strip():
            continue
        qt = (h.get("quoted_text") or "").strip()
        if qt:
            texts.append(qt)
    return texts


def signal_sig(source_type: str, source_id: str, words: Optional[dict] = None) -> tuple:
    """(`指纹`, `字数`, `条数`) —— 台账写入与收件箱判定共用的**同一对口径**。"""
    texts = signal_texts(source_type, source_id, words)
    sig = words_sig({"items": [{"text": t} for t in texts]})
    return sig, sum(len(t) for t in texts), len(texts)


def _resolve_source(source_type: str, source_id: str) -> tuple:
    """把 source 归到它的「所有者」。

    ★ 对话里的划线/批注存在 source_type='chat' + source_id=<消息 id> 下，
      但 inventory() 应该把它们归到文章（conversation 的 source）下显示，
      否则会出现一堆「chat:123」这种不可读的来源。
    """
    if source_type == "chat":
        # 找到这条消息所属的对话，用对话的 source
        msg = ChatMessage.objects.filter(id=source_id).select_related("conversation").first()
        if msg and msg.conversation:
            return msg.conversation.source_type, msg.conversation.source_id
    return source_type, source_id


# ------------------------------------------------------------------ 收件箱（零 LLM）
def inventory(limit=None) -> list:
    """哪些来源「有我的字」、提炼到什么程度。**零 LLM，纯查库 + 读台账。**

    ★ 为什么不让人自己去文章里点「提炼」：命题是「不靠手动记得点按钮」。
      这里把「有我的字但还没提炼 / 提炼后又加了字」的来源自己列出来，
      人只需要判断「要不要花这一分钱」。

    返回 [{source_type, source_id, title, words_chars, n_items, last_at,
           state: new|changed|done, n_candidates, n_pending}]
    state（判定用**内容指纹**，不用字数 —— 理由见 words_sig）：
      done    = 提炼过，且信号一字未变（信号 = 我的原话 + 我划出的原文，见 signal_texts）
      changed = 提炼过，之后我又敲/改了字，或又划了新的一段
      new     = 从没提炼过

    ★★ `limit=None` = **不截断**（2026-11-09 清零后暴露的问题）：`limit` 的职责
      只有「表格画多少行」，**不许拿它当计数**。原默认 30 被两处误用 ——
      ① 首页「N 处新痕迹」是 `sum(... for r in inventory())`，来源一超过 30
         （清零重来后真实库 33 个），面板就**少报**、还说不出被截了；
      ② `extract_then_compile()` 的 `todo` 同样只拿前 30 个，剩下的**静默不提炼**。
      两处都是「看着有活、其实干不完、而且没人知道」—— 最贵的那类失败。
      ⇒ 计数 / 决定「这一轮该处理谁」的调用方一律显式 `limit=None`；
        只有画表格的调用方给行数。
    """
    led = read_ledger()
    buckets = {}

    # ★ 只负责「哪些来源有痕迹 + 最后活动时间」；「信号原文」不在这里拼 ——
    #   统一由 signal_texts() 给（这份扫描一旦自己攒文本，就会和台账算出两套指纹，
    #   2026-11-03 entry 6670 就是这么永久卡在「提炼后又写了」的）。
    def bump(st, sid, at):
        b = buckets.setdefault(_key(st, sid), {"source_type": st, "source_id": sid,
                                               "last_at": at})
        if at and (b["last_at"] is None or at > b["last_at"]):
            b["last_at"] = at

    # ★★ 2026-11-16 用户拍板：**审批对话压根不该进待提炼清单**。
    #   `draftbatch` 不是内容块（`marks.validate_source` 明确拒挂痕迹），但原先
    #   这个循环会把它当「有我的字」的来源收进 buckets —— 只要在审批浮层说过
    #   一句话，此后每点一次编译都会白跑一次 `extract`、报一条
    #   「1 处没提炼成：draftbatch:current」，而且**永远消不掉**（失败不写台账
    #   ⇒ 永远 state=new），还占掉 `EXTRACT_MAX_SOURCES` 一个名额（真实库实测：
    #   它是全库唯一 todo，10 个名额白占 1 个）。
    #   ★ 判据取 `wiki_agent.APPROVAL_SOURCE_TYPE`，不在这里重写 "draftbatch"
    #     字面量 —— 同一口径两处写，改一处漏一处就是下一次的坑。
    #   ★ 延迟 import：`wiki_agent` 依赖 `core.wiki`，本模块在 core 层被多处
    #     引用，放模块顶部会把 import 顺序变成隐式契约。
    from core import wiki_agent as wag
    approval_type = wag.APPROVAL_SOURCE_TYPE
    for m in (ChatMessage.objects.filter(role="user")
              .select_related("conversation").order_by("id")):
        if is_preset(m.body):
            continue
        c = m.conversation
        if c.source_type == approval_type:
            continue
        bump(c.source_type, c.source_id, m.created_at)

    for h in Highlight.objects.live().exclude(note="").only("source_type", "source_id", "note",
                                                             "created_at"):
        if (h.note or "").strip():
            # ★ 对话里的划线归到文章下（2026-11-02 修复：之前显示为 chat:xx）
            st, sid = _resolve_source(h.source_type, h.source_id)
            bump(st, sid, h.created_at)

    # ★ 2026-11-02 修复：没有备注的划线也要算进来（划线原文本身就是信号）
    #   之前只算有 note 的划线，导致「只划线没写备注」的文章不出现
    for h in Highlight.objects.live().filter(note="").only("source_type", "source_id",
                                                             "quoted_text", "created_at"):
        if (h.quoted_text or "").strip():
            st, sid = _resolve_source(h.source_type, h.source_id)
            bump(st, sid, h.created_at)

    for a in Annotation.objects.live().only("source_type", "source_id", "body", "created_at"):
        if (a.body or "").strip():
            # ★ 对话里的批注同样归到文章下（2026-11-03）：之前直接用 a.source_type
            #   入桶，会凭空多出一行点不开的 `chat:xx`，而 `my_words()` 是把它们
            #   并在文章里的 —— 一处没归、一处归了，等于又给指纹埋了第二种口径。
            st, sid = _resolve_source(a.source_type, a.source_id)
            bump(st, sid, a.created_at)

    rows = []
    for k, b in buckets.items():
        rec = led.get(k) or {}
        # ★ 指纹、「N 字」、「M 条」三样都从 signal_sig 取，与台账写入同一份定义
        sig, n_chars, n_items = signal_sig(b["source_type"], b["source_id"])
        b["words_chars"], b["n_items"] = n_chars, n_items
        if not rec:
            state = "new"
        elif rec.get("sig") and rec.get("sigv") == SIG_VERSION:
            state = "done" if rec["sig"] == sig else "changed"
        else:
            # 没有指纹、或指纹是旧算法算的 → 退回字数比较，下一次提炼会写成新版本
            state = "done" if int(rec.get("chars") or 0) == n_chars else "changed"
        b["state"] = state
        b["sig"] = sig
        b["key"] = k
        b["last_ledger_at"] = rec.get("at") or ""
        b["force"] = bool(rec.get("force"))  # ★ 是否强制提炼（无信号，直接从正文）
        b["n_candidates"] = IngestCandidate.objects.filter(
            source_type=b["source_type"], source_id=b["source_id"]).count()
        b["n_pending"] = IngestCandidate.objects.filter(
            source_type=b["source_type"], source_id=b["source_id"],
            status="pending").count()
        b["title"] = title_of(b["source_type"], b["source_id"])
        rows.append(b)

    # ★ 2026-11-02 修复：台账里有但 buckets 里没有的来源（force 提炼的无信号文章）
    #   这些文章没有「我的字」，但已经提炼过，要显示出来让人知道状态
    #   ★ 用「有没有 batch」判断是否真正提炼过（空 sig 也可能是 force 提炼的）
    for k, rec in led.items():
        if k in buckets:
            continue  # 已经在 buckets 里了
        if not rec.get("batch"):
            continue  # 没有 batch = 从没真正提炼过
        st, sid = k.split(":", 1)
        rows.append({
            "source_type": st, "source_id": sid,
            "words_chars": 0, "n_items": 0, "last_at": None,
            "state": "done",  # 提炼过，但没信号
            "sig": rec.get("sig", ""), "key": k,
            "last_ledger_at": rec.get("at", ""),
            "force": True,  # ★ 台账里没有 buckets = 强制提炼的
            "n_candidates": IngestCandidate.objects.filter(
                source_type=st, source_id=sid).count(),
            "n_pending": IngestCandidate.objects.filter(
                source_type=st, source_id=sid, status="pending").count(),
            "title": title_of(st, sid),
        })

    # 排序：先「该动手」的（new/changed），再按最后活动时间倒序
    order = {"new": 0, "changed": 1, "done": 2}
    rows.sort(key=lambda r: (order.get(r["state"], 9),
                             -(r["last_at"].timestamp() if r["last_at"] else 0)))
    # ★ limit=None = 全量（计数 / 决定这一轮处理谁）；给了数字才截（画表格）
    return rows if limit is None else rows[:max(1, limit)]


@lru_cache(maxsize=256)
def title_of(source_type: str, source_id: str) -> str:
    """来源标题。★ 取不到不报错，退回 `type:id` —— 收件箱是「导航」，
    因为一个条目被删就整块消失会更糟（那样人会以为是这里坏了）。

    ★ 缓存：`inventory()` 在**每次** /wiki 页面加载时都跑（零 LLM，但取标题要
      问 Miniflux）。不加缓存等于每开一次知识库就打 N 次外部接口；标题变了最多
      显示旧名字，刷新/重启即恢复 —— 这是「便宜且错得轻」的一侧。
    """
    try:
        blk = sourceblock.load(source_type, source_id)
        if blk and blk.get("title"):
            return blk["title"]
    except Exception:      # noqa: BLE001 —— 导航层降级，别让一条读不到炸掉整页
        log.debug("收件箱取标题失败 %s/%s", source_type, source_id, exc_info=True)
    return f"{source_type}:{source_id}"


# ------------------------------------------------------------------ 提炼（一次 LLM 调用）
SYSTEM = """你是「谛听」阅读器里的知识编辑。你的任务不是总结文章，而是从**读者的痕迹**里
找出值得沉淀进个人 wiki 的判断，并按五类判据归档。

【材料分三层，含义完全不同，绝不能混】
- 《正文》：作者的文章。**不是读者的观点**，只用来理解上下文。
  正文里的 `【引文】…【/引文】` 是**被作者引用进来的别人的话**（回帖里引用的提问、
  README 的 Note、新闻里转述的原话）：**不要把【引文】里的主张写成作者的判断**。
  引文很长、作者只回了一两句时，作者那几句才是正文的重点 —— 读者关心的是作者的
  回应，不是被引者说了什么。
- 《我划出的原文》：读者从正文摘出来的片段，表示他「在意这一段」。
  可能是重要事实，也可能是他认可（或存疑）的他人的观点。
  它**不是他的原创判断**，但它本身**可以沉淀**：读者摘出这段，说明这段对他有价值。
- 《我的原话》：读者自己敲的字（对话里的提问、划线备注、批注）。
  **只有这一层是他的原创判断**。
- 《来源》：材料抬头那行，这条内容**是谁发布的**（订阅源）。claim 要写主体名时
  从这里取；想把它换成更通行的称呼（账号名 → 真人名），必须有材料里**别的依据**
  （比如引文里出现了「段总」），否则就照写来源里的名字 —— **不许凭记忆补人名**。

【候选来源】
- quote 可以来自《我的原话》（读者的原创判断）或《我划出的原文》（读者在意的事实/观点）。
- 两层都能产出候选，但语义不同：前者是「我的结论」，后者是「我在意/认可的东西」。

【五类判据，选最贴切的一个】
- surprise 意外：与读者原有认知冲突，或他原本不知道的结论
- reuse 复用：下次遇到同类问题能直接拿来用的做法/判据
- decision 决策：读者做出的取舍与理由（选 A 不选 B，为什么）
- pitfall 坑：踩过的坑、失效条件、代价
- transfer 可迁移：能搬到别的领域的方法或结构

【硬约束】
1. 每条候选的 quote 必须**逐字**摘自《我的原话》或《我划出的原文》：
   不改写、不缩写、不拼接。抄不出逐字片段的想法一律不要输出——宁可少一条。
2. claim 是**给三个月后的自己看的一句话**（≤40 字），必须能脱离 quote 独立读懂。
   - quote 是**证据**，claim 是**你的理解**，两者不是同一句话：
     quote 是「嗯嗯，明天卖点某公司股票的看跌期权。」，claim 要写成
     「某作者计划明天卖出该公司的看跌期权」——把「嗯嗯」「明天」「他」还原成
     具体的人、时间、动作，让人三个月后不用再读 quote 也看得懂。
   - 用第三人称写（读者 / 作者名字），不要照抄 quote 的口语与指代
     （「嗯嗯」「他」「这个」这类词在 claim 里必须还原成具体的人和时间）。
   - 可以补上材料里**本来就有**的主语、时间、对象 —— 那是还原语境，不是编造；
     材料里没有的名字、数字、事件一律不许加。
   - **不许改变原话的动作方向**：卖 put 就是卖 put，不是买 put，也不等于「看好」；
     改写只负责把话说完整，不负责替他把意思改掉。
   - quote 若来自《我划出的原文》，claim 要如实写成「读者在意/认可的事实或他人观点」，
     **不要**把它说成读者自己的原创结论。
   - claim 与 quote 逐字相同（或只是 quote 里的连续一段）= 没理解，等于把原文当候选，
     这种会被程序直接丢弃，不要输出。
3. 《正文》**永远不能作为 quote 的来源**。
4. 如果《我的原话》和《我划出的原文》里都没有值得沉淀的东西（比如读者只是提问、
   没表达看法、也没划出有价值的片段），返回 {"candidates": []}。这是**正确结果**，不要硬凑。
5. domain 取 __DOMAINS__；confidence 取 high/medium/low（看读者原话有多明确）。
6. 只输出 JSON，不要解释、不要 markdown 代码块。

输出格式：
{"candidates":[{"kind":"decision","claim":"…","quote":"…","domain":"ai","confidence":"medium"}]}
"""


def _system_prompt(force: bool = False) -> str:
    """把域清单填进提示词。force=True 时允许从正文提取。

    ★ 不用 `str.format()`：提示词里全是 JSON 示例的花括号，format 会把
      `{"candidates": …}` 当成占位符，直接 KeyError（第一次实现就踩了，
      而且是在真实调用路径上炸，单测没跑那一行时完全看不见）。
      用显式占位符替换，谁也不会把数据当语法。
    """
    prompt = SYSTEM.replace("__DOMAINS__", ", ".join(domains_for_prompt()))
    if force:
        # ★ force 提炼时允许从正文提取事实/观点（用户明确要求）
        prompt = prompt.replace(
            "《正文》**永远不能作为 quote 的来源**。",
            "《正文》通常不能作为 quote 的来源，但**本次是用户强制提炼**，"
            "允许从正文提取重要事实或观点，quote 标注为来自正文即可。"
        )
        prompt = prompt.replace(
            "如果《我的原话》和《我划出的原文》里都没有值得沉淀的东西",
            "如果《我的原话》、《我划出的原文》和《正文》里都没有值得沉淀的东西"
        )
    return prompt


def _material(source_type: str, source_id: str, words: dict, blk: dict) -> str:
    """拼材料。★ 三个小标题本身就是说明书：模型的默认倾向是把清单里的东西
    全当成「读者的观点」，光靠 SYSTEM 里的规则压不住，标签要写在数据旁边。"""
    limit = max(300, int(settings.WIKI_INGEST_ARTICLE_CHARS))
    # ★ 引用块边界走 `reporttext.body_text`：日报/周报、阅读时的对话、本模块
    #   三条链路共用同一个判据。这里原来自己写了一遍 `split_blockquotes` ——
    #   同一个判据抄两份的必然结果，就是 2026-11-02 只修了这一份、对话那份漏了
    #   （`conversation._material` 到 2026-11-03 还在拿被 `to_plain` 洗过的 text）。
    raw = reporttext.body_text(blk)
    body = reporttext.clip_quotes(raw, limit)
    cut = len(raw) > limit

    parts = [f"# 材料：{blk.get('title') or '（无标题）'}"]
    # ★ 主体名要有据可依：「某人在某场合表示…」这种 claim 里的主语，原先材料里
    #   一个字都没有 —— 模型只能靠记忆里的别名对照，容易张冠李戴。来源行由
    #   `sourceblock.source_note` 出，两条材料链路共用一份。
    note = sourceblock.source_note(blk)
    if note:
        parts.append(note)
    parts += ["", "## 正文（作者的，不是我的）", body]
    if cut:
        parts.append(f"（正文超过 {limit} 字，以上为前 {limit} 字）")

    # ★ 合并文章正文划线 + 对话里的划线（2026-11-02 修复：对话划线之前被漏掉）
    hs = marks.list_marks(source_type, source_id)["highlights"]
    chat_hs = chat_highlights(source_type, source_id)
    all_hs = hs + [{"id": h["id"], "quoted_text": h["quoted_text"], "note": h["note"],
                     "mark_id": None, "msg_id": h["msg_id"]} for h in chat_hs]
    if all_hs:
        parts += ["", f"## 我划出的原文（{len(all_hs)} 条；我在意/认可，可能值得沉淀，"
                      "但**不是我的原创判断**）"]
        for i, h in enumerate(all_hs, 1):
            parts.append(f"{i}. 「{(h.get('quoted_text') or '').strip()}」")

    items = words["items"]
    cap = max(200, int(settings.WIKI_INGEST_WORDS_CHARS))
    parts += ["", f"## 我的原话（{len(items)} 条；**我自己敲的字，只有这部分算我的判断**）"]
    used = 0
    shown = 0
    for it in items:
        text = it["text"]
        if used + len(text) > cap:
            break
        used += len(text)
        shown += 1
        parts.append(f"[{WORD_KINDS.get(it['kind'], it['kind'])}] {text}")
    if shown < len(items):
        parts.append(f"（还有 {len(items) - shown} 条原话因超长未列出）")
    return "\n".join(parts)


def verify_quotes(cands: list, words: dict, highlights: Optional[list] = None,
                  article_text: str = "", quote_text: str = "") -> tuple:
    """候选守卫：quote 的出处必须真实，claim 不能是 quote 的复制。

    返回 (kept, dropped)，kept 里每条带 `quote_from`：`mine`（我敲的字）、
    `highlight`（我划出的原文）或 `article`（正文本身，仅 force 提炼时允许）。
    以及**锚点**：`mark_id` / `msg_id`（可空）。
    锚点 = 命中的那条具体记录（划线/批注/消息）的 id，是编译阶段拼精确深链
    的唯一事实源，不靠文本反查（见 my_words 里的 ★★ 说明）。

    ★ 为什么划线的原文也能当 quote 来源（2026-11-01 用户纠正）：划线摘录虽然
      是别人的字，但「我摘出这一段」这件事本身代表我的**特别关切** —— 它可能是
      我认可的他人的观点，也可能是重要事实。这些同样值得沉淀，只是语义不同：
      `mine` 是我的判断，`highlight` 是我在意/认可的东西，`article` 是正文事实。
      三层都收，但**分开标注**，绝不让「我划出的原文」或「正文」伪装成「我的原创判断」。

    ★ 这是本模块唯一**不可省**的校验。模型改写过一句「我的原话」或「我划的原文」，
      看着完全像我的东西，但其实是它写的 —— 那正是把 AI 的观点伪装成我的判断，
      比不提取坏得多。程序比对能挡住，prompt 挡不住。

    ★★ 锚点命中必须是**单条**：q 是子串、可能同时命中多条（同话说过多次）。
      用「最短命中」（最接近原文长度的那条）当锚点，其余不管 —— 反正是逐字子串，
      最短那条最可能就是被摘的那一条；真配错也只是一条深链跳得稍有偏差，
      比「不存锚点、编译时猜」好一个量级。
    """
    kept, dropped = [], []
    for c in cands:
        q = norm(c.get("quote") or "")
        if not q:
            dropped.append({**c, "_why": "缺 quote"})
            continue

        # ★ claim 不许是 quote 的复制（2026-11-03 用户原话：「为啥直接把原文当候选呢？
        #   llm 应该会有自己的理解」）。候选的价值全在 claim 这一句 —— 它就是
        #   「三个月后的自己能不能一眼看懂」。claim 与 quote 归一化后完全相同，
        #   说明模型没理解、只是把原文搬进了 claim 栏：这种候选既没有沉淀价值，
        #   还让人以为提炼根本没做。宁可不产出，也不产出这种。
        # ★ 只判"完全相同"，不判"claim 是 quote 的一段"：删掉「这」「嗯嗯」这类
        #   缩句是合格的简化（「七格只适合 ToB」← 「这七格只适合 ToB」），
        #   拿子串判会把它误杀。**守卫过严会让人失去对守卫的信任**，比漏一条更贵。
        cl = norm(c.get("claim") or "")
        if cl and cl == q:
            dropped.append({**c, "_why": "claim 与 quote 逐字相同（没理解，等于把原文当候选）"})
            continue

        # 先在「我的原话」里找命中（逐条带 id）
        hit_mine = None
        for it in words["items"]:
            if q in norm(it["text"]):
                cand = {"quote_from": "mine",
                        "msg_id": it.get("msg_id"), "mark_id": it.get("mark_id")}
                if hit_mine is None or len(it["text"]) < hit_mine[1]:
                    hit_mine = (cand, len(it["text"]))
        if hit_mine is not None:
            kept.append({**c, **hit_mine[0]})
            continue

        # 再在「我划出的原文」里找（逐条带 id）
        hit_hl = None
        for h in (highlights or []):
            if q in norm((h.get("quoted_text") or "").strip()):
                if hit_hl is None or len(h.get("quoted_text") or "") < hit_hl[1]:
                    hit_hl = ({"quote_from": "highlight", "mark_id": h.get("id")},
                              len(h.get("quoted_text") or ""))
        if hit_hl is not None:
            kept.append({**c, **hit_hl[0]})
            continue

        # ★ 最后在「正文」里找（force 提炼时允许）
        if article_text and q in norm(article_text):
            kept.append({**c, "quote_from": "article", "mark_id": None, "msg_id": None})
            continue

        # ★ 落选理由要分两种（2026-11-03）：命中【引文】= 模型读懂了这篇、只是
        #   摘错了层（引文是别人的话）；哪儿都没有 = 模型编的。混成一句
        #   「不是我的原话，也不是我划的原文」，人就分不清模型是笨还是坏。
        #   前者还带着一条出路：他若真在意这句，划一条线就合法了 —— 这比让他
        #   自己去猜「为什么这条没了」有用得多。
        # ★ 只影响文案，不影响任何判定：quote_text 不参与「能不能过守卫」。
        if quote_text and q in norm(quote_text):
            dropped.append({**c, "_why": "这句在【引文】里（别人说的），不是作者的判断 —— "
                                        "若你在意它，划线后就能作为候选"})
            continue
        dropped.append({**c, "_why": "quote 不是我的原话，也不是我划的原文"})
    return kept, dropped


def parse_candidates(text: str) -> list:
    """从模型输出里取候选数组。解析不出来返回 []（由调用方判「空结果」）。"""
    for obj in jsontool.iter_json(text or ""):
        if isinstance(obj, dict) and isinstance(obj.get("candidates"), list):
            return [c for c in obj["candidates"] if isinstance(c, dict)]
        if isinstance(obj, list):
            return [c for c in obj if isinstance(c, dict)]
    return []


def clean_candidates(cands: list, limit: int) -> list:
    """逐条规整 + 校验枚举值；非法项直接丢（不修正——猜它的意思是替它编）。"""
    prompt_domains = domains_for_prompt()
    out = []
    for c in cands[:max(1, limit)]:
        kind = str(c.get("kind") or "").strip().lower()
        claim = str(c.get("claim") or "").strip()
        quote = str(c.get("quote") or "").strip()
        if kind not in KIND_KEYS or not claim or not quote:
            continue
        domain = str(c.get("domain") or "").strip().lower()
        conf = str(c.get("confidence") or "").strip().lower()
        out.append({"kind": kind, "claim": claim[:200], "quote": quote[:400],
                    "domain": domain if domain in prompt_domains else "",
                    "confidence": conf if conf in CONFIDENCE else ""})
    return out


def extract(source_type: str, source_id: str, force: bool = False) -> dict:
    """跑一次「分诊 + 提取」。返回 {ok, err, batch, created, dropped, words_chars, n_words}。

    err 是**直接能显示给人**的话（字数不够、预算用尽、读不到正文、模型没输出）。
    ★ 0 条候选不是失败（ok=True, created=0）——「这篇不值得沉淀」是合法结论，
      但台账仍要更新，否则收件箱会一直提示「还没提炼」（重复花钱）。
    ★ force=True 时跳过信号检查（2026-11-02）：人主动点「发送到 wiki」时，
      即使没有划线/批注/对话，也允许从文章内容直接提炼。
    """
    if not settings.WIKI_INGEST_ENABLED:
        return {"ok": False, "err": "提取未开启（WIKI_INGEST_ENABLED=0）",
                "batch": "", "created": 0, "dropped": [], "words_chars": 0, "n_words": 0}

    # ★ 审批未完成时拒绝新提炼（2026-11-09 用户澄清）：草稿待审批时不该开新一轮提炼，
    #   否则候选越堆越多、审批上下文被打散。编译中（compile_run.running）由锁挡，
    #   编译后待审批由 approval_active 挡 —— 两者分开，不冲突。
    from core import wiki as wstore
    if wstore.approval_active():
        return {"ok": False, "err": "审批进行中（有草稿待裁决），先跑完审批再提炼",
                "batch": "", "created": 0, "dropped": [], "words_chars": 0, "n_words": 0,
                "paused": True}

    st, sid, verr = marks.validate_source(source_type, source_id)
    if verr:
        return {"ok": False, "err": verr, "batch": "", "created": 0,
                "dropped": [], "words_chars": 0, "n_words": 0}

    words = my_words(st, sid)
    # ★ 信号 = 我的字 + 我划出的原文（两者现在都能产出候选）。
    #   「完全没有划线和批注」= 信号为 0，才该忽略；其余都放行 —— 重要判断
    #   不一定长，一句「好」就代表认可（2026-11-01 用户两次纠正：先 ≥15→≥8，
    #   再放宽到「有一个字就认」，门槛默认 1）。
    # ★ 2026-11-02 修复：对话里的划线也要算进信号（之前只算文章正文的划线）
    hl_signal = marks.list_marks(st, sid)["highlights"]
    chat_hs = chat_highlights(st, sid)
    all_hl_signal = hl_signal + chat_hs
    signal_chars = words["chars"] + sum(
        len((h.get("quoted_text") or "").strip()) for h in all_hl_signal)
    need = int(settings.WIKI_INGEST_MIN_CHARS)
    if signal_chars < need and not force:
        # ★ 零成本闸：不调模型、不记账。原因要具体（多少字、差多少），
        #   否则人只会看到「提炼失败」而不知道为什么。
        return {"ok": False,
                "err": f"这篇没有可沉淀的信号（我的字 + 划线摘录共 {signal_chars} 字，"
                       f"需要 ≥{need} 字）。先写点自己的判断（对话里说、或给划线加备注），"
                       f"或划出几段你在意的原文，再来提炼。",
                "batch": "", "created": 0, "dropped": [], "words_chars": words["chars"],
                "n_words": len(words["items"])}

    blk = sourceblock.load(st, sid)
    if blk is None:
        return {"ok": False, "err": "读不到这篇的正文（条目可能已删除）",
                "batch": "", "created": 0, "dropped": [], "words_chars": words["chars"],
                "n_words": len(words["items"])}

    # ★ 受控 domain 清单解析不了就**别提炼**。放在调模型之前：既不白花钱，也不
    #   会把「所有 domain 都被清空」伪装成一次成功的提炼（静默失败最贵）。
    domains = wstore.ontology_domains()
    if not domains:
        log.error("domain 受控清单解析失败：%s", wstore.ONTOLOGY_PATH)
        return {"ok": False,
                "err": "domain 受控清单读不到（Meta/ontology.md 的「领域」表），已停止提炼",
                "batch": "", "created": 0, "dropped": [], "words_chars": words["chars"],
                "n_words": len(words["items"])}

    batch = uuid.uuid4().hex[:12]
    # ★ stats 要留下来看 `truncated`（2026-11-09）：正文被硬截断时，返回的 JSON 是
    #   半截 —— 解析出来的候选必然少几条，而**台账照样会被写成 done**，等于这批
    #   内容永久少了一部分、还不会再去提炼。真实案例：10:02:15 那次 in=3214
    #   out=4096（正好撞顶）created=0、耗时 27.7 s，钱花了、内容没了、状态变 done。
    stats = citeguard.fresh_stats()
    text, err = citeguard._call(
        "wiki_extract", _system_prompt(force=force), _material(st, sid, words, blk),
        stats, max_calls=1,
        max_tokens=int(settings.WIKI_INGEST_MAX_TOKENS),
        temperature=float(settings.WIKI_INGEST_TEMPERATURE),
        pool=cr.POOL_WIKI, tag="wiki 提取",
        label=(blk.get("title") or sid)[:40])
    cut = bool(stats.get("truncated"))

    if err or not text:
        log.warning("wiki 提取失败 %s/%s: %s", st, sid, err or "空输出")
        return {"ok": False, "err": err or "模型没有返回内容", "batch": "",
                "created": 0, "dropped": [], "words_chars": words["chars"],
                "n_words": len(words["items"])}

    raw = parse_candidates(text)
    # ★ 「模型输出不合格」和「确实没有候选」是两件事，不能都算成 0 条：
    #   前者是真故障（要人看），后者是合法结论。判据是约定的键有没有出现 ——
    #   {"candidates": []} 含它，一段散文解释不含它。混起来会静默丢结果。
    if not raw and "candidate" not in (text or "").lower():
        log.warning("wiki 提取输出不合格 %s/%s：%s", st, sid, (text or "")[:120])
        return {"ok": False, "err": "模型输出不是约定的 JSON（没找到 candidates）",
                "batch": "", "created": 0, "dropped": [], "words_chars": words["chars"],
                "n_words": len(words["items"])}
    cleaned = clean_candidates(raw, int(settings.WIKI_INGEST_MAX_CANDIDATES))
    # ★ 划线也要进引用守卫的 hay：我划出的原文同样是合法 quote 来源（见 verify_quotes）
    # ★ 2026-11-02 修复：对话里的划线也要参与引用守卫
    hl = marks.list_marks(st, sid)["highlights"]
    chat_hs_for_verify = [{"id": h["id"], "quoted_text": h["quoted_text"], "note": h["note"],
                            "mark_id": None, "msg_id": h["msg_id"]} for h in chat_hs]
    # ★ force 提炼允许 quote 来自正文，但正文里必须**剔除引文段**：
    #   拿未洗过的 `blk["text"]` 当 hay，被引者的话同样过守卫、还被标成
    #   「正文事实」—— 那正是「从引文里提炼候选」的最后一道口子。
    # ★ 但**剔除 ≠ 从材料里删掉**（2026-11-03 用户追问）：模型看到的正文始终是
    #   完整的 `body_text(blk)`，引文原文都在、只带【引文】标记。这里剔除的只是
    #   「归属资格」—— 谁能被标成「作者的判断」。引文交代的背景，模型照样读得到。
    body_all = reporttext.body_text(blk)
    article_text_for_verify = reporttext.unquoted(body_all) if force else ""
    kept, dropped = verify_quotes(cleaned, words, highlights=hl + chat_hs_for_verify,
                                  article_text=article_text_for_verify,
                                  quote_text=reporttext.quoted(body_all))

    # ★ 内容发布时间：整批候选共用一个来源时间 —— 一次 extract() 只处理一个
    #   (source_type, source_id)，来源只有一个发布时间。绝不去 my_words 的 items
    #   里按 mark_id 找 `at`（那是我什么时候写的，不是内容什么时候发布的）。
    # ★ domain 再过一道受控闸：清单外一律落空，不写清单外的域名（「模型建议新
    #   domain → 人确认晋升」是后续改动，本步只做到「不凭空造」）。
    # ★ tags 本步一律 []：模型建议 / 审批增删属后续环节，提炼 prompt 未改动。
    published_at = (blk or {}).get("published_at")
    objs = [IngestCandidate(batch=batch, source_type=st, source_id=sid,
                            kind=c["kind"], claim=c["claim"], quote=c["quote"],
                            domain=(c["domain"] if c["domain"] in domains else ""),
                            confidence=c["confidence"],
                            quote_from=c.get("quote_from", "mine"),
                            mark_id=c.get("mark_id"), msg_id=c.get("msg_id"),
                            published_at=published_at, tags=[])
            for c in kept]
    if objs:
        IngestCandidate.objects.bulk_create(objs)

    # ★ 台账一定要写，即使是 0 条：否则收件箱永远显示「还没提炼」，人会反复点
    #   ★★ 但**被截断的那次除外**（2026-11-09）：输出被硬切时 JSON 是半截，候选
    #     必然少几条。写台账 = 把这篇标成「提炼过了」，下次不再提炼 ⇒ 少掉的那部分
    #     内容永久蒸发，而且一个字都不会报。宁可下次再花一次钱，也不静默少提炼。
    if cut:
        log.warning("wiki 提取被截断（%s/%s，这次只拿到 %d 条）：不记台账，下次会重提炼",
                    st, sid, len(objs))
    else:
        led = read_ledger()
        # ★ 保留原有的 force 标记：重新提炼时不丢失"这是用户主动触发的"信息
        existing_force = bool(led.get(_key(st, sid), {}).get("force"))
        # ★★ 指纹与字数必须和收件箱**同一份口径**（signal_sig）：这里若只按「我的原话」
        #   记账，而收件箱还把没备注的划线原文算作信号，两边永远对不上 —— 提炼完仍显示
        #   「提炼后又写了」，人以为没生效、再点一次再花一次钱（2026-11-03 entry 6670）。
        sig, sig_chars, _n = signal_sig(st, sid, words)
        led[_key(st, sid)] = {
            "at": timezone.localtime().strftime("%Y-%m-%d %H:%M"),
            "chars": sig_chars, "sig": sig, "sigv": SIG_VERSION,
            "batch": batch, "n": len(objs),
            **({"dropped": len(dropped)} if dropped else {}),
            **({"force": True} if (force or existing_force) else {}),
        }
        _write_ledger(led)

    if dropped:
        # ★ 不静默：丢弃是守卫在起作用，但人要知道「模型试图编 2 条」这件事
        log.info("wiki 提取丢弃 %d 条（引用对不上 / claim 照抄原文） %s/%s",
                 len(dropped), st, sid)

    return {"ok": True, "err": "", "batch": batch, "created": len(objs),
            "dropped": dropped, "raw": len(raw), "words_chars": words["chars"],
            "n_words": len(words["items"]), "truncated": cut}


# ------------------------------------------------------------------ 候选的读写
def list_candidates(*, status=None, source=None, limit=60) -> list:
    """候选列表。`status` 不传 = **未丢弃全部**（pending + accepted）。

    ★ 2026-11-05 候选免审：候选层不再逐条人审，「未丢弃 = 可编译」。
      默认口径跟着改成「未丢弃全部」；显式传 `status` 时仍精确按该状态过滤
      （待审面板 / 已处理折叠区都靠显式传参，行为不变）。
    ★ 用 `exclude(discarded)` 而不是 `filter(pending, accepted)`：前者跟
      `compile_backlog` 的判据同形，将来加新状态时不会两边漂。
    ★ `limit=None` = 全量（2026-11-09）：查询页承诺「翻看全部候选」，默认 60
      会在真实库 66 条 pending 时静默截掉 6 条 —— 计数说 28 条已进草稿、
      列表只画出 25 条，数字和真相各说各话（与 inventory 那次同一个坑）。
    """
    qs = IngestCandidate.objects.all()
    if not status:
        qs = qs.exclude(status="discarded")
    else:
        qs = qs.filter(status=status)
    if source:
        st, sid = source
        qs = qs.filter(source_type=st, source_id=sid)
    qs = qs.order_by("-id")
    return list(qs if limit is None else qs[:max(1, limit)])


def set_status(cid: int, status: str) -> tuple:
    """改候选状态（采纳 / 丢弃）。返回 (ok, err)。

    ★ 只接受三个合法值 —— 拼错会写进一个谁都不认的状态，然后这条候选
      既不在待审里、也不在已采纳里，**静默消失**。
    ★ 已编进草稿/页的候选拒绝在这里丢弃（2026-11-09）：本函数只改 DB，
      草稿 frontmatter 的 `candidates` 仍挂着这个 id —— 审批照样能过，入库后
      溯源区多一条「已不存在」的幽灵引用，且 polish 的素材被污染。分组的真相
      在产物里，要排除它只能去审批浮层让助手移出分组，已入库的页先从详情页
      拉入审批编辑。判据与编译共用 `_compiled_ids()`。同日起查询页纯查询、
      没有丢弃按钮，本函数是 API/脚本层的兜底守卫。
    """
    valid = {k for k, _ in INGEST_STATUS}
    if status not in valid:
        return False, f"非法状态：{status!r}"
    if status == "discarded" and cid in _compiled_ids():
        return False, ("候选 #%s 已编进草稿或已入库页，这里不能直接丢弃（会让那份草稿"
                       "挂着幽灵引用）。还在草稿：去审批浮层让助手把它移出分组；"
                       "已入库：先从详情页「拉入审批编辑」再排除。" % cid)
    n = IngestCandidate.objects.filter(id=cid).update(status=status)
    return (True, "") if n else (False, "候选不存在")


def stats() -> dict:
    """给 /wiki 顶部计数用的几个数。"""
    rows = (IngestCandidate.objects.values("status")
            .annotate(n=Count("id")).values_list("status", "n"))
    m = dict(rows)
    return {"pending": m.get("pending", 0), "accepted": m.get("accepted", 0),
            "discarded": m.get("discarded", 0)}


# ==========================================================================
# 2.3 编译：未丢弃候选 → `.drafts/<slug>.md` 草稿
#
#
# ★★ 保守路线的全部含义（用户 2026-11-02 拍板）：
#   **编译不生成「我的」连贯正文。** 它只做归并、去重、归类、排序、挂出处。
#   让模型用第一人称写一段通顺的话，它必然会把「别人的原文」和「它自己的归纳」
#   都写成「我认为」—— 那是第一人称的「自信的错误」，比第三人称难发现得多。
#   所以正文里每一条都必须**能指回一条具体候选**（`cand_id`），指不回去的不进来。
#
# ★ 深链由**程序**拼（`sourceblock.source_url`），模型只负责挑 `cand_id`。
#   让模型拼 URL，错一个字符就是死链，而且改 URL 规则时要改提示词 —— 双份漂移。
# ==========================================================================

# ⛔ COMPILE_MAX_CANDS（一次编译的候选上限 60）已于 2026-11-09 按用户拍板删除：
#   提炼已藏进一条龙，候选就该一次消费完，别留「待编译」积压到下轮。流入速率由
#   EXTRACT_MAX_SOURCES 限住；真实账全天wiki侧 ≈¥0.68，60 条的闸离成本约束很远，
#   反而制造「提炼产出 > 编译消费」的单位错配（30 来源产 66 条，6 条永远慢一轮）。

#: 一条龙一次最多提炼多少个来源（每个来源 = 一次模型调用）。
#: ★ 2026-11-09 补：这个上限此前**不存在**，是被 `inventory()` 的默认 limit=30
#:   **意外**当成的上限 —— 「一次最多 30 个来源」是隐含行为，且超出的部分
#:   **一个字都不报**（清零重来后真实库 33 个来源，3 个静默不提炼）。
#:   现在把它显式命名 + 剩余数如实回报（`extract_left`），人知道「还差几处，
#:   再点一次接着来」。上限的意义是「一次别花太多钱」，不是「后面的永远够不着」。
#: ★ 2026-11-09 用户拍板 30→10：真实账（LlmUsage 当日）单来源提炼 ≈¥0.011，
#:   钱不是瓶颈，**人工审批吞吐才是** —— 30 来源产 66 条候选、编出 15 份草稿，
#:   人一轮裁决不动这么多。上限跟着消化能力走：10 来源 ≈ 20 条候选 ≈ 一批 S1
#:   多一点，一轮的草稿量人裁决得动。（同日取消「审批中暂停」：草稿可以接着编，
#:   不再锁死下一轮，但单轮 inflow 仍由这个上限限住。）
EXTRACT_MAX_SOURCES = 10

#: 编译正文的生成预算。★ 4096 不是随手写的：推理模型的推理链计入 completion_tokens，
#: 1600 会被推理吃光 → content 空串 → 翻倍重试（周报分片实测单片 192 秒，见记忆）。
COMPILE_MAX_TOKENS = 4096

#: S1（分组）一批判几条。2026-11-03 设 8（21 条一次判会把预算全吃光）；2026-11-04
#: 用户定方向调到 16（分批是不得已，最好不分批）；2026-11-09 用户拍板**调回 8**：
#: 当日真实账 16 条/批的 5 次 S1 调用 3 次 completion 顶到 4096（撞顶后靠对半拆批
#: 兜底才落地，多花一倍钱），且 60 条喂进去 32 条被漏分。S1 整轮才 ≈¥0.2，
#: 多拆几批的页目录开销可忽略，换推理从容、漏分更少。
#: 配套两条让分批不再「瞎」：
#:   ① 后面的批次看得见前面批次已定下的组（`pending_groups` / `join`），跨批的
#:      同一概念能合成一组，一次审批就好；
#:   ② 待审批草稿进页目录（`draft: true`），新候选并进草稿而不是被拒之门外。
#: 推理量随「要同时比较的候选数」涨得比候选本身快，页目录是每批的固定开销；
#: 往大调就回到「21 条一次判、推理吃光 8192、产出 0 页」那一档。
COMPILE_S1_BATCH = 8

# ------------------------------------------------------------------ ⛔ 已退役：三块体系
#: ★★ 第十三步（形态 A）**有意裁剪，不是遗漏**：下面这一段常量 / 两条 S2 提示词、
#:   `_label_for` / `_cand_line` / `_split_sections` / `_append_hint` /
#:   `_render_sections` 全部**不再被任何写路径调用**。正文改为「纯 polish 一段」，
#:   S2 不再调模型，`_compose_page` 只组装 frontmatter。
#:   来源区分下沉到候选 `quote_from`（提炼阶段已由 `verify_quotes` 程序锁死），
#:   精确出处由折叠溯源区读 DB 负责；数字守卫搬到 `wiki_loop._check_numbers`。
#:   这些定义只为「读清零前的老页 / 旧测试固定装置」保留，**新代码不得引用**；
#:   `verify_wiki.py` 有一条静态契约守住这条边界。清零完成后可整段删除。
# ----------------------------------------------------------------------------------------
#: 原「我的判断 / 事实与他人观点 / 我的在意点」三块的键（S2 返回 JSON 里的 section）。
COMPILE_SECTIONS = ("judge", "material", "interest")

#: 素材类型：判断（我说的）/ 事实（客观）/ 他人观点（谁说的）/ 在意（我划了没表态）
COMPILE_TYPES = ("judgment", "fact", "other_opinion", "interest")

#: G2/G3 的人称词。「事实与他人观点」块里出现这些词 = 把别人的话写成了我的主张。
_FIRST_PERSON = re.compile(r"我认为|我觉得|我判断|我相信|我认[为同]|我看好|我看空|我主张")

#: ★ 每一块**接受哪些来源语义**（2026-11-03）。三层语义必须各有落点：
#:   mine（我敲的字）→ 我的判断；highlight（我划的原文）→ 事实/在意点；
#:   article（正文事实，仅强制提炼产生）→ 事实。
#:   之前只写了「judge 要 mine、material/interest 要 highlight」，`article` 两头
#:   都不在 —— 被采纳的正文事实候选**永远过不了守卫**，面板却一直把它算进
#:   「待编译」，人点了只在最后看到「所有候选都没通过守卫」。三层语义只落地两层，
#:   第三层就成了黑洞。
_SECTION_ALLOWS = {
    "judge": ("mine",),
    "material": ("highlight", "article"),
    "interest": ("highlight",),
}

#: 每块的落选理由。**要说清是哪一层被写成了哪一块** —— 一律写「划出的原文被写成
#: 我的判断」会让人以为候选里有划线，实际那条是正文事实，修的方向完全不同。
_DENY_WHY = {
    ("judge", "highlight"): "划出的原文被写成「我的判断」",
    ("judge", "article"): "正文事实被写成「我的判断」",
    ("material", "mine"): "我敲的字被降级成素材",
    ("interest", "mine"): "我敲的字被降级成素材",
    ("interest", "article"): "正文事实被写成「我在意」（在意点只能来自我划的原文）",
}
_QF_ZH = {"mine": "我的原话", "highlight": "划出的原文", "article": "正文事实"}
_SECTION_ZH = {"judge": "我的判断", "material": "事实/他人的观点", "interest": "我在意"}

#: G5：结论词。「可能的联系」只许陈述「可能指向同一问题」，不许替我下结论。
_CONCLUSION = re.compile(r"所以|因此|可见|说明应该|建议|结论是|应当|必须要|值得买|应该买")

COMPILE_S1_SYSTEM = """你是「谛听」知识库的编辑，负责**分组**：把一组待编译的知识候选，
按「是否属于同一个原子概念」分成若干组，每组将编译成一个 wiki 页。

【分组的价值在于「合并」，不是给每条判断建文件夹】
- 反例（错）：11 条候选 → 11 页。那不是分组，只是把候选换了个地方存。
- 正例：同一篇文章里，「投资要看十年后的现金流」「十年后无法精确预测，只能毛估估」
  「核心是 right business，本质是提高胜率」—— 三条在回答**同一个问题**
  （投资该看什么、能看多远）→ **合成一页**。
- 判据：两条候选**在回答同一个问题**（哪怕角度不同、结论互相补充）就合并；
  只有当它们回答**不同问题**（一条讲投资判断、一条讲怎么读长文）时才拆开。
- 同一来源、同一 domain 的候选，通常应落在 1–2 页内；明显超过说明你拆得太碎。

【判据】
1. 一个组 = 一个能独立回答「X 是什么 / 怎么办 / 我怎么看」的概念。**不是一篇文章、不是一个主题**。
2. 同一判断的不同表述 → 合并成一组（这是分组的价值：去重）。判断不同 → 分开。
3. **绝不跨 domain 合并**：domain 不同的候选不能进同一组。
4. 每条候选必须且只能出现在一组里。不许丢、不许重复。
5. 组数由内容决定，不要预设；但**「一条候选一页」是错的结果**，先尝试合并，
   合并不了才拆。

【已有页：能并就并，不能并就新建】
材料里的 `pages` 是库里**已有**的页（slug / title / domain / 一句话）。每组要给
一个 `target`：

- `"new"` —— 这组讲的是库里还没有的概念。
- `"existing:<slug>"` —— 这组在补充/修正**那一页**已经在回答的问题。
- `pages` 里带 `"draft": true` 的是**待审批草稿**：指到它同样合法（新内容会并进
  那份草稿，审批一次即可），不必为它另建新页。
- `"split:<slug>"` —— 这组候选要和 <slug> 页里**拆出来的那部分候选**组成一个新页。
  只在一种情况下用：<slug> 页现在混着**两个不同问题**的候选，其中一部分与本组在
  回答同一件事，另一部分属于别的概念。`pages[].candidates` 是该页现有候选的摘要
  （claim 已截短，够判断「在问什么」），`cands_more` 大于 0 说明这一页还有没列
  出来的候选。给 `split` 时**不要再给 `target`** —— 一个组只有一种去向。
  不要为了拆而拆：同一问题的不同侧面属于同一页；一页里只是讲得散，不是拆的理由。

判据不是「主题相近」，而是**回答同一个问题**：已有页的那句话和这一组候选在回答
同一件事，才并。判据要严，理由是两种错的代价不对称：
- 该并却新建 = 同一个概念两页，以后改一条判断要改两处（可修）；
- 不该并却并 = 把新判断塞进一页人已经确认过的知识里，那一页从此不可信（难修）。
所以**拿不准就新建**。只有下面两种可以并：
① 这组候选是同一判断的更具体版本（补了数字、时间、对象）；
② 这组候选在同一页的框架内补充了新的一条（同一问题的另一个侧面）。

并入时 `domain` 必须与目标页一致。如果 `pages_total` 比 `pages` 的条数多，说明只列了
最近的一部分页：清单里没有的 slug，你**不能确定**库里没有，这种时候按新建处理
（宁可多一页，也不要瞎猜一页去覆盖）。清单里根本没有的 slug 一律不许写。

【前面批次已生成的组】`pending_groups` 是本轮**前面批次**已经定下的组（gid 从 1 起）。
本批候选若与其中一组在回答同一个问题，给那组返回 `"join": <gid>`（domain 必须一致），
不要为它另开一组、也不要复述它已有的判断。没有合适的组时，照常 `new` / `existing`。

【给新页起名字与建议标签】
每组给一个 `slug`：ASCII kebab（小写字母数字与连字符），由页标题转写，
例如「十年现金流」→ ten-year-cash-flow。它只作文件名，人读的是 `title`。
每组给 `tags`：2–4 个中文词，概括这页在回答的问题，作为**建议标签**
（审批时人可增删，已填过的标签保持不动）。

材料里的 `tag_pool` 是库里**已经在用**的标签词（越靠前用得越多）。起 tags 时先看它：
- 能表达这个意思的，**逐字照抄** `tag_pool` 里那个词，不要换一种写法 ——
  `AI泡沫` 与 `AI 泡沫`、`大模型` 与 `LLM` 在筛选时是两个不同标签，点选一次
  就漏掉另一半。
- `tag_pool` 里确实没有合适的，才新造；一页 2–4 个标签里，新造的尽量不超过 1 个。
- 材料里**没有** `tag_pool` 这个字段 = 库里还没有任何标签，按内容自由起。

【输出】只输出 JSON，不要解释、不要 markdown 代码块：
{"groups":[{"title":"中文页标题","slug":"ascii-kebab","tags":["现金流","估值"],
"domain":"invest","cand_ids":[9,10],"target":"new","reason":"为什么这几条是同一概念"}]}
并入已有页时：`"target":"existing:ten-year-thinking"`（slug 逐字照抄 `pages` 里的）。
从已有页拆候选重组时：`"split":"ten-year-thinking"`（同样逐字照抄；不要再写 target）。
并进前面批次的组时：`"join": 2`（gid 照抄 `pending_groups` 里的）。
"""

#: S1.5（2026-11-06 第七步）：被 S1 点名「要拆某一页」的组，单独一次聚焦判断。
#: ★ 为什么独立成一条提示词（已与用户敲定）：S1 的主职是分组，它的提示词已经很长；
#:   再把「拆页怎么挑」塞进去，一个提示词同时干两件事，两件都会变糊。而这里的输入
#:   （一页的候选全明细 + 一组新候选）与输出（拆哪几条 + 新页名）跟分组完全不同。
#: ★ 这是渐进式披露的第二层：S1 只看目录卡（claim 截短），只有它点名要拆的那一页
#:   才付「全明细」这份贵钱 —— 页多的时候，这是「省钱」与「看得清」唯一的交点。
COMPILE_S1_SPLIT_SYSTEM = """你是「谛听」知识库的编辑，现在只做一件事：**从一页已入库的 wiki 页里，
挑出该拆走的候选** —— 它们将和这次新进来的一组候选合成一个新页。

【输入】
- `page`：一页已入库的 wiki 页。`page.candidates` 是它现在**全部**候选的明细
  （claim 是这条候选的判断，quote 是它从哪句话来）。
- `group`：这次新进来的一组候选，它们已经判定为在回答同一个问题。
- `domains`：新页可选的 domain。

【怎么挑】
一页里混着两个不同的问题时，拆开才对：其中与本组**在回答同一个问题**、合起来
能构成一个完整页的那部分，就是该拆走的（写进 `take`）。
判据是「在问什么」，不是「主题像不像」：
- 同一问题的不同侧面、更具体的版本、对同一判断的补充 → 仍属同一页，留在原页；
- 换了一个问题（一条讲投资该看什么，一条讲怎么读长文）→ 该拆走。
`take` 给空数组同样是一个合格的回答：这一页并不混，只是本组该另立一页。

【给新页起名】
`title` 是新页的页标题（中文，照实概括这批候选在回答的问题，≤20 字）；
`domain` 从 `domains` 里选，沿用 `page.domain` 是常见答案，只有新页确实属于另一个
领域时才改。

【输出】只输出 JSON，不要解释、不要 markdown 代码块：
{"take":[3,5],"title":"投资看的是十年后的现金流","domain":"invest","reason":"为什么这几条是同一个问题"}
`take` 里的每个 id 逐字来自 `page.candidates` 的 `cand_id`。
"""

COMPILE_S2_SYSTEM = """你是「谛听」知识库的编辑，负责把**一组**知识候选整理成一页 wiki 草稿的内容。
你**不写新观点**，只做归类、去重、排序；每一行都必须指回一条具体候选（cand_id）。

【三块分类，含义绝不同】
- judge（我的判断）：只能放 quote_from=mine 的候选 —— 那是读者自己敲的字。
- material（事实 / 他人的观点）：放 quote_from=highlight 或 article 的候选。
  type 取 fact（客观事实/事件）或 other_opinion（某人发表的观点）；other_opinion 要给 who（谁说的）。
  ★ quote_from=article 是**正文里的事实**（读者强制提炼时才有），type 必须是 fact，
  不要写成 other_opinion，更不许放进 judge —— 正文不是读者的话。
- interest（我的在意点）：只能放 quote_from=highlight、且读者**没有表态**的候选
  （划了但只是问句、或只是在意这条线）。type 固定 interest。

【text 怎么写：claim 就是底稿】
- 一条候选单独成行时，text **直接用它的 claim**，不要重写。claim 已经是为「三个月
  后的自己」写好的那一句 —— 主语、时间、动作方向都在里面；你重写一遍只会把它写
  含糊（把「明天卖点 put」写成「关注期权策略」就是写坏了，虽然更"像知识"）。
- 只有两种情况才改写：① 把几条同义候选合并成一行（合并后仍要保留每条的主语与动作
  方向）；② material 块补 who/type 时，在 claim 后面补，不改 claim 本身。
- 改写也**不许丢限定**：谁说的、什么时候、做什么、方向（买/卖、看好/看空）不许变。

【硬约束】
1. 不许改写候选的 quote，也不许把 A 候选的意思说成 B。text **默认就是候选 claim
   原文**，补 who/type 之外不许新增候选里没有的信息。
2. material 块里**禁止第一人称表态**（我认为/我觉得/我判断…）—— 别人说的话不是我的结论。
3. 每条候选只能用在一条 line 上；用不上的候选可以不出现（不要硬凑）。
4. facts 只放**数字类事实**（金额、比例、日期、数量），正文里出现的数字必须都在 facts 里。
5. hint 是「可能的联系」：只陈述「下面这些素材可能指向同一个问题/张力」，
   **禁止写结论**（不许出现「所以/因此/建议/应该/结论是」），只许引用本页已有的内容。
   没有值得说的就留空串。
6. slug 用 ASCII kebab（小写字母数字与连字符），由页标题转写，例如 ten-year-thinking。

【输出】只输出 JSON，不要解释、不要 markdown 代码块：
{"slug":"…","title":"…","lines":[{"cand_id":9,"section":"judge","type":"judgment","text":"…"},
{"cand_id":10,"section":"material","type":"other_opinion","who":"某作者","text":"…"},
{"cand_id":11,"section":"interest","type":"interest","text":"…"}],
 "facts":["…"], "hint":"…"}
"""


#: S2 的第二条路（阶段 2.3d）：把新候选**并进已有页**。
#: ★ 为什么不许模型重写整页：老正文里有人的润色、有逐条出处的深链。让模型输出
#:   「整页新正文」，它就会安静地少抄两条链接、改掉一句限定 —— 而「少了一条判断」
#:   这种事在页面上看不出来（页还在、还挺长）。所以分工是：**模型只决定新候选
#:   归哪一块、写成什么**，老内容由程序原样搬运。守恒由结构保证，不靠模型自觉。
COMPILE_S2_MERGE_SYSTEM = """你是「谛听」知识库的编辑。这一页**已经入库**，现在有几条新候选要并进去。
你只负责「新候选放在哪一块、写成什么」，**老的正文由程序原样保留**，你不要复述它。

【三块分类，含义绝不同】
- judge（我的判断）：只能放 quote_from=mine 的候选 —— 那是读者自己敲的字。
- material（事实 / 他人的观点）：放 quote_from=highlight 或 article 的候选。
  type 取 fact（客观事实/事件）或 other_opinion（某人发表的观点）；other_opinion 要给 who（谁说的）。
  ★ quote_from=article 是**正文里的事实**，type 必须是 fact，不许放进 judge。
- interest（我的在意点）：只能放 quote_from=highlight、且读者**没有表态**的候选。type 固定 interest。

【先在老页里找一遍，避免同一句话说两遍】
- 老页已经有同义的一条，且新候选**没有**多出信息 → 这条候选不出现在 lines 里（跳过）。
- 新候选更具体（补了数字、时间、对象、方向）→ 也要出现，但 text 只写**补充了什么**，
  不要把老话重复一遍，也不要写成「同上」这种看不懂的短语。
- 判断不了是否同义就照常归类 —— 宁可多一行，也不要漏掉一条新判断。

【text 怎么写：claim 就是底稿】
- 一条候选单独成行时，text **直接用它的 claim**，不要重写。
- 只有合并几条同义候选时才改写，且不许丢主语、时间、动作方向（买/卖、看好/看空）。

【硬约束】
1. 不许改写候选的 quote，也不许把 A 候选的意思说成 B；补 who/type 之外不许新增信息。
2. material 块里禁止第一人称表态（我认为/我觉得/我判断…）。
3. 每条候选只能用在一条 line 上。
4. facts 只放**这次新增**的数字类事实（金额、比例、日期、数量）；老页的 facts 程序会并进来。
5. hint 是「可能的联系」，只陈述「这些素材可能指向同一个问题」，**禁止写结论**
   （不许出现 所以/因此/建议/应该/结论是）。没有值得说的就留空串。
6. **不要**输出 title / slug —— 这是同一页，页名不动。

【输出】只输出 JSON，不要解释、不要 markdown 代码块：
{"lines":[{"cand_id":9,"section":"judge","type":"judgment","text":"…"}],
 "facts":["…"], "hint":"…"}
"""


def _mark_id_for(cand) -> Optional[int]:
    """候选的 quote 出自哪一条划线/批注 —— 用于 `?mk=` 精确深链。

    ★ 三条来源都要认：划线正文（quoted_text）、划线备注（note）、批注正文（body）。
      认不出来就返回 None（落到来源层级），**不猜**：猜错等于把人带到别的位置，
      比不跳更糟（他会以为那条划线被删了）。
    """
    st, sid, q = cand.source_type, cand.source_id, (cand.quote or "").strip()
    if not q:
        return None
    try:
        # ★ 只认还活着的痕迹：软删的那条页面上根本不存在，深链跳过去只会
        #   落空（而且不报错），不如退回来源层级。
        h = (Highlight.objects.live().filter(source_type=st, source_id=sid, quoted_text=q).first()
             or Highlight.objects.live().filter(source_type=st, source_id=sid, note=q).first())
        if h is not None:
            return h.id
        a = (Annotation.objects.live()
             .filter(source_type=st, source_id=sid, body=q).first())
        if a is not None:
            return a.id
    except Exception:      # noqa: BLE001 —— 深链定位失败不该挡住编译
        log.debug("候选定位失败 %s/%s", st, sid, exc_info=True)
    return None


def _label_for(cand) -> str:
    """链接前面的名字（不是裸「来源」）。★ 素材长什么样、模型就抄成什么样：
    周报第一版把标签写成 `[来源](url)`，成品里就出现一堆裸 `[来源]`（踩过两次）。"""
    st, sid = cand.source_type, cand.source_id
    if st == "report":
        head = "日报" if sid.startswith("daily") else "周报"
        day = sid.split("-", 1)[1] if "-" in sid else sid
        return f"{head} · {day[5:]}" if len(day) >= 10 else f"{head} · {day}"
    if st == "chat":
        return "对话"
    name = title_of(st, sid)
    return f"文章《{name[:16]}》" if name else "文章"


def _cand_line(cand, link: str) -> str:
    """一条候选用在人能读的正文里的出处串。"""
    return f"[{_label_for(cand)}]({link})"


#: S1 材料里最多列多少页「已有页」。几百页时光页清单就把 S1 的预算吃光
#: （每页约 60 字）；截断时**必须告诉模型「这不是全部」**，否则它会以为没有那一页，
#: 于是再造一页 —— 这正是本功能要消灭的重复。
COMPILE_MAX_PAGES = 80

#: 目录卡里每页最多列几条候选摘要（第七步：S1 靠它判断「这一页混没混别的问题」）。
#: ★ 这是 S1 的**固定开销倍增器**：页卡从约 60 字涨到「60 + 8×约 45 字」≈ 420 字，
#:   80 页 ~30KB 输入。没有这一格，S1 只能按标题猜要不要拆；有这一格，它才看得见
#:   「这页里混着另一个问题」。所以是「有上限的一格」，不是「把候选都摊给 S1」——
#:   摊开就回到「21 条一次判、推理吃光预算」那一档。
#: ★ 截断必须说出口（卡上的 `cands_more`）：只列 8 条而页里有 20 条却不说，模型会
#:   以为那 8 条就是全部，于是判定「没有该拆的」—— 静默漏掉正是本功能要防的事。
COMPILE_PAGE_CANDS = 8

#: 标签池上限：S1 只看得见库里最常用的这么多个标签。
#: ★ 为什么要有上限：全库标签随页数线性膨胀（2026-11-16 实测真实库 32 页已长出
#:   约 96 个词，其中 93 个只出现过一次），而这一整块在 prompt 里只有「让模型
#:   挑一个现成的词」这一个用途 —— 看 200 个词并不比看 40 个更容易复用，只是更贵。
#:   按使用次数降序取前 N，**顺序本身就是「哪些词常用」的信号**（不给次数）。
COMPILE_TAG_POOL = 40


def _tag_pool(pages, limit=None) -> list:
    """库里**已在用**的标签词表，按使用次数降序（次数相同按词升序）。

    ★ 这是「标签收敛」的唯一抓手（2026-11-16 用户拍板）：真实库 32 页长出 ~96 个
      标签、93 个只出现一次 —— 模型每页自由造词，词表永远不收敛，faceted 筛选
      也就永远筛不出东西（每个标签只对应一页）。让 S1 先看见现成的词，才有复用。
    ★ 只给词、不给次数：次数会把这段变成统计表，而「靠前的更常用」这个顺序信息
      已经够用；给次数还会诱使模型为了「显得正确」硬套高频词。
    ★ 排序必须**稳定**（次数降序 → 词升序）：同一份库两次运行要得到同一份池，
      否则 prompt 不可复现，判据也钉不住。
    ★ 入参是 `wstore.pages()` 的字典（已带 `tags`）；不是模型对象。
    ★ 计数与排序收在 `wstore.tag_counts()` 一处 —— 审批页「输入时提示已有标签」
      读的是同一份词表，两边各数一遍迟早分叉（S1 在往 A 收敛、人在往 B 收敛）。
    """
    n = COMPILE_TAG_POOL if limit is None else int(limit)
    if n <= 0 or not pages:
        # ★ `not pages` 不是「没传参 = 去读库」，是「这次没有可用页 ⇒ 池子为空」。
        #   把 None 交给 `tag_counts()` 会去扫真实库（那边 None = 现读），
        #   症状是空库跑出 40 个真实标签 —— 2026-11-16 全量单测抓到的正是这条。
        return []
    return [v["tag"] for v in wstore.tag_counts(pages, limit=n)]


def _claim_map(ids) -> dict:
    """`{cand_id: claim}`，只含**未丢弃**的候选。

    ★ 「未丢弃」的判据不在这里写：用 `INGEST_LIVE_STATUS`，与 `compile_backlog` /
      `wiki_agent.live_candidates` 同一份。各写一遍的症状是「目录卡里还看得见一条
      早就丢弃的候选，模型建议拆它，拆出来正文却写不动」。
    """
    want = set()
    for i in ids or ():
        try:
            want.add(int(str(i).strip()))
        except (TypeError, ValueError):
            continue        # 脏值跳过，不猜（口径同 `wiki._cand_ids`）
    if not want:
        return {}
    return {c.id: (c.claim or "") for c in IngestCandidate.objects.filter(
        id__in=sorted(want), status__in=INGEST_LIVE_STATUS)}


def _cand_rows(cands) -> list:
    rows = []
    for c in cands:
        rows.append({
            "cand_id": c.id,
            "claim": c.claim,
            "quote": (c.quote or "")[:300],
            "quote_from": c.quote_from,
            "domain": c.domain or "",
            "confidence": c.confidence or "",
            "source": f"{c.source_type}:{c.source_id}",
            # ★ 出处标题也要进材料（2026-11-03 修）：正文里的出处标签是程序拼的
            #   （`文章《…#805.彼得·蒂尔…》`），标题不在材料里 ⇒ 数字守卫把程序自己
            #   拼的期号当成「模型编的数字」，当场拒掉 4 组。上一轮我把这归因为
            #   「模型自己写进正文的数字」，是错的：`number_audit` 早就剥掉了 URL，
            #   剩下的 `#805` 在标签文本里。模型看不见标题，就不该为标题负责。
            "source_title": title_of(c.source_type, c.source_id)[:60],
        })
    return rows


def _payload(cands) -> str:
    """S1/S2 的材料：候选清单（JSON）。键名就是说明书，模型照着填，不解释。"""
    return json.dumps({"candidates": _cand_rows(cands)}, ensure_ascii=False, indent=1)


def _page_catalog(pages, claim_of=None) -> list:
    """已有页的「目录卡」：S1 靠它判断该新建、并进哪一页、还是从哪一页拆。

    ★ 只给 slug / title / domain / 一句话 / **候选摘要**。**不给全文**：S1 的活是
      分组不是改写，喂全文只会让它照着已有页复述，还把预算花在无关页上。
    ★ 按 updated 倒序：最近动过的页最可能需要补充。
    ★ 摘要要剥掉出处串（`[文章《…》](url)`）：那是「这条判断哪来的」，不是
      「这页在回答什么问题」；S1 拿它做判断，出处只会制造相似度噪音。
    ★ `candidates`（第七步）：每页 `{id, claim[:40]}`，**不给 quote**。理由与摘要
      同源 —— 分组/拆页问的是「这几条在回答同一件事吗」，出处证据在那一步只是噪音；
      全明细留给 S1.5 在真正要拆的那一页上花（渐进式披露）。
      只列 `COMPILE_PAGE_CANDS` 条，截断时卡上带 `cands_more` 说清还有几条没列。
    ★ 已丢弃的候选不进卡：它拆不进任何页（正文写不动），列表里留着只会诱使模型
      提一个注定失败的拆页方案。
    """
    pages = list(pages or [])
    if claim_of is None:
        claim_of = _claim_map([i for p in pages for i in (p.get("candidates") or [])])
    rows = []
    for p in sorted(pages, key=lambda x: (x.get("updated") or "", x.get("slug") or ""),
                    reverse=True):
        # ★ `_first_sentence` 已经把方括号洗掉了，所以出处串在摘要里长这样：
        #   `…是投资最难处 文章《转摩根朋友的观点》(/?sel=feed:3&open=6810…)`
        #   两段都要清：留着 URL，S1 会以为两个页「都引了同一篇文章」而更像；
        #   留着「文章《…》」则每页都长得一样，等于没有摘要。
        s = p.get("summary") or ""
        s = re.sub(r"\((?:https?://|/)[^)]*\)", "", s)
        s = re.split(r"文章《|日报 ·|周报 ·|日报·|周报·", s)[0]
        s = re.sub(r"\s+", " ", s).strip(" ·，。")
        if not s or s.startswith("（"):
            s = p.get("title") or ""
        ids = [int(i) for i in (p.get("candidates") or [])
               if str(i).strip().isdigit() and int(i) in claim_of]
        card = {
            "slug": p.get("slug") or "",
            "title": (p.get("title") or "")[:40],
            "domain": p.get("domain") or "",
            "summary": s[:80],
            # ★ 待审批草稿也是合法的并入目标（2026-11-04）：新候选并进草稿，
            #   审批一次就进库。不标出来的话 S1 分不清「页」和「草稿」。
            "draft": bool(p.get("draft")),
            "candidates": [{"id": i, "claim": (claim_of.get(i) or "")[:40]}
                           for i in ids[:COMPILE_PAGE_CANDS]],
        }
        if len(ids) > COMPILE_PAGE_CANDS:
            card["cands_more"] = len(ids) - COMPILE_PAGE_CANDS
        rows.append(card)
    return rows


def _plan_payload(cands, catalog, total_pages: int, prior=None,
                  tag_pool=None) -> str:
    """S1 的材料：候选 + 已有页目录 + 一条「清单是否完整」的交代。

    ★ `pages_total` 大于 `len(pages)` 时必须让模型看见：否则它会把「清单里没有」
      当成「库里没有」，于是再造一页 —— 而截断本来只是省钱的手段。
    ★ `prior`（2026-11-04）：本轮前面批次已经定下的组。不给它的话，分批就是
      「各判各的」，跨批的同一概念只能靠同名撞上 —— 用户明确要求分批时
      不同批之间要能互相合并，一次审批就好。
    ★ `tag_pool`（2026-11-16）：库里已在用的标签词。**空池时不写这个键**（而不是
      写 `"tag_pool": []`）：prompt 里说「没有这个字段 = 库里还没有标签」，
      而空数组会让模型理解成「库里有标签，但一个都没给我看」，于是照样自由造词。
      「空」与「没有」在这里是两个不同的事实，不能合并成一个假值。
    """
    obj = {"candidates": _cand_rows(cands),
           "pages": catalog,
           "pages_total": total_pages,
           "pages_listed": len(catalog)}
    if tag_pool:
        obj["tag_pool"] = list(tag_pool)
    if prior:
        obj["pending_groups"] = [
            {"gid": g["gid"], "title": g["title"], "domain": g["domain"],
             "claims": g.get("claims") or []}
            for g in prior if g.get("gid")]
    return json.dumps(obj, ensure_ascii=False, indent=1)


def _parse_target(raw) -> tuple:
    """S1 给的 target → (slug 或 "")。空串表示「新建」。

    ★ 只认 `existing:<slug>` 与 `new` 两种写法；写成 `existing` 却没说哪一页、
      或写成别的什么，一律当新建 —— 但会记一条 note 让人看见，不静默。
    """
    s = str(raw or "").strip()
    if s.lower().startswith("existing"):
        rest = s.split(":", 1)[1].strip() if ":" in s else ""
        return rest
    return ""


def _parse_split(raw) -> str:
    """S1 给的 `split` → 要从哪一页拆（空串 = 不拆）。

    ★ 为什么不复用 `_parse_target`：target 是 `"existing:<slug>" / "new"` 的**字符串
      协议**，它回答「并入哪一页」；split 是独立字段，只承载「从哪一页拆」。塞进同
      一个解析器，两边都会长出对方的分支，而「拆」被误读成「并」的代价是一整页知识
      被覆盖（比多一页贵得多）。
    ★ 宽容接受裸 slug（模型常把 `split:xxx` 写成 `xxx`）：格式的严格性不换安全，
      安全性靠 `_check_split` 那条「必须在已有页清单里」的校验。
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    if s.lower().startswith("split"):
        s = s.split(":", 1)[1].strip() if ":" in s else ""
    return s


def _check_split(slug: str, by_slug: dict, g: dict) -> tuple:
    """拆页目标能不能用 → (生效的 slug 或 ""，给人看的 note)。

    ★ 三道校验，每一种拒绝的**修法都不同**，所以话要分开写（同 `_resolve_target`
      的纪律）：清单里没有 → 该补页目录；是草稿 → 该先审那份草稿；跨域 → 该拆组。
    ★ 跨域这条与 `_resolve_target` 是同一条铁律的镜像：并入不许跨域，拆出来再和
      本组候选合成一页同样不许 —— 只不过后者的症状是「新页里混着两个域的判断」，
      比并入更难发现（新页本来就是新文件，没人会去对照它的来路）。
    """
    entry = by_slug.get(slug)
    if entry is None:
        return "", f"想拆 {slug}，但已有页清单里没有它 —— 按新建处理"
    if entry.get("draft"):
        return "", f"想拆 {slug}，但那是待审批草稿 —— 先处理它，这组按新建处理"
    if entry.get("domain") != g["domain"]:
        return "", (f"想拆 {slug}，但它属于 {entry.get('domain')}、这组是 {g['domain']} —— "
                    "不跨域，按新建处理")
    return slug, ""


def _batch_cands(cands, size=None) -> list:
    """把候选按来源切成若干批。

    ★ `size=None` 时才读模块常量（不是写进默认参数）：默认参数在函数定义时就求值，
      测试改不动它 —— 那样「批大小」这条判据就没法用一条小例子的反证来验，
      只能真花 21 条的钱去跑。
    ★ 为什么按来源聚：批内合并是分组唯一的收益，而同一篇文章 / 同一份日报的候选
      通常落 1–2 页。把它们切到两批，两批互相看不见 → 同一个概念被建成两页，
      正是拆批最容易引入的退化。
    ★ 单个来源本身就超过 size 时不再迁就（宁可切散）：硬把 20 条留在同一批等于
      没拆，推理量又回到「吃光 8192」那一档。
    ★ 顺序来自 `compile_backlog`（source_type, source_id, id），同源本来就相邻；
      这里只是把「相邻」变成「同批」。
    """
    size = size or COMPILE_S1_BATCH
    runs = []
    for c in cands:
        key = (c.source_type, c.source_id)
        if runs and runs[-1][0] == key:
            runs[-1][1].append(c)
        else:
            runs.append((key, [c]))
    out, cur = [], []
    for _key, items in runs:
        if len(items) > size:
            if cur:
                out.append(cur)
                cur = []
            for i in range(0, len(items), size):
                out.append(items[i:i + size])
            continue
        if cur and len(cur) + len(items) > size:
            out.append(cur)
            cur = []
        cur = cur + items
    if cur:
        out.append(cur)
    return out


def _group_list(text):
    """从模型输出里挖出 groups 数组；挖不到返回 None。"""
    for obj in jsontool.iter_json(text):
        if isinstance(obj, dict) and isinstance(obj.get("groups"), list):
            return obj["groups"]
    return None


def _clean_groups(raw, ids, used) -> list:
    """一批 S1 输出 → 通过**数据校验**的组（cand_id 真实 / 不重复用 / domain 在册）。

    ★ 这里只管数据合法性；`target`（新建还是并入哪页）由 `_resolve_target` 一处判
      —— 同批去重和跨批合并都要用它，判据抄两份必然漂（本项目为此付过三次学费）。
    """
    prompt_domains = domains_for_prompt()
    out = []
    for g in raw:
        if not isinstance(g, dict):
            continue
        title = str(g.get("title") or "").strip()
        domain = str(g.get("domain") or "").strip().lower()
        got = [i for i in (g.get("cand_ids") or [])
               if isinstance(i, int) and i in ids]
        got = [i for i in got if i not in used]          # 一条只能用一次
        if not title or not got or domain not in prompt_domains:
            continue
        used.update(got)
        # ★ `target` 先原样带着模型的写法（"existing:xxx" / "new" / 空），由
        #   `_resolve_target` 解析成**生效的 slug** 并覆盖。重建 dict 时把它丢掉，
        #   等于所有并入判定都静默变成「新建」—— 本轮就是这么被测试抓到的：
        #   note 也是空的，连「按新建处理」的理由都没有。
        out.append({"title": title[:40], "domain": domain, "cand_ids": got,
                    "target": str(g.get("target") or ""), "note": "",
                    # ★ 第十三步：S2 退役后，slug / tags 由 S1 顺带给。
                    #   `slug` 只作文件名（后面还要过 `_unique_slug` 防撞名）；
                    #   `tags` 是**建议**，只在页还没有标签时写入（§3.4 不自动采信）。
                    #   模型给了非列表的 tags 就当没建议 —— 不把脏值写进 frontmatter。
                    "slug": str(g.get("slug") or "").strip()[:60],
                    "tags": [str(t).strip()[:20] for t in (g.get("tags") or [])
                             if isinstance(g.get("tags"), list) and str(t).strip()][:4],
                    # ★ `join`（2026-11-04）：并进本轮前面批次已定下的组。合法性
                    #   （gid 存在 / domain 一致）在 `plan_groups` 里判 —— 这里
                    #   只负责把它**原样带上**，跟 target 同一条教训。
                    "join": g.get("join"),
                    # ★ `split`（2026-11-06 第七步）：拆哪一页。这里同样只负责**原样
                    #   带上** —— 重建 dict 时丢掉它，所有拆页判定会静默变成「新建」，
                    #   而页面上的表现只是「多了一页」，没有任何报错（target 踩过）。
                    "split": str(g.get("split") or ""),
                    "reason": str(g.get("reason") or "")[:120]})
    return out


def _resolve_target(g, by_slug, batch_targets) -> tuple:
    """一组的 `target` → (真正生效的 slug 或 ""，给人看的 note)。

    ★ 三种拒绝理由分别写清（目标不存在 / 跨域 / 同批两组装同一页）：这是模型最常
      犯的三种错，而且**修法完全不同** —— 混成一句「target 无效」，人就不知道该
      去补页目录、还是该拆组。
    """
    want = _parse_target(g.get("target"))
    note = ""
    if want and want not in by_slug:
        note = f"想并入 {want}，但已有页清单里没有它 —— 按新建处理"
    elif want and by_slug[want]["domain"] != g["domain"]:
        note = (f"想并入 {want}，但它属于 {by_slug[want]['domain']}、这组是 {g['domain']} —— "
                "不跨域，按新建处理")
    elif want and want in batch_targets:
        note = f"两组装成了同一页 {want}，这一组按新建处理（否则后一份会盖掉前一份）"
    return ("" if note else want), note


def _norm_title(t: str) -> str:
    """标题归一化：只用来判「两批是不是在说同一个概念」。"""
    return re.sub(r"[\s\W_]+", "", (t or "").lower())


def _focus_split(g, slug, by_slug, cands_by_id, live) -> tuple:
    """S1.5：只看 <slug> 这一页，决定拆哪几条、新页叫什么。返回 (plan, why)。

    plan = {from, take, title, domain, page_total}；为 None 时 `why` 说明为什么不拆
    （调用方按新建处理，并且**把 why 说出来**）。

    ★ 材料只给这一页 + 这一组：渐进式披露的第二层，贵钱只花在被点名的那一页上。
    ★ `take` 的合法性由程序判（⊆ 该页 candidates），不看模型面子：模型给了一个不在
      这一页的 id，照收的后果是那条候选**既不在原页也不在新页** —— 页面都在、都挺
      长，就是少了一条判断，没人会发现。
    ★ `take` 里只留**还活着**的候选：已丢弃的候选拆不进新页（写不出正文），收下它
      等于让 `_compose_page` 在后面炸掉整组。
    """
    path, why = wstore.find_page(slug)
    if path is None:
        return None, why.replace("合并目标", "拆页目标")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"读不到 {slug} 这一页（{exc}）—— 按新建处理"
    fm, _body = wstore.parse_front(text)
    page_ids = wstore._cand_ids(fm)
    page_cands = [live[i] for i in page_ids if i in live]
    if not page_cands:
        return None, f"{slug} 页里没有还能用的候选（都已丢弃）—— 按新建处理"

    entry = by_slug.get(slug) or {}
    new_cands = [cands_by_id[i] for i in (g["cand_ids"] or []) if i in cands_by_id]
    material = json.dumps({
        "page": {"slug": slug, "title": fm.get("title") or slug,
                 "domain": entry.get("domain") or "",
                 "summary": entry.get("summary") or "",
                 "candidates": _cand_rows(page_cands)},
        "group": {"title": g["title"], "domain": g["domain"],
                  "candidates": _cand_rows(new_cands)},
        "domains": list(domains_for_prompt()),
    }, ensure_ascii=False, indent=1)

    reply, err = citeguard._call(
        "wiki_compile", COMPILE_S1_SPLIT_SYSTEM, material,
        citeguard.fresh_stats(), max_calls=1,
        max_tokens=COMPILE_MAX_TOKENS, temperature=0.1,
        pool=cr.POOL_WIKI, tag="wiki 编译拆页", label=f"拆 {slug}")
    if err or not reply:
        return None, f"拆页判断失败（{err or '模型没有返回结果'}）—— 按新建处理"
    obj = None
    for o in jsontool.iter_json(reply):
        if isinstance(o, dict) and isinstance(o.get("take"), list):
            obj = o
            break
    if obj is None:
        return None, "拆页判断的输出不是约定的 JSON（没有 take）—— 按新建处理"

    ok_ids = {c.id for c in page_cands}
    raw_take = [i for i in obj["take"] if isinstance(i, int)]
    take = list(dict.fromkeys(i for i in raw_take if i in ok_ids))
    bad = [i for i in raw_take if i not in ok_ids]
    why_note = ""
    if bad:
        why_note = ("拆页判断给了不在 %s 页的候选 %s，已忽略"
                    % (slug, "、".join(str(i) for i in bad)))
    if not take:
        return None, ((why_note + "；") if why_note else "") + \
            f"{slug} 这一页没有该拆走的候选 —— 按新建处理"

    title = str(obj.get("title") or "").strip()[:40] or g["title"]
    dom = str(obj.get("domain") or "").strip().lower()
    if dom and dom not in domains_for_prompt():
        why_note = ((why_note + "；") if why_note else "") + \
            f"模型建议的 domain「{dom}」不在册，已按 {g['domain']} 处理"
        dom = ""
    plan = {"from": slug, "take": take, "title": title,
            "domain": dom or entry.get("domain") or g["domain"],
            "page_total": len(page_ids)}
    return plan, why_note


def _plan_splits(groups, by_slug, cands_by_id) -> list:
    """收尾：给每个带 `split` 的组做一次 S1.5 聚焦，把结果写进 `split_plan`。

    ★ 放在「分组 + 跨批合并」**之后**：先把组定下来再花钱看页，否则后来 join 进来
      的候选会让同一页被问两遍。
    ★ 同一页一轮只拆一次：`.drafts/<slug>.md` 只有一份，第二次 `pull_page` 必被拒。
      所以这里先判重，第二组**降级为新建并说出来** —— 而不是让它在写盘那一步失败。
    """
    notes = []
    todo = [g for g in groups if str(g.get("split") or "").strip()]
    if not todo:
        return notes
    # ★ 延迟取：没有拆页时不多查一次库（每次编译都会走 `plan_groups`）。
    from core import wiki_agent as wagent
    live = wagent.live_candidates()
    done = set()
    for g in todo:
        slug = g["split"]
        if slug in done:
            g["split"] = ""
            notes.append("「%s」也要拆 %s，但同一轮一页只能出一份替换稿 —— 这组按新建处理"
                         % (g["title"], slug))
            continue
        plan, why = _focus_split(g, slug, by_slug, cands_by_id, live)
        if plan is None:
            g["split"] = ""
            notes.append("「%s」%s" % (g["title"], why))
            continue
        done.add(slug)
        g["split_plan"] = plan
        if why:
            notes.append(why)
        notes.append("「%s」将拆走 %s 的候选 %s，与新候选一起组成「%s」"
                     % (g["title"], slug, "、".join(str(i) for i in plan["take"]),
                        plan["title"]))
        if len(plan["take"]) >= plan["page_total"]:
            # ★ 纯拆分（把整页搬空）= 删页（第八步落地后）：那份替换稿的
            #   `candidates` 是空的，而「edits + 空候选」正是删除请求的形态 ——
            #   提交时那一页会被移除、候选一并回池废弃。所以这里要**明说**
            #   「这一轮等于在提一个删除」，否则人看到「1 页草稿」会以为是新页。
            notes.append("注意：%s 的候选被全部拆走，这份替换稿等于一个**删除请求**"
                         "（提交后该页移除、候选回池废弃）；想保留这页就给它补候选，"
                         "或退稿" % slug)
    return notes


def plan_groups(cands, pages=None) -> tuple:
    """S1：把候选分成概念组。返回 (groups, err, notes)。

    ★ 组是「一页草稿」，所以这一步的错法最贵：分太粗会让一页装两个判断（违反
      原子概念），分太细会让读者看到一堆半页。校验放在程序里，不靠模型自觉：
      cand_id 必须是真实的、每条只能用一次、domain 必须在白名单。

    ★ 阶段 2.3d：S1 现在能看到已有页，并决定「新建」还是「并入 <slug>」。
      没有这一眼，它会为同一个概念反复造新页（实测：`llm-moat-validator` 与
      `specialized-ai-validator` 同源同判断，被建成两页）。合并的钱要花得值：
      并入的校验（目标存在 / 同域 / 只许一组用）也在程序里，不看模型面子。

    ★ 拆批（2026-11-03，见 `COMPILE_S1_BATCH`）：原来的「全部候选 + 页目录一次判」
      在 21 条时稳定失败 —— 推理链把 4096、翻倍后的 8192 全吃光，正文 0 字，
      ¥0.057 一条产物都没有。三条配套判据缺一不可：
      - **同一来源尽量同批**（`_batch_cands`），否则同一个概念被两批各建一页；
      - **跨批合并**：两批各自判「并入同一页」或各自新建同名页时，合成一组。
        不合并就会出现「一份并入草稿 + 一页同名新建」，人看到的是「怎么多了一页」；
      - **一批失败不许让整轮归零**：其余批照常编，失败那批的条数与 id 写进 notes。
        它们没被标成已编译，下次点编译会再试。原先最气人的正是这里：21 条全有、
        一条判不动、产出 0 页、钱照花。

    ★ 2026-11-04（用户定方向「分批是不得已，最好不分批」）：批大小 8→16
      （2026-11-09 据实测调回 8，见 `COMPILE_S1_BATCH` 注释）；后面
      批次经 `pending_groups` / `join` 看得见前面批次已定下的组 —— 分批不再各判
      各的，跨批的同一概念合成一组，一次审批就好。待审批草稿也进页目录（由
      `_compile_locked` 合成后传入），并入它审批一次即可。

    ★★ 2026-11-09（撞预算的处理换了一半）：原先「推理吃光预算 → token 翻倍重试」，
      在**分组这一步是白花钱**。实测两次（2026-11-03 的 21 条、2026-11-09 的第 4/4
      批 16 条）：4096 撞顶、翻倍到 8192 **照样撞顶**，两笔开销换来同一批候选
      一条没进去。原因是输出量随**候选条数**涨，不随 max_tokens 涨。
      ⇒ 分组改成：撞预算就**对半拆批重跑**（任务变小，输出自然变少），拆到 1 条
      还撞才认输；只有拆不动的单条批才用翻倍重试。批队列因此可增长，`max_calls`
      随队列走（总调用数上界 = 初始批数的两倍左右）。
    """
    ids = {c.id for c in cands}
    full_catalog = _page_catalog(pages)
    catalog = full_catalog[:COMPILE_MAX_PAGES]
    by_slug = {p["slug"]: p for p in catalog}
    batches = _batch_cands(cands)
    # ★ 所有批共用一个 stats：调用计数、token 累计要跨批连续，否则「这批花了多少」
    #   每题都对不上（`max_calls` 也必须是批数，不然第二批直接被「达到单次调用上限」拒）。
    stats = citeguard.fresh_stats()

    groups, used = [], set()
    by_target = {}        # 跨批：target → 已收下的那一组
    new_by_title = {}     # 跨批：新建组的 (domain, 归一化标题) → 已收下的那一组
    formed = []           # 本轮已定下的组（后面批次经 `join` 并进来 —— 分批不再各判各的）
    claims = {c.id: (c.claim or "")[:60] for c in cands}
    notes, failed, missed = [], [], []
    # ★ 标签池用**全量** pages，不是 `catalog`（它只列最近 COMPILE_MAX_PAGES 页）：
    #   目的是「全库用词一致」，只统计最近 80 页的标签等于让老页的词汇被无视。
    tag_pool = _tag_pool(pages)

    # ★★ 批队列可增长（2026-11-09）：撞预算不再「把 token 翻倍重打一遍」。
    #   实测（2026-11-09 清零重来第一次真跑，LlmUsage 逐行）：
    #     第 4/4 批 16 条 → 4096 撞顶（out=4096、正文空、29.7 s、¥0.041）
    #       → 翻倍 8192 重打 → **8192 也撞顶**（out=8192、57.1 s、¥0.070）
    #       → 那 16 条候选最后还是没进去。
    #     同一轮还有另一形态：i=3835 的那批 out=4096 **有半截正文**（JSON 被切断，
    #       连 err 都没有）—— 它当时被当成「模型输出不合格」记了一笔失败。
    #   两次 4096 的调用 + 一次 8192 共烧 ≈¥0.15 / 119 s，换回 0 组。
    #   同一形态 2026-11-03 也踩过（21 条时 4096 + 8192 全吃光）。
    #   ⇒ 结论：**输出量随候选条数涨，不随 max_tokens 涨**，翻倍只是在同一个过大
    #   的问题上多花一倍钱。改为：撞预算就把这一批**对半拆开重跑**（任务变小，
    #   输出自然变少），拆到 1 条还撞才认输。单条批无法再拆，此时才允许它沿用
    #   「翻倍重试」。
    queue = list(batches)
    bi = 0
    while bi < len(queue):
        batch = queue[bi]
        bi += 1
        total = len(queue)
        head = f"第 {bi}/{total} 批（{len(batch)} 条）"
        # ★ 每次调用前记一次截断计数：stats 是**跨批共用**的（调用数/token 要连续），
        #   只能比差值才知道「这一批被截断了没有」。
        cut_before = len(stats.setdefault("truncated", []))
        text, err = citeguard._call(
            "wiki_compile", COMPILE_S1_SYSTEM,
            _plan_payload(batch, catalog, len(full_catalog), formed, tag_pool),
            stats, max_calls=len(queue) + 2,
            max_tokens=COMPILE_MAX_TOKENS, temperature=0.1,
            pool=cr.POOL_WIKI, tag="wiki 编译分组",
            # ★ 多条的批不走「token 翻倍重试」（理由见上面 queue 那段实测）：
            #   拆批才是对的杠杆。单条批拆不动，留着重试自愈。
            retry_on_empty=len(batch) <= 1,
            # ★ label 必须带「第几批」：日志只写「21 条候选」时，失败的是哪 8 条、
            #   是不是同一个原因，全看不出来（上一轮为此多查了半小时）。
            label=f"第 {bi}/{total} 批 · {len(batch)} 条候选")
        cut = len(stats["truncated"]) > cut_before
        raw = _group_list(text) if (text and not err) else None
        # ★★ 两种「这批对单次调用太大了」的形态，处置**必须相同**（对半拆批）：
        #   (a) 推理把预算吃光 → 正文为空、err 带「推理占满」；
        #   (b) 推理 + 正文一起吃满 → 有正文但 JSON 是半截，`stats` 记了 truncated。
        #   (b) 极易漏：它 err 为空、只有 JSON 解析不出来，若只判 (a) 就会被当成
        #   「模型输出不合格」记一笔失败 —— 实测 2026-11-09 那次 4395（i=3835 /
        #   out=4096）正是这一形态，整批 8 条候选一条没进去。
        too_big = bool(err and "推理占满" in err) or (cut and raw is None)
        if too_big and len(batch) > 1:
            mid = len(batch) // 2
            why = "推理吃光预算" if (err and "推理占满" in err) else "输出被截断，JSON 半截"
            queue[bi:bi] = [batch[:mid], batch[mid:]]
            log.warning("%s 撞满 %d tokens 预算（%s），已对半拆成 %d+%d 条重跑",
                        head, COMPILE_MAX_TOKENS, why, mid, len(batch) - mid)
            notes.append("%s 撞满 %d tokens 预算（%s），对半拆成 %d+%d 条重跑"
                         % (head, COMPILE_MAX_TOKENS, why, mid, len(batch) - mid))
            continue
        if err or not text:
            failed.append(f"{head}：{err or '模型没有返回分组结果'}")
            missed.extend(c.id for c in batch)
            continue
        if raw is None:
            # ★ 「输出不合格」要跟「确实没分组」分开：前者是真故障，后者不可能发生
            #   （有候选就该有组）。混起来会静默返回「0 页草稿」而人以为成功了。
            failed.append(f"{head}：模型输出不是约定的 JSON（没找到 groups）"
                          + ("，且被截断过（单条批拆不动）" if cut else ""))
            missed.extend(c.id for c in batch)
            continue

        batch_ids = {c.id for c in batch}
        batch_targets = set()
        for g in _clean_groups(raw, batch_ids, used):
            # ---- join：并进前面批次已定下的组（2026-11-04）
            jid = g.get("join")
            prev_g = (formed[jid - 1]
                      if isinstance(jid, int) and 1 <= jid <= len(formed) else None)
            if prev_g is not None:
                if prev_g["domain"] == g["domain"]:
                    prev_g["cand_ids"].extend(g["cand_ids"])
                    prev_g["claims"].extend(
                        claims[i] for i in g["cand_ids"] if i in claims)
                    notes.append("「%s」并进了前面批次已定的组「%s」"
                                 % (g["title"], prev_g["title"]))
                    continue
                notes.append("「%s」想 join=%s，但那是 %s 域的组、这组是 %s 域"
                             " —— 按常规分组处理"
                             % (g["title"], jid, prev_g["domain"], g["domain"]))
            elif jid is not None:
                notes.append("「%s」想 join=%s，但前面批次没有这个组 —— "
                             "按常规分组处理" % (g["title"], jid))

            want, note = _resolve_target(g, by_slug, batch_targets)
            # ---- split：这组要从某一页拆候选重组（第七步）
            #   ★ 合法性的三道校验在 `_check_split` 一处，S1.5 的聚焦调用放到收尾
            #     （组定下来之后再花钱看页，见 `_plan_splits`）。
            #   ★ 拆与并互斥：模型同时给了 target 和 split 时按拆处理 —— 拆是更保守
            #     的那一头（原页不动、产出替换稿等人批），并把被丢掉的那个说出来。
            split_slug = _parse_split(g.get("split"))
            if split_slug:
                if want:
                    notes.append("「%s」同时给了并入 %s 和拆 %s，按拆页处理（两者互斥）"
                                 % (g["title"], want, split_slug))
                want, note = "", ""
                split_slug, spl_note = _check_split(split_slug, by_slug, g)
                if spl_note:
                    notes.append("「%s」%s" % (g["title"], spl_note))
            g["split"] = split_slug
            if want and want in by_target:
                prev = by_target[want]
                prev["cand_ids"].extend(g["cand_ids"])
                prev["claims"].extend(
                    claims[i] for i in g["cand_ids"] if i in claims)
                notes.append("「%s」与前面的组都在并 %s，已合成一组" % (g["title"], want))
                continue
            if want:
                batch_targets.add(want)
                by_target[want] = g
            else:
                # ★ 拆页组进 `new_by_title` 时要带上「从哪一页拆」（第七步）：不带的话
                #   一个「新建 T」会跟一个「从 P 拆出 T」合并成一组，被吞掉的那个去向
                #   就此消失，且没有任何提示。
                key = (g["domain"], _norm_title(g["title"]), g["split"])
                prev = new_by_title.get(key)
                if prev is not None:
                    prev["cand_ids"].extend(g["cand_ids"])
                    prev["claims"].extend(
                        claims[i] for i in g["cand_ids"] if i in claims)
                    notes.append("「%s」与前面一批的同名组都是新建，已合成一组"
                                 "（同一概念不该出两页）" % g["title"])
                    continue
                new_by_title[key] = g
            g["target"], g["note"] = want, note
            g["gid"] = len(formed) + 1
            g["claims"] = [claims[i] for i in g["cand_ids"] if i in claims]
            formed.append(g)
            groups.append(g)

    if not groups:
        # ★ 一批都没成功：把每批的原因都带上（同「一页未出要说清每一组」的纪律）
        return [], ("；".join(failed)
                    or "模型给出的分组全部不合法（cand_id / domain 对不上）"), notes

    # ★ 拆页的聚焦判断（S1.5）放在**合并去重之后**：先把组定下来，再花钱看页；
    #   否则后来 join 进来的候选会让同一页被问两遍（那页的明细要再喂一次全价）。
    notes.extend(_plan_splits(groups, by_slug, {c.id: c for c in cands}))
    # ★★ 标签复用/新造的**程序事实**（2026-11-16）：prompt 要求「优先复用现成词」，
    #   但那只是一句提示词承诺 —— 不把它算出来，膨胀永远看不见（真实库已 93/96
    #   个标签只出现一次，而没有任何地方报过这件事）。这里只统计**新页**的建议标签：
    #   并入时 `_draft_from_group` 会保留老页标签，那部分不产生新词。
    if tag_pool:
        proposed = [t for g in groups if not g.get("target")
                    for t in (g.get("tags") or []) if t]
        if proposed:
            pool_set = set(tag_pool)
            reused_n = sum(1 for t in proposed if t in pool_set)
            fresh = [t for t in proposed if t not in pool_set]
            fresh_uniq = list(dict.fromkeys(fresh))
            # ★ 口径必须是「复用条数 + 新造条数 == 建议总数」（用同一个列表计数），
            #   括号里再列去重后的新词 —— 混用「去重后词数」会让两个数加不回去，
            #   读的人第一反应是「统计错了」（本项目已经在别处踩过这种口径漂移）。
            notes.append("标签：本批建议 %d 个，复用池内 %d 个、新造 %d 个%s"
                         % (len(proposed), reused_n, len(fresh),
                            ("（%s）" % "、".join(fresh_uniq)) if fresh_uniq else ""))
    if failed:
        notes.append("有 %d 条这次没编进去（%s）：%s。它们不会被标成已编译，"
                     "下次点编译会再试"
                     % (len(missed), "；".join(failed),
                        "、".join(str(i) for i in missed)))
    return groups, "", notes



def _slugify(text: str, fallback: str) -> str:
    """页标题 → 文件名。★ 只留 ASCII：中文 slug 会让 URL / Obsidian 导出都变麻烦。"""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:60].strip("-")
    return s or fallback


#: 三块的标题 → 键。合并时靠它把老页正文拆回结构里（顺序与渲染器一致）。
_SECTION_HEADS = {"## 我的判断": "judge",
                  "## 事实 / 他人的观点": "material",
                  "## 我的在意点": "interest"}

#: 渲染器自己补的空占位行。合并时要认出来并**丢掉**：留着会把「空块」当成有内容，
#: 下次再并就会在占位后面追加，越堆越怪。
_EMPTY_MARKERS = ("- （无）",)

#: 「可能的联系」块标题。合并时新 hint 追加进这个块，而不是再造一个同名块。
_HINT_HEAD = "## 可能的联系（系统提示，非我的结论）"

_JUDGE_EMPTY = "- （本页暂无可确认的我的判断：这一组都是我划出来的素材）"


def _split_sections(body: str) -> tuple:
    """老页正文 → (三块条目行, 块外原文行)。

    ★ 只认三个已知块的 `- ` 条目行；**块外的内容整块原样保留**（`## 可能的联系`、
      人自己加的补充段）。合并最不能做的事，就是弄丢人自己写的东西。
    ★ 占位行不算内容：它是渲染时补的，留着会让空块看起来像有内容。
    """
    secs = {"judge": [], "material": [], "interest": []}
    others, cur = [], ""
    for ln in (body or "").splitlines():
        head = _SECTION_HEADS.get(ln.strip())
        if head:
            cur = head
            continue
        if ln.lstrip().startswith("## "):
            cur = ""
            others.append(ln)
            continue
        if cur:
            s = ln.strip()
            if not s.startswith("- ") or s in _EMPTY_MARKERS or s == _JUDGE_EMPTY:
                continue
            secs[cur].append(ln.rstrip())
        else:
            others.append(ln)
    while others and not others[-1].strip():
        others.pop()
    return secs, others


def _append_hint(others: list, hint: str) -> list:
    """把新 hint 追加到已有的「可能的联系」块尾；没有这个块就补一个。"""
    if not hint:
        return list(others)
    out = list(others)
    for i, ln in enumerate(out):
        if ln.strip() == _HINT_HEAD:
            out.insert(i + 1, f"- {hint}")
            return out
    while out and not out[-1].strip():
        out.pop()
    return out + ([""] if out else []) + [_HINT_HEAD, f"- {hint}"]


def _render_sections(secs: dict, others=(), judge_placeholder: str = _JUDGE_EMPTY) -> str:
    """三块正文 + 原样保留的其它块。

    ★ 新建与合并**共用这一段**：同一套排版写两遍，迟早有一遍忘了改（这个项目已经
      为「同一判据抄两份」付过三次学费）。
    """
    out = ["## 我的判断"]
    out += secs["judge"] or [judge_placeholder]
    out += ["", "## 事实 / 他人的观点"]
    out += secs["material"] or ["- （无）"]
    out += ["", "## 我的在意点"]
    out += secs["interest"] or ["- （无）"]
    tail = [ln for ln in (others or [])]
    while tail and not tail[-1].strip():
        tail.pop()
    if tail:
        out += [""] + tail
    return "\n".join(out)


def _compose_page(group, cands_by_id, payload_text, old_page=None) -> tuple:
    """一组候选 → 草稿全文（**形态 A：frontmatter + 空正文**）。返回 (text, slug, dropped, err)。

    ★ 第十三步（用户拍板）：正文只留一段 polish 结论，由 `_compile_locked` 收尾
      **统一**调 `wiki_loop._polish_one` 生成；`candidates` 是分组唯一真值，
      精确出处由详情页折叠溯源区读 `IngestCandidate` 按 `quote_from` 分组负责。
    ★★ **有意裁剪，不是遗漏**：S2 的 `COMPILE_SECTIONS` / `_SECTION_ALLOWS` /
      `_FIRST_PERSON` / G1–G5（三块分类、人称、数字、提示守卫）整套退役。
      来源（`quote_from`）在提炼阶段已由 `verify_quotes` 程序锁死、精确对应
      由溯源区保证，用户已明确「正文 polish 可接受轻微问题」。数字守卫搬到
      `wiki_loop._polish_one`，并入守恒改由「候选集合只增不减」
      （`_validate_draft._merges_lost`）+「引用守恒」（`_check_links`）两处守。
      下个会话别把它们当成漏项补回来。
    ★ `tags` 是 S1 的**模型建议**：只在页还没有标签时写入，人手填过的一律保留
      （不自动采信；审批对话可改）。
    ★ 草稿天生带 `stale: true`：结论还没重编。`set_polish` 成功才剥；失败时
      标记留住、审批浮层可见，且 `_validate_draft` 会因「正文为空」拒绝入库。
    ★ `payload_text` 保留参数位以兼容既有调用签名；A 形态下正文不在这里组装，
      S1 才是那份素材的消费者。
    """
    ids = list(group["cand_ids"])
    dropped = [{"cand_id": i, "why": "候选读不到（已丢弃或不在本批）"}
               for i in ids if i not in cands_by_id]
    if dropped:
        return "", "", dropped, ("候选读不到：%s"
                                 % "、".join(str(d["cand_id"]) for d in dropped))
    cands = [cands_by_id[i] for i in ids]
    merging = bool(old_page)
    ofm = (old_page or {}).get("fm") or {}
    old_ids = [int(i) for i in (ofm.get("candidates") or [])] if merging else []

    # ★ noop 改成**程序判据**：这一组的候选老页全都有了，就不必再出一份草稿。
    #   旧实现靠 S2 模型说「同义跳过」，S2 退役后不能留一个只有模型能给的判据。
    if merging and set(ids) <= set(old_ids):
        return "", "", [{"cand_id": None, "why": "这一页已有这些候选，本次没有新增"}], ""

    conf_rank = {"high": 3, "medium": 2, "low": 1}
    new_sources = {f"{c.source_type}:{c.source_id}" for c in cands}
    if merging:
        title = str(ofm.get("title") or old_page.get("title") or group["title"]).strip()[:60]
        slug = old_page["slug"]
        domain = old_page["domain"]
        sources = sorted(set(ofm.get("sources") or []) | new_sources)
        cand_ids = list(dict.fromkeys(old_ids + ids))
        conf = str(ofm.get("confidence") or "").strip().lower()
        if conf not in conf_rank:
            conf = "medium"
        # 置信度取老页原值：加一条新判断不等于整页更可信（取 max 会虚高）
        facts = list(dict.fromkeys(
            str(x).strip() for x in (ofm.get("facts") or []) if str(x).strip()))
        # ★ `merges` 分三种（2026-11-04 起并入对象可以是待审批草稿）：
        #   已入库页 → merges=<slug>；带 merges 的草稿 → 原样保留；普通新页草稿 → 不写。
        if old_page.get("draft"):
            base_merges = str(ofm.get("merges") or "").strip()
            extra_fm = [f"merges: {base_merges}"] if base_merges else []
            # ★ 并入待审批草稿 = 同名覆盖那份稿，它的稳定编号必须跟着走：否则
            #   展示入口的补号器会发一个新号，人看到的 D<n> 凭空跳变（本次要消灭的）。
            did = str(ofm.get(wstore.DRAFT_ID_FM) or "").strip()
            if did:
                extra_fm.append(f"{wstore.DRAFT_ID_FM}: {did}")
        else:
            extra_fm = [f"merges: {slug}"]
    else:
        domain = group["domain"]
        conf = "medium"
        ranked = [c.confidence for c in cands if c.confidence in conf_rank]
        if ranked:
            conf = max(ranked, key=lambda x: conf_rank[x])
        title = str(group.get("title") or "新页").strip()[:60] or "新页"
        slug = _slugify(str(group.get("slug") or ""),
                        f"{domain}-{min(ids) if ids else 0}")
        sources = sorted(new_sources)
        cand_ids = ids
        facts = []
        extra_fm = []

    # ★ tags：模型建议只在页还没有标签时写入；并入保留老标签（人手填过的优先）。
    old_tags = [str(t).strip() for t in (ofm.get("tags") or []) if str(t).strip()]
    suggested = [str(t).strip() for t in (group.get("tags") or []) if str(t).strip()]
    tags = list(dict.fromkeys(old_tags or suggested))[:8]

    fm = ["---", f"title: {title}", f"domain: {domain}",
          f"sources: [{', '.join(sources)}]",
          f"updated: {timezone.localdate().isoformat()}", f"confidence: {conf}",
          f"tags: [{', '.join(tags)}]",
          f"candidates: [{', '.join(str(i) for i in cand_ids)}]"]
    if facts:
        fm.append("facts:")
        fm += [f"  - {f}" for f in facts]
    fm += extra_fm
    fm.append(f"{wstore.STALE_FM}: true")
    fm.append("---")
    # ★ 正文留空，见 docstring。「结论还没生成」是**可见状态**，不塞占位字
    #   （会污染结论，还会让「正文为空」的入库闸门失灵）。
    return "\n".join(fm) + "\n\n", slug, dropped, ""


def _link_for(cand) -> str:
    """候选 → 深链。★ 唯一来源是 `sourceblock.source_url`，不在这拼 URL。

    ★★ 锚点优先读候选自己存的 `mark_id`/`msg_id`（提取阶段就定好了，见
      verify_quotes），**只有老候选（这两列是 NULL）才退回文本反查兜底**。
      反查不可靠：同话说过多次会配错、quote 是子串会误配到别的划线。
    """
    mark_id = cand.mark_id or _mark_id_for(cand)
    msg_id = cand.msg_id
    # ★ 有 msg_id 说明 quote 来自对话里的某条消息：要 ai=1（弹对话浮窗）
    #   才能落得到「我的具体问题」上，否则 msg 参数没人消费（前端只在浮窗里滚）。
    return sourceblock.source_url(cand.source_type, cand.source_id, ai=bool(msg_id),
                                  mark_id=mark_id, msg_id=msg_id, src="wiki") or ""


def compiled_ids_from(drafts, pages) -> set:
    """从**已经读出来的**草稿 / 已入库页里算「已编译」的候选 id。

    ★ 为什么把入参摊开：`apps/wiki/views._ctx` 为了渲染页面本来就读过这两样（实测
      20ms），再调一次 `_compiled_ids()` 等于把同一件事做两遍。更要紧的是 ——
      页面上的 drafts 只展示前 `DRAFT_SHOW` 条、pages 还会被 `?src=` 过滤，
      **拿截过 / 滤过的列表算出来的集合会漏掉被截掉的那批**：那些候选于是从
      「已编译」退回「待编译」，面板上的按钮亮了，点下去整批跳过。所以调用方
      必须传**全量**（见 `_ctx` 里的注释）。
    """
    out = set()
    for d in drafts or []:
        out.update(d.get("candidates") or [])
    for p in pages or []:
        out.update(p.get("candidates") or [])
    return out


def _compiled_ids() -> set:
    """已经被编译成页的候选 id —— **草稿和已入库页两处都扫**。

    ★ 为什么不能只扫 `.drafts/`（2026-11-02 真踩到）：`wstore.approve()` 只是把文件
      `os.replace` 进 `wiki/<domain>/`，候选在库里仍然是 `accepted`，它「已编译」的
      唯一证据就是页 frontmatter 里的 `candidates:`。只扫草稿的话，页一入库这批候选
      就重新变成「没编译过」：再点编译 = 重跑一整轮（真花钱）+ 产出一份与已入库页
      一模一样的草稿，人看到的是「怎么又来一套同名页」。
    ★ 不扫 `.drafts/.discarded/`：归档不是产物。退稿如今是「内容级否定」，
      候选已随退稿在 DB 标 discarded（见 wiki.discard），编译按状态预排除；
      改判回待审即可重编。把归档算成「已编译」会让改判后的候选永远编不出来
      （而且没有任何提示）。
    ★ 不读数据库状态：编译的真相在产物里（产物才决定人看到什么）。候选的 status
      只表达「我认不认这条判断」，不表达「它有没有变成页」——两件事分开记。
    """
    return compiled_ids_from(wstore.list_drafts(), wstore.pages())


_COMPILE_RESULT = "meta/compile.json"


def _compile_result_path():
    return wstore.root() / _COMPILE_RESULT


def write_compile_result(payload: dict) -> None:
    """记下**这一次**编译的结果：成功几页、哪几组被守卫拒、为什么。

    ★ 为什么必须落盘（2026-11-03，用户报「切个页面回来按钮又能点了」）：
      编译结果原先只在 htmx 那一次响应里说一遍，失败组连 `log.md` 都没进
      （那里只记成功的页）。于是刷新后只剩一个亮着的按钮 —— 人既不知道还有
      9 条没编，也不知道它们上次为什么被拒，只能再点一次：同样被拒、同样花钱。
      **失败必须留痕**，这是这个项目最贵的一课（静默的坏结果没人会去查）。
    """
    p = _compile_result_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_compile_result() -> dict:
    """上次编译的结果。没编译过 / 文件坏了都返回 {}（页面据此不显示那一段）。"""
    try:
        got = json.loads(_compile_result_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _record_compile(drafts, made, cands, dropped, skipped, err="",
                    notes=None, noop=None) -> None:
    """把 `_compile_locked` 的产物压成一份结果落盘（形状只在这里定义）。

    ★ `merged` 单独列出来：合并与新建在页面上是两件事（一个覆盖已有页、一个加新页），
      混在一个 `pages` 列表里，人就分不清「这次到底动了哪几页」。
    """
    write_compile_result({
        "at": timezone.localtime().strftime("%Y-%m-%d %H:%M"),
        "pages": list(drafts or []),
        "merged": [{"title": m.get("title") or "", "target": m["target"],
                    "cand_ids": list(m.get("cand_ids") or [])}
                   for m in (made or [])
                   if m.get("target") and not m.get("err") and not m.get("split")],
        # ★ 拆页单独列（第七步）：它既不是「新页」也不是「并入已有页」—— 它是
        #   「原页会少候选、新页会多候选」的一对动作，混进任一边都会让人误判
        #   「这次我的哪一页被动了」。
        "splits": [{"title": m.get("title") or "", "from": m["target"],
                    "cand_ids": list(m.get("cand_ids") or [])}
                   for m in (made or [])
                   if m.get("split") and m.get("target") and not m.get("err")],
        "failed_groups": [{"title": m.get("title") or "",
                           "why": m.get("err") or "",
                           "target": m.get("target") or "",
                           "cand_ids": list(m.get("cand_ids") or [])}
                          for m in (made or []) if m.get("err")],
        "noop": list(noop or []),
        "notes": list(notes or []),
        "cand_ids": [c.id for c in (cands or [])],
        "n_cands": len(cands or []),
        "n_dropped": len(dropped or []),
        "skipped": skipped or 0,
        "err": err or "",
    })


def compile_backlog(done_ids=None, source=None, cand_ids=None) -> dict:
    """还剩多少条**真的会被这次编译吃进去** —— 判据只有这一份。

    返回 `{accepted, todo, batch, skipped, cands}`：
      accepted  **可编译候选总数**（pending + accepted，可被 source / cand_ids 限定）。
               键名沿用历史，但语义已不是「已采纳数」—— 候选免审后 pending
               也直接进编译，见下面的判据。改名会牵动面板和一堆测试，故留名改义。
      todo      其中还没变成页的条数（= 面板该显示的「N 条待编译」）
      batch     这一次点下去会进模型的数量（2026-11-09 起 = `todo` 全量，不截断）
      skipped   之前已编译过的条数（草稿待审批 或 已入库）
      cands     这一次的候选对象（按编译顺序）

    ★ 为什么面板必须问这个函数，而不是自己数：面板写「已采纳 36 条」是 `status`
      口径，而「还能不能编译」是**产物**口径（见 `_compiled_ids` 的说明）。两个口径
      各算一遍，必然漂成「按钮亮着、点下去 0 页」—— 点一次是空的，人下次就不点了；
      等真有活的时候也以为又是空的。所以只留一份判据，面板与花钱的那条路共用。
    ★ 「已编译」必须先剔（2026-11-03 的教训，截断取消后仍成立）：当年「先截 60
      再剔已编译」让排在后面的候选**永远够不着且无提示**。2026-11-09 用户拍板
      取消 60 条截断（见文件头 ⛔ 注）：提炼藏在一条龙里，候选一次消费完，
      流入速率由 EXTRACT_MAX_SOURCES 限住。
    """
    # ★ 候选免审（2026-11-05）：**未丢弃 = 可编译**。pending 与 accepted 一律进
    #   编译，逐条「采纳」不再是前置条件，裁决全部推迟到审批对话。唯一的提前
    #   干预是 discarded（明确排除），它不是审批，是预排除。
    # ★ 白名单而不是 `exclude(discarded)`：将来若加新状态，没显式列入的不会被
    #   悄悄吞进一次花钱的编译（钱花出去才知道多选了，成本高）。
    qs = IngestCandidate.objects.filter(status__in=INGEST_LIVE_STATUS)
    if cand_ids:
        qs = qs.filter(id__in=list(cand_ids))
    if source:
        st, sid = source
        qs = qs.filter(source_type=st, source_id=sid)
    all_cands = list(qs.order_by("source_type", "source_id", "id"))
    done = _compiled_ids() if done_ids is None else done_ids
    todo = [c for c in all_cands if c.id not in done]
    batch = todo
    # ★ paused：审批未完成时编译被暂停（2026-11-09 用户澄清恢复）。草稿待审批时
    #   不该开新一轮编译，否则草稿越堆越多、审批上下文被打散。
    from core import wiki as wstore
    paused = wstore.approval_active()
    return {"accepted": len(all_cands), "todo": len(todo), "batch": len(batch),
            "skipped": len(all_cands) - len(todo), "cands": batch, "paused": paused}


# ---------------------------------------------------------------- 编译闸
# ★ 同一时刻只允许跑一次编译（2026-11-02，用户的原话：「很容易点多次」）。
#   前端 `hx-disabled-elt` 只挡得住「同一页面上连点」；**两个标签页、点完刷新再点、
#   或者等得不耐烦再点一次**，照样并发跑两遍 —— 那是真金白银双倍花费，而且两遍
#   都看到「这页还没生成」，于是同一批候选出两份草稿（`_unique_slug` 加 `-2`，
#   人看到的是「怎么生成了两套页」）。
#   所以闸放在**花钱的地方**（core），不是放在按钮上 —— 判据只有一份，谁来调都挡。
#   ★ 用 flock 不用进程内 Lock：dev server 是多线程、生产可能多进程，只有文件锁
#     跨进程有效。★ 进程被 kill / 异常退出时内核自动释放锁，不会留下「死锁让
#     编译永远跑不起来」的残局（这是选 flock 而不是「写个 lock 文件 + 时间戳」的主因）。
#   ★ 注意分工（2026-11-03 补）：下面确实也写了一个状态文件，但它**不是锁**、
#     也不参与「能不能跑」的判定 —— 它只负责让页面看得见「正在跑」。判在跑必须
#     「文件在 **且** 锁被占」两者合看，理由见下面「编译中的状态」那一节。
_COMPILE_LOCK = ".compile.lock"


def _try_lock_compile():
    """拿编译锁：拿到返回 fd，拿不到（已有一份在跑）返回 None。"""
    path = wstore.root() / _COMPILE_LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _unlock_compile(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------- 编译中的状态
# ★ 为什么锁之外还要落一份「正在编译」（2026-11-03，用户报：点了「编译成草稿」，
#   立刻切到「开销」页再切回来，按钮又能点了 —— 于是会点第二次）。
#   flock 只回答「现在能不能跑」，它**不告诉页面**「已经有一份在跑」。浏览器一切页，
#   整页重新渲染，`_ctx` 没有任何东西可看 → 按钮照旧亮着。后端确实挡得住（回一句
#   「还在跑」，一分钱不多花），但那句话是**点了之后**才说的：人已经先误解了一次，
#   而且第二次点击的响应会把「上次编译」那块也换掉，草稿反而看不见了。
#   ⇒「在跑」必须是**页面上看得见的状态**，不能只活在某个 fd 里。
#
# ★ 为什么不能只用文件、仍要 flock：文件不会自己消失。进程被 kill / 机器重启 /
#   异常退出，文件会留在原地 —— 只认文件的话，编译就**永久**点不动了，还不报错
#   （比原来的毛病更糟）。内核会在进程消失时自动释放 flock，所以两个信号合起来
#   才能分辨三种局面：
#     文件在 + 锁被占   = 正在跑（按钮灰着，说清开始时间与已跑多久）
#     文件在 + 锁是空的 = 上次没跑完就没了（放行，但**必须说出来**，不许静默）
#     文件不在          = 空闲
#   ★ 这条「静默放行」正是本项目最贵的一课：上次被杀掉这件事如果没人看见，
#     人只会觉得「这次编译又出问题了」，然后一遍遍重试。
_COMPILE_STATE = "meta/compile.running.json"


def _state_path():
    return wstore.root() / _COMPILE_STATE


def _read_state() -> dict:
    """读「正在编译」状态；没有 / 读坏了都返回 {}。

    ★ 读坏按「没有」处理是安全的：页面当成空闲，人点了还会被 flock 挡住 ——
      最坏是多一次误解，不会多花钱、也不会永久锁死。
    """
    try:
        got = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _mark_running(**extra) -> None:
    """记下这一次编译：谁在跑、什么时候开始、要编几条。

    ★ 原子写（tmp + os.replace）：页面线程随时可能在读，读到半截 JSON 会被当成
      「没在跑」—— 状态面板就会静默消失，正是本函数要防的那类假象。
    ★ 写是**合并**（`setdefault` + `update`）：开始时间以第一次为准，后面补的字段
      不会把它冲掉。当前只有一处调用（算完 backlog 之后），留合并是为了将来
      「跑了一半再补个进度」时不必改这里。
    """
    got = _read_state()
    got.setdefault("started", time.time())
    got.setdefault("started_at", timezone.localtime().strftime("%H:%M:%S"))
    got.setdefault("pid", os.getpid())
    got.update({k: v for k, v in extra.items() if v is not None})
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(got, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _clear_state() -> None:
    """删掉状态文件（只在**正常收尾**时调）。漏删 = 下次被误报「没跑完」。"""
    try:
        _state_path().unlink()
    except OSError:
        pass


def _lock_held() -> bool:
    """编译锁现在有没有人拿着。★ 只探测：拿到就立刻放，绝不漏锁。"""
    fd = _try_lock_compile()
    if fd is None:
        return True
    _unlock_compile(fd)
    return False


def _elapsed_text(sec: int) -> str:
    if sec < 60:
        return "%d 秒" % sec
    m, s = divmod(sec, 60)
    return "%d 分 %d 秒" % (m, s) if m < 60 else "%d 分" % (m // 60)


def compile_state() -> dict:
    """页面该看到的编译状态。

    返回 `{running, started_at, elapsed, elapsed_text, n, source, aborted}`：
      running=True  → 现在有一份在跑（按钮必须灰，且说清开始时间与已跑多久）
      aborted={...} → 上次没收尾就没了（放行，但页面必须显示出来）

    ★ 先读文件再探锁：没有文件就直接返回空闲，不去碰锁 —— 日常页面渲染不打扰
      花钱那条路（万一探锁与它抢着开锁，会白回一句「还在跑」）。
    """
    got = _read_state()
    if not got:
        return {"running": False, "aborted": {}}
    if not _lock_held():
        return {"running": False, "aborted": got}
    started = got.get("started") or 0
    # ★ 时钟回拨 / 系统改时间时 max(0, …) 保证不出现「已 -3 秒」
    elapsed = int(max(0, time.time() - started)) if started else 0
    return {"running": True, "started_at": got.get("started_at") or "",
            "elapsed": elapsed, "elapsed_text": _elapsed_text(elapsed),
            "n": got.get("n") or 0, "source": got.get("source") or "",
            # ★ 第十一步：一条龙分「提炼 / 编译」两段，页面要把当前阶段说出来 ——
            #   提炼也是几分钟，只报「编译中」会让人以为卡在编译上。
            "phase": got.get("phase") or "",
            "aborted": {}}


def _compile_busy_result() -> dict:
    """拿不到编译锁时统一返回的那句话（两条入口共用）。

    ★ 抽成一处的理由（第十一步）：`compile_candidates` 与 `extract_then_compile`
      拿的是同一把锁，各写一遍「还在跑」的话，迟早一边改了另一边没改
      （同口径两处必漏一处）。
    ★ 把「谁在跑、跑了多久」写进这句话（2026-11-03）：只说「还在跑」，人分不清
      是正常进行还是卡死，也不知道该等还是该再点 —— 而再点一次就是又一次
      模型调用（被挡下也要付一次误解的代价）。
    """
    log.info("wiki 编译被闸挡住：已有一份在跑")
    run = compile_state()
    detail = "，".join(x for x in (
        run.get("started_at") and "%s 开始" % run["started_at"],
        run.get("running") and "已 %s" % run["elapsed_text"],
        run.get("n") and "%d 条候选" % run["n"],
    ) if x)
    return {"ok": False,
            "err": "上一次编译还在跑%s（同一时刻只跑一份，防重复扣钱）："
                   "通常 1–3 分钟。等它结束再看「待审批」——"
                   "如果草稿已经出来了，就不用再点。"
                   % ("（%s）" % detail if detail else ""),
            "drafts": [], "groups": [], "dropped": [], "skipped": 0}


def compile_candidates(cand_ids=None, source=None) -> dict:
    """把未丢弃的候选编译成草稿（S1 分组 → S2 逐页生成 → 守卫 → 写 .drafts/）。

    返回 {ok, err, drafts:[slug], groups:[{title,domain,n}], dropped:[{cand_id,why}],
          skipped:int}。err 是**能直接显示给人**的话。

    ★ 外面包一层编译锁（见上方「编译闸」）：拿不到锁就**当场**返回一句人话，
      不排队、不静默等待 —— 排队会让人以为没反应又点一次，等于没加闸。
    """
    fd = _try_lock_compile()
    if fd is None:
        return _compile_busy_result()
    try:
        # ★ 审批未完成时拒绝新编译（2026-11-09 用户澄清）：草稿待审批时不该开新一轮
        #   编译，否则草稿越堆越多、审批上下文被打散。编译中由锁挡，编译后待审批
        #   由 approval_active 挡 —— 两者分开，不冲突。
        from core import wiki as wstore
        if wstore.approval_active():
            return {"ok": False, "err": "审批进行中（有草稿待裁决），先跑完审批再编译",
                    "drafts": [], "groups": [], "dropped": [], "skipped": 0, "paused": True}
        # ★ 状态由 `_compile_locked` 在**算出这批有几条之后、调模型之前**写
        #   （见那里的说明）：写在这里的话只知道「有人在跑」，不知道在编几条，
        #   而「已跑 40 秒」不配「几条」人没法判断该等多久。
        return _compile_locked(cand_ids, source)
    finally:
        # ★ 顺序不能反：先清状态、再放锁。反了会出现「锁空了、文件还在」的一瞬，
        #   正好被另一个线程读成「上次没跑完」，白报一次警。
        _clear_state()
        _unlock_compile(fd)


def _compile_locked(cand_ids=None, source=None) -> dict:
    """真正干活的部分（只由 `compile_candidates` 在持锁状态下调用）。"""
    if not settings.WIKI_INGEST_ENABLED:
        return {"ok": False, "err": "编译未开启（WIKI_INGEST_ENABLED=0）",
                "drafts": [], "groups": [], "dropped": [], "skipped": 0}

    # ★ 2026-11-09 用户拍板：去掉「审批中拒编译」（原改动 E）。草稿分批产生，
    #   有草稿就拒编会把后续编译卡死；并发由编译锁 + 运行期 mutex 兜底
    #   （编译进行中审批/编辑写被挡），拿到锁就该把这一轮跑完。

    # ★ 选哪批候选只有一份判据（`compile_backlog`），面板显示的数字也是它 ——
    #   自己再写一遍「取候选再剔已编译」，就成了两个口径，迟早对不上。
    backlog = compile_backlog(cand_ids=cand_ids, source=source)
    if not backlog["accepted"]:
        # no_candidates 是给一条龙的结构标记：提炼跑完却 0 条候选是合法结论，
        # 编排层据此转成 ok=True 的如实汇报，而不是红着「编译未完成」骗人。
        return {"ok": False, "err": "没有未丢弃的候选可编译（先提炼，或把丢弃的改判回来）",
                "drafts": [], "groups": [], "dropped": [], "skipped": 0,
                "no_candidates": True}
    cands, skipped = backlog["cands"], backlog["skipped"]
    # ★ 写「正在编译」的唯一一处，且必须在**第一次调模型之前**：页面线程只认这个
    #   文件，晚一步写，那一整段（1–3 分钟，正是人在别处切来切去的时候）按钮都是亮的。
    #   ★ 位置选在这里而不是拿到锁的那一刻：只有算完 backlog 才知道「几条候选」，
    #     而「已跑 40 秒」不配「还有几条」的话，人没法判断该等多久。
    #   ★ 记账用 merge（`_mark_running` 合并而非覆盖），所以后面的调用不会把这些冲掉。
    _mark_running(n=len(cands), skipped=skipped,
                  source="%s:%s" % source if source else "")
    if not cands:
        return {"ok": True, "err": "", "drafts": [], "groups": [], "dropped": [],
                "skipped": skipped,
                "msg": "这些候选都已经编译过了（草稿在 .drafts/ 待审批，或已入库到 wiki/）"}

    # ★ 已有页必须在分组时就看见（阶段 2.3d）：没有这一眼，S1 会为同一个概念
    #   反复造新页 —— 库里 `llm-moat-validator` / `specialized-ai-validator` 就是
    #   同源同判断被建成两页。页清单只给 slug/title/一句话，不给全文。
    # ★ 待审批草稿也在清单里（2026-11-04，用户问「会看草稿的页面吗」）：答案
    #   曾经是「不看，而且并入会被拒」—— 同一概念等审批时又编译一次，会再出
    #   一份草稿，人得审批两次。现在草稿进目录（同 slug 时**盖过**已入库页：
    #   并入对象是这份草稿，它审过之后就是那一页），新候选并进去，一次审批。
    pages = wstore.pages()
    entries = {p["slug"]: p for p in pages}
    for d in wstore.list_drafts():
        entries[d["slug"]] = {
            "slug": d["slug"], "title": d["title"], "domain": d["domain"],
            "updated": d["updated"] or "",
            "summary": wstore._first_sentence(d.get("body") or ""),
            "page": f".drafts/{d['slug']}.md", "draft": True,
            # ★ 草稿的候选也进目录卡（第七步）：S1 看得见「那一页里有什么」，才判得
            #   出要不要拆。草稿本身拆不了（`pull_page` 只认 `wiki/`），`_check_split`
            #   会明确拒掉并让人先处理这份草稿。
            "candidates": d.get("candidates") or [],
        }
    catalog_pages = list(entries.values())
    groups, err, plan_notes = plan_groups(cands, catalog_pages)
    if err:
        log.warning("wiki 编译分组失败：%s", err)
        # ★ 分组失败也要落盘：这是「点了编译、什么都没出」里最贵的一种，
        #   刷新后再看不到就等于白花一次 S1 的钱、还查不出原因。
        _record_compile([], [], cands, [], skipped, err, notes=plan_notes)
        return {"ok": False, "err": err, "drafts": [], "groups": [], "dropped": [],
                "skipped": skipped, "notes": plan_notes}

    cands_by_id = {c.id: c for c in cands}
    payload_text = _payload(cands)
    drafts, dropped, made, collisions = [], [], [], []
    # ★ 从 `plan_groups` 的 notes 起手，不能新开一个空列表：那里装着「哪一批分组
    #   失败、哪几条这次没编」，丢掉就等于静默少编了几条（本环节最贵的错法）。
    notes, noop = list(plan_notes), []
    page_slugs = _page_slugs()
    pages_by_slug = entries
    # ★ 本编已写过草稿的目标：同轮两组写同一页，后一组会拿**老底本**再并一次、
    #   把前一组刚写的内容整个盖掉 —— 这道闸必须留在写盘这一侧，plan_groups 的
    #   by_target 挡不住「模型给两组起了不同 target 却落同一页」以外的漏网。
    #   （2026-11-03 之前这里还兼挡「已有并入 X 的草稿待审批」；现在那种情况
    #   改为**并进那份草稿**，见下。）
    written = set()
    box = wstore.root() / ".drafts"

    # ---- 拆页（第七步）要用的旧候选：`take` 里的 id **不在**本轮 `cands` 里 ——
    #   它们早就编译进页了，这轮不会被重新提炼。按需取一次（没有拆页就不查库）。
    split_groups = [g for g in groups if g.get("split_plan")]
    all_cands = {}
    if split_groups:
        from core import wiki_agent as wagent
        all_cands = wagent.live_candidates()
    all_cands.update(cands_by_id)
    taken = set()          # 本轮真正拆走的候选（只算**落地成功**的拆页）
    replaced = set()       # 本轮产出替换稿的原页 slug

    for g in groups:
        if g.get("note"):
            notes.append("「%s」%s" % (g["title"], g["note"]))
        plan = g.get("split_plan") or None
        target = "" if plan else (g.get("target") or "")
        old_page = None
        base_is_draft = False
        if target:
            if target in replaced:
                # 本轮已经把这一页拆成了替换稿：再并进去就是拿**老底本**重写一遍，
                # 会把刚拆走的候选又原样写回来（两份稿子指向同一页，后写的赢）。
                made.append({"title": g["title"], "domain": g["domain"], "target": target,
                             "n": len(g["cand_ids"]), "cand_ids": list(g["cand_ids"]),
                             "err": f"本轮已把 {target} 拆成替换稿，不能再并进去"
                                    "（先处理那份草稿）"})
                continue
            if target in written:
                made.append({"title": g["title"], "domain": g["domain"], "target": target,
                             "n": len(g["cand_ids"]), "cand_ids": list(g["cand_ids"]),
                             "err": f"本编已经并过 {target}（同一页一轮只并一次）"})
                continue
            entry = pages_by_slug.get(target)
            base_is_draft = bool(entry and entry.get("draft"))
            old_page = _load_page(entry)
            if old_page is None:
                notes.append("「%s」想并入 %s，但那一页读不到 —— 按新建处理"
                             % (g["title"], target))
                target = ""
                base_is_draft = False

        if plan:
            # ★ 新页 = **拆出来的旧候选 + 本组新候选**。旧候选不在本轮 `cands` 里，
            #   必须并进 `all_cands` 才能被 S2 写成正文。
            ids = list(dict.fromkeys(list(plan["take"]) + list(g["cand_ids"])))
            missing = [i for i in ids if i not in all_cands]
            if missing:
                made.append({"title": g["title"], "domain": g["domain"], "target": "",
                             "n": len(ids), "cand_ids": ids, "split": True,
                             "err": "要拆的候选 %s 读不到（已被丢弃？）"
                                    % "、".join(str(i) for i in missing)})
                continue
            g_sub = dict(g, cand_ids=ids,
                         title=plan["title"] or g["title"],
                         domain=plan["domain"] or g["domain"])
            # ★ 数字守卫的底本只给**这一页的候选**：拆页时若沿用整轮 payload，
            #   别的组的数字会替这一页的编数背书（那正是守卫要拦的东西）。
            text, slug, drop, perr = _compose_page(
                g_sub, all_cands, _payload([all_cands[i] for i in ids]), None)
        else:
            ids = list(g["cand_ids"])
            text, slug, drop, perr = _compose_page(
                g, cands_by_id, payload_text, old_page)
        dropped.extend(drop)
        if not text and not perr:
            # 并入时「老页已有同义内容」是合法结果：不生成草稿，也不算失败
            noop.append(g["title"])
            continue
        if perr:
            log.warning("wiki 编译跳过一组「%s」：%s", g["title"], perr)
            made.append({"title": g["title"], "domain": g["domain"], "target": target,
                         "n": len(ids), "cand_ids": ids,
                         "err": perr, "split": bool(plan)})
            continue
        fm, _ = wstore.parse_front(text)
        rep_path = None
        if plan:
            # ---- 替换稿：原页复制成草稿 + 把拆走的候选从 candidates 里去掉
            #   ★ 只动 `candidates`、不动正文：正文里那几行该由人来删（审批浮层就是
            #     干这个的）。这一步产出的不是「成品」，是一份**提案**。
            #   ★ 原页原地不动（`pull_page` 的既有语义）：移动会让反链断、index
            #     缺行、`[[wikilink]]` 404。
            ok, info = wstore.pull_page(plan["from"])
            if not ok:
                made.append({"title": g["title"], "domain": g["domain"], "target": "",
                             "n": len(ids), "cand_ids": ids, "err": info, "split": True})
                continue
            rep_path = box / f"{plan['from']}.md"
            try:
                rep_text = rep_path.read_text(encoding="utf-8")
                rep_fm, _rb = wstore.parse_front(rep_text)
                drop_set = set(plan["take"])
                keep = [i for i in wstore._cand_ids(rep_fm) if i not in drop_set]
                rep_path.write_text(wstore._set_fm_line(
                    rep_text, "candidates", "[%s]" % ", ".join(str(i) for i in keep)),
                    encoding="utf-8")
            except OSError as exc:
                log.error("拆页替换稿改写失败 %s：%s", plan["from"], exc, exc_info=True)
                rep_path.unlink(missing_ok=True)
                return {"ok": False, "err": f"拆页替换稿改写失败：{exc}", "drafts": drafts,
                        "groups": made, "dropped": dropped, "skipped": skipped}
            taken.update(plan["take"])
            replaced.add(plan["from"])
            drafts.append(plan["from"])
            made.append({"title": str(rep_fm.get("title") or plan["from"]),
                         "domain": str(rep_fm.get("domain") or ""),
                         "target": plan["from"], "n": len(plan["take"]),
                         "cand_ids": list(plan["take"]), "err": "", "split": True})
            wstore.log_append(
                "compile", f".drafts/{plan['from']}.md",
                "拆页：候选 %s 移出 → 新页；正文里对应的行待人工删"
                % "、".join(str(i) for i in plan["take"]))
            log.info("wiki 编译：拆 %s（移出 %d 条候选）", plan["from"], len(plan["take"]))
        if target:
            # ★ 合并草稿**必须**与目标页同名：approve 靠这个文件名找覆盖对象，
            #   也靠它让「待审批卡 → 目标路径」一眼对上。所以这里不走 `_unique_slug`
            #   （那个函数会为了避开已入库页把名字改成 `-2`，正好改错）。
            slug = target
            # ★ 目标本身是待审批草稿 → 覆盖它就是**本题**（并进那份草稿）；
            #   目标是已入库页而 `.drafts/<slug>.md` 又存在 → 那份草稿一定不是
            #   合并草稿（合并草稿已在目录里盖过那一页），盖掉它会丢人没审过的
            #   内容，必须停。
            if not base_is_draft and (box / f"{slug}.md").exists():
                made.append({"title": g["title"], "domain": g["domain"], "target": target,
                             "n": len(ids), "cand_ids": ids,
                             "err": f".drafts/{slug}.md 已存在（不是合并草稿），先处理它"})
                continue
            # ★ 已有一份**别的文件名**的并入稿（旧版「原地改判」留下的源稿名）：
            #   再写一份同目标草稿 = 用户实测的「两份 AI泡沫、内容各缺一半」。
            #   停下来让人先处理那一份，别让编译静默造出第二份。
            if not base_is_draft:
                dup, _dup_page = _merge_draft_for(slug)
                if dup and dup != slug:
                    made.append({"title": g["title"], "domain": g["domain"],
                                 "target": target, "n": len(ids), "cand_ids": ids,
                                 "err": f"已有一份并入 {slug} 的草稿（{dup}），先处理它"})
                    continue
            written.add(target)
        else:
            wanted = slug
            slug = _unique_slug(slug, page_slugs)
            page_slugs.add(slug)
            # ★ 「跟已入库的页撞名了，所以我把它改名成 xxx-2」必须说出来：
            #   否则人看到的是一个陌生的页名，而真正该做的是去合并那两页。
            if slug != wanted and wanted in page_slugs:
                collisions.append({"wanted": wanted, "used": slug, "page": f"{wanted}.md"})
        try:
            wstore.ensure_dirs()
            (box / f"{slug}.md").write_text(text, encoding="utf-8")
        except OSError as exc:
            log.error("wiki 草稿写入失败 %s：%s", slug, exc, exc_info=True)
            # ★ 拆页时新页写不进去，那份替换稿必须收回：否则它是「候选已经少了几条、
            #   却没有任何新页接住」的稿子 —— 而那几条候选会从「待编译」里永久消失
            #   （它们仍然在某个草稿的旧 candidates 里，但评审者看到的是一份缺口的页）。
            if rep_path is not None:
                rep_path.unlink(missing_ok=True)
            return {"ok": False, "err": f"草稿写入失败：{exc}", "drafts": drafts,
                    "groups": made, "dropped": dropped, "skipped": skipped}
        drafts.append(slug)
        made.append({"title": str(fm.get("title") or g["title"]), "domain": g["domain"],
                     "target": target, "n": len(ids),
                     "cand_ids": list(ids), "err": "", "split": bool(plan)})
        note = f"{len(ids)} 条候选 · sources={len(fm.get('sources') or [])}"
        if target:
            kind = "并入待审批草稿" if base_is_draft else "并入已有页"
            note = f"{kind} {target} · " + note
            log.info("wiki 编译：%s %s（+%d 条候选）", kind, target, len(ids))
        elif plan:
            note = f"拆页重组自 {plan['from']}（拆出 {len(plan['take'])} 条）· " + note
            log.info("wiki 编译：拆 %s → 新页 %s（%d 条候选）",
                     plan["from"], slug, len(ids))
        wstore.log_append("compile", f".drafts/{slug}.md", note)

    # ★★ 迁移守恒（第七步）：**拆走的候选不许凭空消失**。
    #   与 `_compose_page` 里那条「并入后老内容没丢」是两层不同的守卫，各管一件事：
    #   那条守「并入别把老正文写丢」，这条守「拆出来的候选在别处有着落」。判据是一个
    #   集合差，与页数、与拆几页都无关 —— 所以「多页拆」「纯拆分」都不会让它变复杂。
    #   只统计**真正落地**的拆页（`taken`）：拆失败的那一组本来就没搬走任何东西，
    #   它由自己的 `err` 报告，不该让整轮编译因为一次可解释的失败而全灭。
    # ★ 落到哪里以**磁盘上的草稿**为准，不看内存里打算写什么 —— 后者只能证明「我
    #   以为写了」，而这条守卫存在的意义正是程序可能写错。
    def _landed_ids():
        """磁盘上的草稿里出现的候选 id（两条守恒守卫共用一份口径）。"""
        out = set()
        for s in drafts:
            try:
                dfm, _db = wstore.parse_front(
                    (box / f"{s}.md").read_text(encoding="utf-8"))
            except OSError:
                continue
            out.update(wstore._cand_ids(dfm))
        return out

    landed = _landed_ids()

    if taken:
        lost = sorted(taken - landed)
        if lost:
            detail = ("拆走的候选 %s 没有落到任何草稿里（程序错误，已整轮回滚）"
                      % "、".join(str(i) for i in lost))
            log.error("wiki 编译：%s", detail)
            for s in drafts:
                (box / f"{s}.md").unlink(missing_ok=True)
            _record_compile([], made, cands, dropped, skipped, detail, notes=notes)
            return {"ok": False, "err": detail, "drafts": [], "groups": made,
                    "dropped": dropped, "skipped": skipped, "notes": notes}

    # ★★ 候选守恒（2026-11-09 真实库第一次清零重编实测发现）：S1 提示词明写
    #   「每条候选必须且只能出现在一组里。不许丢、不许重复。」—— 但**程序从没验过**。
    #   真实库第一次跑：喂进 60 条，落到 15 页草稿里的只有 **28 条**，32 条凭空消失，
    #   而日志和反馈都写「60 条候选」，人以为全进去了。
    #   ⇒ 这里按**磁盘产物**算「喂进去的 − 落了地的 − 守卫丢的」，差额必须进 notes
    #     （人看得见）+ log.error（机器看得见）。不整轮回滚：草稿本身是好的，
    #     而回滚等于把已经花掉的 S1/S2 钱全扔掉；那些候选没进草稿就仍是「待编译」，
    #     下一轮自然会被再试（`_compiled_ids` 只认产物）。
    fed = {c.id for c in cands}
    dropped_ids = {d.get("cand_id") for d in (dropped or []) if d.get("cand_id")}
    unplaced = sorted(fed - landed - dropped_ids)
    if unplaced:
        shown = "、".join("#%s" % i for i in unplaced[:8])
        if len(unplaced) > 8:
            shown += " 等 %d 条" % len(unplaced)
        msg = ("%d 条候选没被分到任何一页（分组漏掉了它们）：%s —— 它们还留在"
               "待编译里，下次编译会再试" % (len(unplaced), shown))
        log.error("wiki 编译：%s", msg)
        notes.append(msg)

    if not drafts:
        if noop and not any(m.get("err") for m in made):
            # 每组都「老页已有同义内容」：这不是失败，别报红色错误
            msg = "没有新内容可并入：%s 里已经有这些话了" % "、".join(noop)
            _record_compile([], made, cands, dropped, skipped, "", notes=notes, noop=noop)
            return {"ok": True, "err": "", "drafts": [], "groups": made, "dropped": dropped,
                    "skipped": skipped, "notes": notes, "noop": noop, "msg": msg}
        # ★ 一页都没出时，把**每一组**的理由都带上：只说第一条，人会以为
        #   「改完这一组就好了」，而其余几组同样是死路。
        reasons = ["「%s」%s" % (m["title"], m["err"]) for m in made if m.get("err")]
        detail = "；".join(reasons) or "全部组都没通过守卫"
        _record_compile([], made, cands, dropped, skipped, detail, notes=notes)
        return {"ok": False, "err": "没有生成任何草稿：%s" % detail, "drafts": [],
                "groups": made, "dropped": dropped, "skipped": skipped, "notes": notes}

    # ★ 「一条候选一页」要**说出来**，不能静默：分组退化成一对一，等于这次分组白做，
    #   而且会让人面对一堆半页。这不是错误（内容确实可能两两不同），但必须让人看见
    #   —— 静默的坏结果比报错更贵，因为没人会去查。
    #   ★ 已有页也吃掉了候选（并入的那几条），所以按「进页的候选」算比例才对得上。
    #   ★ 拆页的替换稿不是「新页」：把它算进分母，一个正常的分组会被误判成「一条
    #     候选一页」（拆一次凭空多出一份草稿）。
    new_pages = len(drafts) - len(replaced)
    over_split = len(cands) >= 4 and new_pages >= max(3, int(len(cands) * 0.75))
    log.info("wiki 编译完成：%d 页草稿（喂 %d 条候选，落地 %d 条，丢 %d 条，"
             "并入 %d 页，拆 %d 页）",
             len(drafts), len(cands), len(landed), len(dropped),
             sum(1 for m in made
                 if m.get("target") and not m.get("err") and not m.get("split")),
             len(replaced))
    # ★★ 第十三步：编译产出的草稿正文是空的（形态 A），这里**立即**重编结论。
    #   统一走审批链同一个 `wiki_loop._polish_one`，不另写一份产出逻辑 ——
    #   「同口径落两处，改一处必漏另一处」的教训。
    #   ★ 拆页替换稿是半成品（原页减候选），按 §1 时机表**暂不自动**，
    #     留审批里人改完再重编，不为一份注定还要改的稿子花钱。
    #   ★ 花钱点集中在 `wiki_loop._call`；桩要打在**被调用点**，并配
    #     「桩真被用上」反证（编译后草稿正文真的有结论才算跑过）。
    #   ★ 失败绝不静默：写进 notes（面板可见）+ log.error + 返回 polish_failed，
    #     草稿仍带 `stale: true`，`_validate_draft` 会因「正文为空」拒绝入库。
    from core import wiki_loop as _wloop
    polished, polish_failed = [], []
    for s in drafts:
        if s in replaced:
            continue
        good, why = _wloop._polish_one(s)
        if good:
            polished.append(s)
        else:
            polish_failed.append({"slug": s, "why": why})
            notes.append(f"「{s}」结论重编失败：{why}（草稿仍带待重编标记）")
            log.error("wiki 编译：%s 结论重编失败：%s", s, why)

    _record_compile(drafts, made, cands, dropped, skipped, notes=notes, noop=noop)
    return {"ok": True, "err": "", "drafts": drafts, "groups": made,
            "dropped": dropped, "skipped": skipped, "notes": notes, "noop": noop,
            "polished": polished, "polish_failed": polish_failed,
            "unplaced": unplaced, "landed": len(landed),
            "over_split": over_split, "collisions": collisions}


def _load_page(p):
    """已入库页 → 编译要用的形状（含正文与 frontmatter）。读不到返回 None。"""
    if not p:
        return None
    path = wstore.root() / p["page"]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        log.warning("读不到已有页 %s（按新建处理）", p.get("page"))
        return None
    fm, body = wstore.parse_front(text)
    return {"slug": p["slug"], "domain": p["domain"],
            "title": p.get("title") or p["slug"], "page": p["page"],
            "draft": bool(p.get("draft")),
            "fm": fm, "body": body}


def _load_draft_page(slug: str):
    """待审批草稿 → 与 `_load_page` 同形状（`draft=True`）。读不到返回 None。"""
    p = wstore.root() / ".drafts" / f"{slug}.md"
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return None
    fm, body = wstore.parse_front(text)
    return {"slug": slug, "domain": str(fm.get("domain") or "").strip(),
            "title": str(fm.get("title") or slug), "page": f".drafts/{slug}.md",
            "draft": True, "fm": fm, "body": body}


def _merge_draft_for(target: str):
    """找「已声明并入 target」的待审批草稿 → `(slug, page_shape)` / `(None, None)`。

    ★★ 同一目标**只能存在一份**并入稿（2026-11-13 用户实测：D1、D8 先后并入
      「AI泡沫」，两份稿各自带着目标页旧候选 + 自己那批，内容互不包含；审批者
      看到两页同名，提交时后一份覆盖前一份，先入库的那批候选静默丢失）。
      所以第二次并入必须**编辑已有草稿**，不能从已入库页再拉一份。
    ★ 判据只看 frontmatter 的 `merges`，不看文件名：旧版「改为并入」是原地
      改判，草稿名沿用源稿（`tui-li-...`），靠文件名找不到它。
    ★ 纯读（`list_drafts` 不写字节）：本函数在写盘前的校验段被调，拒写时必须
      一个字节都没动。
    """
    for d in wstore.list_drafts():
        if str(d.get("merges") or "").strip() != target:
            continue
        page = _load_draft_page(d["slug"])
        if page is not None:
            return d["slug"], page
    return None, None


def _write_drafts_atomic(pairs) -> tuple:
    """`[(Path, text)]` → 先全部写 tmp，再逐个 os.replace。返回 `(ok, err)`。

    ★ 与 `wiki_agent._commit_writes` 同一口径：把「写坏一个、另一个没写」的
      窗口压到最小；tmp 全写成功之前不动任何正式文件。
    """
    staged = []
    try:
        for path, content in pairs:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            staged.append((tmp, path))
        for tmp, path in staged:
            os.replace(tmp, path)
    except OSError as exc:
        log.error("并入写盘失败：%s", exc)
        return False, f"写盘失败：{exc}（请检查 .drafts/ 里的 .tmp 残留）"
    return True, ""


def _compose_append(merge_slug: str, merge_page: dict, ids, live) -> tuple:
    """把 ids 追加进一份**已有**并入稿 → `(text_or_None, err)`。

    `text is None` 且无 err = 这些候选目标稿里全都有，本次没有新增。
    ★ 正文保留旧结论、只打 stale（与 `wiki_agent._set_split` 同契约）：重编失败
      时人还能看见上一版正文，而不是一份空白稿；成功由 `_polish_one` 覆盖。
    """
    group = {"cand_ids": list(ids), "title": merge_page.get("title") or merge_slug,
             "domain": merge_page.get("domain") or "", "tags": [],
             "slug": merge_slug}
    text, _slug, _drop, err = _compose_page(group, live, "", merge_page)
    if err:
        return None, err
    if not text:
        return None, ""
    body = (merge_page.get("body") or "").strip()
    if body:
        text = text + body + "\n"
    return text, ""


def merge_draft_into(slug: str, target: str) -> tuple:
    """把一份**整页草稿**并入已入库页 `<target>`。返回 (ok, payload)。

    审批浮层里「可能相关的已入库页」按钮的落点（2026-11-11）。人点一下，机器
    不替他判断该不该并，只把「并」这件事做对；判据仍在 `_validate_draft`。

    ★★ 2026-11-13 改（用户实测）：并入某页只产出一份**以目标页命名的并入稿**
      （`.drafts/<target>.md`，frontmatter `merges: <target>`）。
      - 第一次点：从已入库页拉一份草稿，**分配新的 draft_id**（这份稿是
        「目标页的替换稿」，不是源草稿的延续；沿用源稿号会让 D 编号指向已经
        换了含义的页）。
      - 第二次点（别的草稿并入同一页）：检出已有并入稿，**直接在它里面编辑** ——
        候选取并集、正文标 stale 后重编。绝不从已入库页再拉一份。
      旧实现是「原地改判」：D1 / D8 各自被改写成 merges: 同一页，文件名不变，
      于是出现两份同名、内容各缺一半的草稿；提交时后一份覆盖前一份，
      先入库的候选静默丢失。
    ★ 源稿整份搬走后归档（`wstore.discard` 留证据）。候选已写进并入稿，
      `discard` 的「仍被别处引用就不废弃」判据会保住它们，不会误杀。
    ★ 产物形状与编译期 `existing:<slug>`、审批对话 `merge_into` 逐字同形
      （复用 `_compose_page`），仍走**同一个** `wiki_loop._polish_one` 重编结论。
    ★ 跨域由人拍板：`_compose_page` 用**目标页**的 domain 归档（机器分组的
      `_resolve_target` 跨域闸约束的是 S1，不是人）。domain 在 ai/invest 间摇摆
      时，这正是人该能改的那种判断。
    """
    slug, target = (slug or "").strip(), (target or "").strip()
    if not wstore._safe_slug(slug) or not wstore._safe_slug(target):
        return False, "草稿名或目标页名非法"
    box = wstore.root() / ".drafts"
    src = box / f"{slug}.md"
    if not src.exists():
        return False, f"草稿不存在：{slug}"
    fm, _old_body = wstore.parse_front(src.read_text(encoding="utf-8"))
    # ★ 只有普通新页稿能改判：替换稿 / 删除稿 / 并入稿 / 搬迁稿都已有明确去向，
    #   再给一个「并进别页」的入口，等于让人把一页已确认的知识再覆盖一次。
    #   判据收在 `wstore.draft_direction` 一处（审批浮层 / LLM 推荐共用），别抄。
    if key := wstore.draft_direction(fm):
        return False, f"这份草稿已有明确去向（{key}），不能再改判为并入"
    entry = next((p for p in wstore.pages() if p["slug"] == target), None)
    if entry is None:
        return False, (f"库里没有这一页：{target} —— 待审草稿不是合法的并入目标"
                       "（提示只列已入库页）")
    ids = wstore._cand_ids(fm)
    if not ids:
        return False, "这份草稿没有挂候选，没有可并入的内容"
    live = {c.id: c for c in IngestCandidate.objects.filter(
        id__in=ids, status__in=INGEST_LIVE_STATUS)}

    merge_slug, merge_page = _merge_draft_for(target)
    if merge_slug == slug:
        return False, f"这份草稿已经声明并入 {target}，不用再并一次"
    if merge_slug is None and (box / f"{target}.md").exists():
        # 同名稿存在但不是并入稿（编辑稿 / 普通新页稿）：不能盖掉人没审过的内容。
        return False, (f".drafts/{target}.md 已有一份待审草稿（不是并入稿），"
                       "先处理它再并入")
    existed = merge_slug is not None

    if existed:
        text, err = _compose_append(merge_slug, merge_page, ids, live)
        if err:
            return False, err
        wrote = text is not None
        pairs = [(box / f"{merge_slug}.md", text)] if wrote else []
    else:
        old = _load_page(entry)
        if old is None:
            return False, f"读不到目标页 {target}"
        group = {"cand_ids": ids, "title": fm.get("title") or slug,
                 "domain": str(fm.get("domain") or "").strip(),
                 "tags": fm.get("tags") or [], "slug": slug}
        text, _new_slug, _drop, err = _compose_page(group, live, "", old)
        if err:
            return False, err
        if not text:
            return False, "目标页已经有这些候选，本次没有新增（无需并入）"
        # ★ 拉入 = 新身份：显式发号，别等展示入口补（补号会让同一份稿两次
        #   渲染之间跳号，chat / 视图拿到的 payload 也不知道它叫 D 几）。
        text = wstore._set_fm_line(
            text, wstore.DRAFT_ID_FM, str(wstore.next_draft_id()))
        merge_slug = target
        wrote = True
        pairs = [(box / f"{merge_slug}.md", text)]

    ok, err = _write_drafts_atomic(pairs)
    if not ok:
        return False, err
    # ★ 整份搬走 → 归档源稿（不删，留证据好回看）。候选此刻已被并入稿引用，
    #   `discard` 不会把它们当「没人要」废弃掉。
    ok, dest = wstore.discard(slug, f"整页并入 {target}，源稿自动归档")
    if not ok:
        log.error("并入稿已写入，但归档源稿 %s 失败：%s", slug, dest)
        return False, f"并入稿已写入，但归档源稿 {slug} 失败：{dest}"

    from core import wiki_loop
    good, why = (True, "")
    if wrote:
        good, why = wiki_loop._polish_one(merge_slug)
    action = "并入已有稿" if existed else "拉入新稿"
    wstore.log_append("merge-into", f".drafts/{merge_slug}.md",
                      f"{action} ← {slug} · → {target} · 候选 {len(ids)} 条 · "
                      + ("结论已重编" if good else f"结论重编失败：{why}"))
    if not good:
        log.error("并入后结论重编失败 %s（源 %s → %s）：%s",
                  merge_slug, slug, target, why)
    return True, {"slug": merge_slug, "draft": merge_slug, "target": target,
                  "to": target, "n": len(ids), "moved": ids,
                  "archived": [slug], "existing": existed,
                  "polished": bool(good), "polish_err": "" if good else str(why)}


def resolve_merge_target(name: str) -> tuple:
    """把「AI泡沫」或「ai-pao-mo」解析成**已入库页**的 slug。返回 `(slug, err)`。

    ★ 只认 slug / title 的**精确**匹配，不做模糊相似度：并错页会覆盖一页人已
      确认过的知识，宁可把可用清单摆出来让他再指定一次。
    ★ title 重名直接拒，不猜第一个；待审草稿不是合法目标（它自己都还没入库）。
    """
    name = (name or "").strip()
    if not name:
        return "", "缺少目标页"
    rows = wstore.pages()
    by_slug = [p["slug"] for p in rows if p["slug"] == name]
    if len(by_slug) == 1:
        return by_slug[0], ""
    if len(by_slug) > 1:
        return "", f"目标 slug {name!r} 对应多个已入库页，无法判定"
    by_title = [p["slug"] for p in rows
                if (p.get("title") or "").strip() == name]
    if len(by_title) == 1:
        return by_title[0], ""
    if len(by_title) > 1:
        return "", (f"目标标题 {name!r} 对应多页，请改用 slug：" +
                    "、".join(by_title))
    return "", ("库里没有这一页：%s（并入目标必须是**已入库页**，待审草稿不算）。"
                "可用 slug：%s" % (name, "、".join(p["slug"] for p in rows[:12])))


def attach_candidates_to_page(from_slug: str, cand_ids, to_slug: str) -> tuple:
    """把一份草稿里的**指定候选**并入已入库页 → 产出可审批的并入稿。

    审批对话 `merge_into` 工具的唯一落点（2026-11-13）。一次调用做完三件事：
      ① 源草稿留下其余候选、结论重编；
      ② 产出 `.drafts/<to>.md`：`_compose_page` 写 `merges: <to>` +
         `candidates = 目标页原有 ∪ 本次指定` + `stale`，**分配新的 draft_id**；
      ③ 两份都走 `wiki_loop._polish_one` 重编结论。
    之后审批浮层自动切到并入 diff 分支，commit 时 `_approve_merge` 备份旧版再
    覆盖目标页 —— 「自动卷入审批」不需要额外开关。

    ★★ 2026-11-13：同一目标已有并入稿时**不再拒绝，也不新建第二份** ——
      直接在那一份里追加候选（`_merge_draft_for`）。两份同名草稿各自只含
      「目标页旧候选 + 自己这批」，提交时后一份覆盖前一份，先入库的候选
      静默丢失（用户实测 D1 / D8 并入 AI泡沫）。
    ★★ 命名与编译期 `existing:<slug>` 逐字同形：**首次**拉入时草稿叫 `<to>.md`，
      标题是目标页标题，待审批列表与目标路径一眼对得上。**不走 `_unique_slug`**
      —— 它会为了避开已入库页改名，正好改错。已有并入稿若是旧版「原地改判」
      留下的源稿名（`tui-li-...`），就沿用那份文件，不再另造一份。
    ★ 与 `merge_draft_into` 的分工：那个是**整份草稿**并入（源稿归档），
      这个是**只取几条候选**（源草稿要留住）。两条路径共用同一套「已有并入稿
      就追加」的判据与 `_compose_page`，不另写一份产出逻辑。
    ★ 先写盘再 polish：写盘成功而模型失败时两份都带 `stale`，
      `_validate_draft` 会因正文为空拒绝入库、视图也把原因摆出来 —— 不静默。
    ★ 2026-11-22：源稿已声明 `merges` 时**允许拆候选改投别的页**（真需求：
      整页并入 A 的稿里有一条其实属于 B）。但 `edits`/`retire`/`delete` 等
      破坏性方向仍一律拒绝；拆给自己当前的并入目标也拒绝（会被
      `_merge_draft_for` 加回去）。源稿的 `merges` 保留，收缩后照常并入 A。
    """
    from core import wiki_agent as wa          # 函数内 import：避开 core 层循环
    from core import wiki_loop

    from_slug = (from_slug or "").strip()
    target, why = resolve_merge_target(to_slug)
    if why:
        return False, why
    if not wstore._safe_slug(from_slug) or not wstore._safe_slug(target):
        return False, "草稿名或目标页名非法"

    box = wstore.root() / ".drafts"
    src = box / f"{from_slug}.md"
    if not src.exists():
        return False, f"草稿不存在：{from_slug}"

    try:
        raw = src.read_text(encoding="utf-8")
        fm, _body = wstore.parse_front(raw)
    except (OSError, ValueError) as exc:
        return False, f"读草稿 {from_slug} 失败：{exc}"
    key = wstore.draft_direction(fm)
    if key and key != "merges":
        return False, (f"这份草稿已有明确去向（{key}），不能只拆几条候选去并入；"
                       "请对它使用审批浮层的『改为并入』")
    if key == "merges":
        # ★ 草稿已声明整页并入 A，其中一条候选其实属于 B
        #   （某条候选在甲页里，用户要改投乙页）。底层本来就
        #   会收缩源稿 + 两侧重编 + 自动卷入审批，一刀切拒掉等于逼人把错分类的
        #   候选一起并进 A。只放行「改投到另一页」：拆给自己当前的并入目标会让
        #   `_merge_draft_for` 找到源稿自己、把刚拆走的候选又加回去（自相矛盾）。
        own = str(fm.get("merges") or "").strip()
        if own == target:
            return False, (f"这份草稿整页并入的目标就是 {target}，拆候选给它没有意义；"
                           "整页并入请用审批浮层的『改为并入』")

    src_ids = wstore._cand_ids(fm)
    try:
        ids = list(dict.fromkeys(int(i) for i in (cand_ids or [])))
    except (TypeError, ValueError):
        return False, f"cand_ids 必须是候选 id 列表：{cand_ids!r}"
    if not ids:
        return False, "没有指定要并入的候选"
    missing = [i for i in ids if i not in src_ids]
    if missing:
        return False, ("候选 %s 不在草稿 %s 里，挪不了（这页有 %s）"
                       % ("、".join(str(i) for i in missing), from_slug,
                          "、".join(str(i) for i in src_ids) or "无"))
    keep = [i for i in src_ids if i not in set(ids)]
    if not keep:
        # ★ 2026-11-16 用户实测 D87：这条话只说「走浮层的『改为并入』」，可浮层那段
        #   目标候选**只来自模型推荐**（该稿缓存 rows=[]）⇒ 人过去也找不到按钮。
        #   现在浮层底下有个不经过模型的「自己指定一页并入」下拉，话里必须点名，
        #   否则这条错误信息等于把人指向另一面墙。
        return False, ("这份草稿只有这些候选，整页并入请用审批浮层的『改为并入』"
                       "（推荐为空时用浮层底部的『自己指定一页并入』下拉；"
                       "那条路径归档源稿、不新建替换稿）")

    entry = next((p for p in wstore.pages() if p["slug"] == target), None)
    if entry is None:
        return False, f"读不到目标页 {target}"
    live = {c.id: c for c in IngestCandidate.objects.filter(
        id__in=ids, status__in=INGEST_LIVE_STATUS)}

    merge_slug, merge_page = _merge_draft_for(target)
    existed = merge_slug is not None
    if not existed and (box / f"{target}.md").exists():
        return False, (f".drafts/{target}.md 已有一份待审草稿（不是并入稿），"
                       "先处理它再并入（不会悄悄覆盖人没审过的内容）")
    added = ids
    if existed:
        # ★ 第二次并入 = 在已有并入稿里编辑。只追加真正缺的候选；全都有时
        #   不重编正文（没变化），但源稿仍要按本次指定收缩。
        old_ids = set(wstore._cand_ids(merge_page.get("fm") or {}))
        added = [i for i in ids if i not in old_ids]
        text, err = _compose_append(merge_slug, merge_page, ids, live)
        if err:
            return False, err
    else:
        old = _load_page(entry)
        if old is None:
            return False, f"读不到目标页 {target}"
        group = {"cand_ids": ids, "title": old.get("title") or target,
                 "domain": entry["domain"], "tags": [], "slug": target}
        text, _slug, _drop, err = _compose_page(group, live, "", old)
        if err:
            return False, err
        if not text:
            return False, "目标页已经有这些候选，本次没有新增（无需并入）"
        # ★ 拉入 = 新身份：写盘时就发号（用户 2026-11-13：「拉入的时候最好用
        #   新的草稿 ID」）。补号器只兜底历史稿，新产物不靠它。
        text = wstore._set_fm_line(
            text, wstore.DRAFT_ID_FM, str(wstore.next_draft_id()))
        merge_slug = target

    # 源草稿：只改 candidates 一行 + 打 stale，其余 frontmatter 顺序原样保留。
    src_text = wa._mark_body_stale(wa._rewrite_fm(raw, {"candidates": keep}))
    pairs = [(src, src_text)]
    wrote_merge = text is not None
    if wrote_merge:
        pairs.append((box / f"{merge_slug}.md", text))
    ok, err = _write_drafts_atomic(pairs)
    if not ok:
        log.error("候选并入写盘失败 %s → %s：%s", from_slug, target, err)
        return False, err

    # ★ 顺序：先重编「候选减少的」源页，再重编「候选新增的」并入稿 —— 顺序
    #   反了时并入稿的 polish 会引用还没收缩的源页旧正文（与 _set_split 同契约）。
    polished, failed = [], []
    for s in [from_slug] + ([merge_slug] if wrote_merge else []):
        good, perr = wiki_loop._polish_one(s)
        (polished if good else failed).append(s if good else f"{s}: {perr}")
    wstore.log_append("attach-to-page", f".drafts/{merge_slug}.md",
                      f"{from_slug} 的候选 {'、'.join(str(i) for i in ids)} → {target} · "
                      f"源页留 {len(keep)} 条 · "
                      + ("已有并入稿追加" if existed else "新建并入稿")
                      + f" · 结论重编 {len(polished)}/{1 + int(wrote_merge)}")
    if failed:
        log.error("候选并入后结论重编未完成 %s → %s：%s",
                  from_slug, target, "；".join(failed))
    return True, {"from": from_slug, "to": target, "draft": merge_slug,
                  "existing": existed, "moved": ids, "added": added,
                  "kept": keep, "polished": polished, "polish_failed": failed}



def _page_slugs() -> set:
    """已入库页的 slug 集合（`wiki/**/<slug>.md` 的 stem）。"""
    return {p.stem for p in (wstore.root() / "wiki").rglob("*.md")}


def _unique_slug(slug: str, page_slugs=None) -> str:
    """同名不覆盖：既避开 `.drafts/` 里的草稿，也避开 `wiki/` 里已入库的页。

    ★ 覆盖等于静默删掉人可能已经润色过的草稿。
    ★ 为什么也要看 `wiki/`：同名页已入库时，草稿再叫这个名字，`wstore.approve()`
      会以「已存在同名页」拒收 —— 那一整轮编译的钱白花，人还卡在一份进不去的草稿上。
    ★ 「并入已有页」不走这里（阶段 2.3d）：那种草稿**必须**与目标页同名，靠
      `merges` 声明去覆盖，改名反而是错的。走到这里的都是 S1 判定「该新建」的组，
      撞名说明它和已有页抢同一个名字 —— 加后缀只是兜底，调用方会把它报出来。
    """
    taken = _page_slugs() if page_slugs is None else page_slugs
    box = wstore.root() / ".drafts"

    def _free(s: str) -> bool:
        return not (box / f"{s}.md").exists() and s not in taken

    if _free(slug):
        return slug
    for i in range(2, 100):
        cand = f"{slug}-{i}"
        if _free(cand):
            return cand
    return f"{slug}-{uuid.uuid4().hex[:4]}"


# ---------------------------------------------------------------- 第十一步：一条龙
def extract_then_compile() -> dict:
    """点「编译」的一条龙：先提炼所有未提炼的新痕迹，再编译成草稿。

    提炼对用户隐身（第十一步）：用户只看到「M 处新痕迹」和一枚「编译」按钮，
    点了之后由本函数把「痕迹 → 候选 → 草稿」一次做完，中间过程不露脸。
    本函数只做**编排**，复用 `extract()` / `_compile_locked()`，不新写模型调用。

    返回形状 = `_compile_locked` 的超集，多五个键：
      extracted        : int   本次真正跑过提炼的来源数（created=0 也算，那是合法结论）
      extract_dropped  : int   提炼阶段被引用守卫丢掉的候选总数（**不静默**）
      extract_skipped  : list  提炼失败的来源 [{source_type, source_id, title, err}]
      extract_left     : int   这一轮**没轮到**的来源数（超过 `EXTRACT_MAX_SOURCES`）
                               ★ 2026-11-09 补：上限必须说出来，否则「点了没反应、
                                 痕迹还在」会被当成 bug，或者干脆没人发现。
      extract_cut      : int   提炼输出被预算硬截断的来源数（**这批下次会重提炼**）
                               ★ 2026-11-09 补：截断的这次不记台账，所以它既不算
                                 `extracted` 也不算「丢弃」—— 不单独报出来，人就只
                                 看到「痕迹还在，但点了一轮没变化」。

    ★ 三条边界（docstring 与单测一起钉死）：
      1. **审批未完成时拒绝新一轮**（2026-11-09 用户澄清）：草稿待审批时不该开
         新一轮提炼+编译，否则草稿越堆越多、审批上下文被打散。编译中由锁挡，
         编译后待审批由 approval_active 挡 —— 两者分开，不冲突。拿到锁就把
         提炼+编译跑完；运行期间由 mutex（compile_state）挡审批/编辑写。
      2. 单个来源失败**不中断整轮**（字数不够 / 预算用尽是那一个来源的事，记进
         `extract_skipped` 继续）；「编译锁拿不到」是整轮级失败，当场返回。
      3. 提炼阶段的 dropped 必须汇总。只报「提炼 5 处、编出 3 页」会让人以为
         5 处的候选全进了编译，守卫丢掉的那几条不能没人知道。

    ★ 锁结构（选 A）：本函数自己拿编译锁，**直接调 `_compile_locked()`**，
      不经过 `compile_candidates()` —— 后者内部还会再拿一次同一把非阻塞锁，
      那样会「自己挡自己」，整轮白跑。代价：`try/finally` 的清状态放锁要在这里
      复刻一份，顺序必须与 `compile_candidates` 一致（先清状态，再放锁）。
      否决 B（不拿锁、先 extract 再调 compile_candidates）：extract 阶段不受锁
      保护，两个标签页同时点会并发跑两遍提炼，双倍花钱。

    ★ 为什么拿锁后**立刻**写状态（技术设计补漏）：`compile_state()` 是「先读
      `meta/compile.running.json`、文件不在就返回空闲」，压根不探锁。提炼也要
      好几分钟，不在这时写状态，这一整段都会被页面读成「空闲」，硬锁方向二
      （编译中挡审批/编辑）就形同虚设。状态用 `_mark_running` 的 merge 语义：
      这里写 phase=extract，`_compile_locked` 算完 backlog 再补 n/skipped/source，
      开始时间由 `setdefault` 保持第一次的值。
    """
    empty = {"drafts": [], "groups": [], "dropped": [], "skipped": 0,
             "extracted": 0, "extract_dropped": 0, "extract_skipped": [],
             "extract_left": 0, "extract_cut": 0}

    # ★ 审批未完成时拒绝新一轮（2026-11-09 用户澄清）：草稿待审批时不该开新一轮
    #   提炼+编译，否则草稿越堆越多、审批上下文被打散。编译中由锁挡，编译后
    #   待审批由 approval_active 挡 —— 两者分开，不冲突。
    from core import wiki as wstore
    if wstore.approval_active():
        return dict(empty, ok=False, err="审批进行中（有草稿待裁决），先跑完审批再编译",
                    paused=True)

    # 1. 整轮级锁：拿不到就一句话回，不排队、不静默
    fd = _try_lock_compile()
    if fd is None:
        return dict(_compile_busy_result(), extracted=0,
                    extract_dropped=0, extract_skipped=[])

    extracted = 0
    extract_dropped = 0
    extract_cut = 0
    extract_skipped = []
    try:
        _mark_running(phase="extract", n=0)
        # ★ 只取一次快照：逐个 extract 会改台账（state → done），循环里重查
        #   inventory 会让「这一轮到底该处理哪些来源」边跑边变。
        # ★★ 必须 `limit=None`（2026-11-09）：默认 30 会被当成本轮的上限，
        #   超出的来源静默不提炼。上限改由 `EXTRACT_MAX_SOURCES` 显式表达，
        #   差额如实进 `extract_left`。
        all_todo = [r for r in inventory(limit=None) if r["state"] in ("new", "changed")]
        todo = all_todo[:EXTRACT_MAX_SOURCES]
        extract_left = len(all_todo) - len(todo)
        for r in todo:
            res = extract(r["source_type"], r["source_id"])
            if not res.get("ok"):
                extract_skipped.append({
                    "source_type": r["source_type"], "source_id": r["source_id"],
                    "title": r.get("title") or "", "err": str(res.get("err") or "提炼失败")})
                log.warning("wiki 一条龙跳过一个来源 %s:%s：%s",
                            r["source_type"], r["source_id"], res.get("err"))
                continue
            # ★★ 截断的这次**不记台账**（见 `extract()`），所以它既不该算进
            #   `extracted`（那会让人以为这篇处理完了），也不该混进 `extract_skipped`
            #   （那不是失败，只是内容太长）—— 单独计数，并在反馈里明说会重提炼。
            if res.get("truncated"):
                extract_cut += 1
                extract_dropped += len(res.get("dropped") or [])
                continue
            # created=0 是合法结论（这篇没有可沉淀的判断），照样算处理过：
            # 台账已更新，下次不会重复花钱。
            extracted += 1
            extract_dropped += len(res.get("dropped") or [])

        # 2. 编译：phase 跟着换；_compile_locked 内部再 merge 进 n/skipped/source
        _mark_running(phase="compile")
        result = _compile_locked()
        # ★ 提炼跑完却一条候选都没有 = 合法结论（这些痕迹没有可沉淀的判断），
        #   不是「编译未完成」。直接调 compile_candidates 的空跑仍报 ok=False
        #   （那是 caller 没活找活），一条龙这里必须如实转绿，否则按钮对新痕迹
        #   开放后，「提炼了但 0 条」会被误报成失败。
        if result.get("no_candidates"):
            result["ok"] = True
            result["err"] = ""
            result["msg"] = ("这些痕迹里没有可沉淀的判断（0 条候选），没生成草稿"
                             if extracted else "没有新痕迹，也没有待编译的候选")
        result["extracted"] = extracted
        result["extract_dropped"] = extract_dropped
        result["extract_skipped"] = extract_skipped
        result["extract_left"] = extract_left
        result["extract_cut"] = extract_cut
        return result
    finally:
        # ★ 顺序不能反：先清状态、再放锁（与 compile_candidates 同一份理由）。
        _clear_state()
        _unlock_compile(fd)
