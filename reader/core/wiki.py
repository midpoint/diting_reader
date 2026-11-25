"""wiki 层：目录骨架 + 草稿审批 + index/log/反向映射维护（阶段 0：零 LLM、零模型表）。

四层落盘：

    raw/        原文快照（★ 首次对话/划线/批注时才写，star 不写）
    .drafts/    AI 编译初稿，等人审批（审批 = 移进 wiki/，就是 Gate 1）
    wiki/       已审批的原子概念页，按领域软分区 `wiki/<domain>/<slug>.md`
    schema.md   页面规范（= 给编译器的 CLAUDE.md）
    index.md    master index（每页一行摘要：人读目录、机器读路径）
    log.md      `## [日期 时间] 动作 | 对象` 稳定前缀，机器可解析
    meta/sources.json  来源 → 受影响页 的反向映射

★ 为什么阶段 0 不建模型表（WikiSource / Highlight / Conversation …）：
  先用纯文件 + 手写草稿把「审批 → 入 wiki → 更新 index → 记 log」这条链跑通。
  表要在「真的出现了需要交互的产物」时才建（阶段 1 的对话），否则是照想象建字段，
  等真实需求长出来一定不一样，再改就要写迁移。

★ 为什么是文件不是数据库：
  wiki 页要能被 Obsidian 直接打开、被 git diff、被 grep、被你整个拿走。
  塞进 SQLite 就把这个生态全丢了 —— 而那个生态正是 wiki 形态的全部价值。

★ 机器可解析是硬约束，不是建议（阶段 0 的 0.4–0.6）：
  - index.md 一行一页、带页相对路径；
  - log.md 前缀固定 `## [YYYY-MM-DD HH:MM] <action> | <target>`；
  - 页 frontmatter 必带 sources / updated / confidence。
  靠"AI 自觉"的格式活不过三次重构，所以这些要在 `approve()` 里**校验**。

★ 本模块只碰文件，不碰 LLM、不碰 DB —— 所以能单测、能离线跑、能被脚本直接调。
"""
import difflib
try:
    import fcntl
except ImportError:  # Windows 无 fcntl，用跨平台兼容层
    from core import flock_compat as fcntl
import hashlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from django.conf import settings
from django.utils import timezone

# ★ 内链语法（含别名）只认 mdlines 一份：反链索引在这边、阅读态渲染在 mdrender，
#   两边各抄一个正则迟早漂（第五步 5c 设计稿就点名过这个隐患）。
from core.mdlines import WIKILINK as _WIKILINK_RE
from core.mdlines import wikilink_parts as _wikilink_parts

log = logging.getLogger(__name__)

#: 子目录（相对 wiki 根）。`.discarded` 藏在 .drafts 里：草稿的垃圾桶不该出现在
#: 「待审批」列表里，也不该和已入库的页混在一起。
SUBDIRS = ("raw", ".drafts", ".drafts/.discarded", "wiki", "meta")

#: frontmatter 必填字段 —— 缺任何一个 `approve()` 直接拒。理由见模块头：
#: sources 断了反向映射、updated 断了「这条结论是哪天的」、confidence 断了 lint 的排序。
#: ★ 2026-11-05 加 `tags` / `candidates`：页面即分组，`candidates` 是分组的唯一
#:   真值，缺它 = 空页；`tags` 是多值标签，字段必须在（值可以是空列表）。
REQUIRED_FM = ("title", "domain", "sources", "updated", "confidence",
               "tags", "candidates")

#: 这几个字段「键在即可、值允许为空容器」：`tags: []` 是合法的无标签页，
#: 用 `not fm.get(k)` 判会把它误判成缺字段 —— 守卫过严比漏一条更贵。
FM_KEY_ONLY = ("tags",)

CONFIDENCE_OK = ("high", "medium", "low")

#: 第十步「删页提案」的瞬时 frontmatter 字段。**只在草稿里有意义**，入库时一律剥掉：
#: `retire` = 删除稿里真正要废弃的候选集合；`retire_from` = 这份搬迁稿来自哪一页。
#: 留在库里会被下一次提案的守恒守卫扫到，把一份已经定稿的页误认成待审搬迁稿。
#: ★ 与 `edits` / `merges` 同属「意图标记」，判据收在这一份常量里，别各写一遍。
RETIRE_FM_KEYS = ("retire", "retire_from")

#: ★ 草稿「已有明确去向」的 frontmatter 键（2026-11-11）。一处定义、三处共用：
#:   ① 审批浮层要不要显示「可能相关的已入库页」；② 改判为并入能不能做；
#:   ③ LLM 推荐要不要跑。少一个入口就会给「覆盖一页已确认知识」多开一道门。
#: ★ `delete` 是 `edits` 的空形态（见 `is_delete_fm`），通常不直接写在 fm 里；
#:   仍列上，是为了老草稿 / 手工改过的稿不被漏判。
DRAFT_DIRECTION_KEYS = ("merges", "edits", "delete", "retire", "retire_from")


def draft_direction(fm: dict) -> str:
    """这份草稿有没有明确去向 → 第一个非空的标记名；普通新页稿返回 ""。

    ★ 返回**键名**而不是 bool：三处调用方都要把「为什么不给这个入口」说给人听，
      各自再循环一遍判据必然漂。
    """
    for key in DRAFT_DIRECTION_KEYS:
        if str((fm or {}).get(key) or "").strip():
            return key
    return ""


# ------------------------------------------------------------------ draft 正文形态（第十三步 A）
#: ★ 2026-11-08 第十三步（用户拍板 A）：正文 = **纯 polish 一段**，不写任何结构标题。
#: `BODY_HEAD` / `POLISH_HEAD` 从「写入判据」**降级为只读兼容判据**：只用于认清零前
#: 的老页 / 旧双层草稿（`split_two_layer`），任何写回路径都不许再产出这两个字形。
#: 判据仍收在这一处：老页回退、静态守卫、读时兼容共用，别各写一遍。
BODY_HEAD = "## 候选清单"      # 只读兼容：旧双层的权威层标题（不再写入）
POLISH_HEAD = "## 通顺话"      # 只读兼容：旧双层的投影层标题（不再写入）

#: ★ 分组改完、结论还没重编成功时的**瞬时 frontmatter 标记**（第十三步 ⚠️M）。
#: 不再插进正文：A 形态下正文是纯 polish，`> ⚠️` 会污染结论。三处口径收在这里：
#:   写 = `wiki_agent._mark_body_stale()`；剥 = `set_polish()` 真重编成功时；
#:   入库前 = `_strip_retire_fm()` 一律剥掉，机器字段不留在人读的页里。
#: `read("draft")["body_stale"]` 据此判，审批浮层给出**可见提示**；polish 失败时
#: 标记仍在（出错时谁会知道 = 打开浮层就看见），不是静默吞掉。
STALE_FM = "stale"

#: ★ 草稿的稳定身份编号（2026-11-13 用户拍板：位置编号 D1/D2/D3 随合并/拆分跳变，
#: 改成类比候选自增 id 的 `draft_id`）。它只在草稿阶段有意义：合并保留目标稿的号、
#: 拆分新页拿新号、改名/重排都不跳变；入库时剥掉（页用 slug 定位，不需要工作区编号）。
#: 分配/补齐见 `ensure_draft_ids()`，计数器落 `meta/draft_seq.json`（flock、永不重号）。
DRAFT_ID_FM = "draft_id"

#: ★ 结论重编失败的具体原因（机器字段，2026-11-22）：失败时落进草稿 frontmatter，
#:   让「带反馈的重试」跨请求/跨重启仍能拿到上次为什么被拒（死引用 slug 这类）。
#:   提示词承诺 ≠ 程序事实——「下次别再引用不存在的页」只是提示，得把**这次的
#:   具体错误**写进下一次的 messages 才算反馈。成功即剥（与 `stale` 同一次写盘），
#:   入库前也剥，机器字段不留在人读的页里。
POLISH_ERR_FM = "polish_error"

#: 入库前一律剥掉的机器字段（`_strip_retire_fm` 按行删，不重序列化）。
TRANSIENT_FM_KEYS = RETIRE_FM_KEYS + (STALE_FM, DRAFT_ID_FM, POLISH_ERR_FM)

#: 清零前老草稿的正文里可能还带着这两条提示：read() 兼容识别，**不再写入**。
STALE_MARK = "> ⚠️ 分组已由审批对话调整"
STALE_NOTE = ("> ⚠️ 分组已由审批对话调整：本页正文仍是上一版分组，可能含已移走的候选。\n"
              "> 当前真相是 frontmatter 的 `candidates` 与「候选清单」段；通顺话待重生成。")


def is_delete_fm(fm: dict) -> bool:
    """这份 frontmatter 是不是「删页稿」：`edits: <slug>` 且候选被搬空。

    ★ 删页不是新动词，是 `edits` 的**空形态**（第八步）：
        edits:<slug> + candidates 非空 = 覆盖那页
        edits:<slug> + candidates 为空 = 用空替换那页 = 删页
      判据只此一份：`missing_fm`（豁免空候选）、`_validate_draft`（走删除分支）、
      `list_drafts`（浮层显示「删除」）都调它。分开写过一次就漏一次。
    """
    return bool(str(fm.get("edits") or "").strip()) and not _cand_ids(fm)


def missing_fm(fm: dict) -> list:
    """frontmatter 缺字段清单 —— `list_drafts()` / `approve()` 共用，判据只此一份。

    ★ 同口径原先在两处各写一遍，改一处漏一处就会「面板说完整、审批却拒收」。
    ★ `candidates: []` 判缺（空页必须拦），`tags: []` 不判缺（合法空标签）。
    ★ 唯一豁免：**删页稿**（`edits` + 空候选）。它的空候选不是「空页」，而是
      「这页不要了」—— 拦掉它等于删页永远入不了库，而拦的理由（空页）并不成立。
    """
    skip = {"candidates"} if is_delete_fm(fm) else ()
    return [k for k in REQUIRED_FM
            if k not in skip
            and ((k not in fm) or (k not in FM_KEY_ONLY and not fm.get(k)))]


#: 受控 domain 清单的唯一真值：项目根 `Meta/ontology.md` 的「领域」表。
#: 用 __file__ 定位而不是 settings —— 模块导入阶段就要能解析，不依赖配置加载顺序。
ONTOLOGY_PATH = Path(__file__).resolve().parents[2] / "Meta" / "ontology.md"
_ONTOLOGY_TAG_RE = re.compile(r"`#([A-Za-z0-9_-]+)`")

#: 进程内缓存：{路径: (stat 指纹, 内容 sha256, domain 元组)}。
#: ★ 只缓存**解析成功**的结果：坏清单一旦被缓存，本体改好了还一直返回旧坏值，
#:   而且查不出为什么 —— 静默失败最贵（见 ontology_domains() 的失败分支）。
#: ★ 不加锁：字典读写在 CPython 下是原子的，并发最坏是各自重算一次同样的结果，
#:   幂等且无正确性问题；为这个给每次调用套一把锁反而把它拖成串行。
_ONTOLOGY_CACHE = {}


def _ontology_fingerprint(p: Path):
    """本体「有没有可能变过」的廉价指纹 —— 只 stat，不读内容（实测约 1us）。

    ★ `ctime` 是关键，不能省：`os.utime()` 能把 mtime 精确回拨到纳秒原值，
      但回拨这个动作本身由内核把 ctime 推到当前时刻，**ctime 回拨不了**。
      实测：同 inode、同长度、内容换掉、mtime 回拨原值 ⇒ mtime/size/ino
      三项全同，只有 ctime 变了。少了 ctime，这种「看着没动其实换了内容」
      就会静默命中旧缓存，而且没人知道。
    """
    try:
        st = p.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _parse_domain_lines(text: str) -> list:
    """从本体全文抽出「领域」表的 `#xxx`。抽成独立函数，缓存命中时才能被计数反证。

    ★ 只认「领域」段里带反引号的表格行 `` `#xxx` ``：正文里其它 `#` 标签
      （概念标题、说明文字）不会混进来。
    """
    in_domains, out = False, []
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("## "):
            in_domains = s.startswith("## 领域")
            continue
        if not in_domains or not s.startswith("|"):
            continue
        m = _ONTOLOGY_TAG_RE.search(s)
        if m:
            v = m.group(1).strip().lower()
            if v and v not in out:
                out.append(v)
    return out


def ontology_domains(path=None) -> list:
    """解析 `Meta/ontology.md` 的「领域」表，返回受控 domain 枚举。

    ★ 判据只此一份：domain 是**受控分类法（单继承）**，清单在 schema 文件里，
      不硬编码进代码。提炼 / 编译 / 审批都调它；模型不许凭空造清单外的域。
    ★ 本体改动极低频 ⇒ 结果在进程内缓存；但**每次调用仍会 stat 校验，
      文件一变立即重读重算，没有 TTL、不用重启**。实测 3.42ms 里 3.26ms 是
      读文件、0.015ms 才是解析：只按 sha 缓存而照旧读文件等于没优化，所以
      用 stat 指纹挡掉「确定没变」的读，sha 只在指纹变了时做二次确认：
        - sha 没变（只是 touch / 原样重写）⇒ 沿用旧结果，只更新指纹；
        - sha 变了 ⇒ 重新解析并替换缓存，log 一条清单变更。
    ★ 解析失败 / 文件缺失 / 非法 utf-8 / 空清单 ⇒ 返回 [] 且**清掉缓存**，
      由调用方显式报错，不静默放行任意 domain，也不让旧坏值卡住下一次调用。
    ★ 返回的是副本：调用方 append / 改列表不许污染缓存。
    `path` 只给测试用（指向临时坏文件）；默认走 ONTOLOGY_PATH。
    """
    p = Path(path) if path is not None else ONTOLOGY_PATH
    key = str(p)
    hit = _ONTOLOGY_CACHE.get(key)
    fp = _ontology_fingerprint(p)
    if fp is not None and hit is not None and hit[0] == fp:
        return list(hit[2])

    try:
        data = p.read_bytes()
    except OSError:
        log.warning("读不到本体文件 %s", p)
        _ONTOLOGY_CACHE.pop(key, None)
        return []
    digest = hashlib.sha256(data).hexdigest()
    # 读到内容后再取一次指纹：缓存标的是「实际读到的这一版」。
    # 若 read 期间文件被改动，挂旧指纹只会让下次多读一次（无害但脏）。
    fp = _ontology_fingerprint(p)
    if hit is not None and hit[1] == digest:
        _ONTOLOGY_CACHE[key] = (fp, digest, hit[2])
        return list(hit[2])
    try:
        domains = _parse_domain_lines(data.decode("utf-8"))
    except UnicodeDecodeError:
        log.warning("本体文件不是合法 utf-8：%s", p)
        _ONTOLOGY_CACHE.pop(key, None)
        return []
    if not domains:
        log.warning("本体 %s 没解析出任何 domain", p)
        _ONTOLOGY_CACHE.pop(key, None)
        return []
    _ONTOLOGY_CACHE[key] = (fp, digest, tuple(domains))
    if hit is not None and hit[2] != tuple(domains):
        log.info("本体变更，受控 domain 已刷新：%s → %s", list(hit[2]), domains)
    return domains


# ------------------------------------------------------------------ 落盘骨架
INDEX_SEED = """# Wiki 索引

> 每页一行摘要，由谛听维护。**要改内容请改页，不要手改这里** ——
> 下次审批会按页的 frontmatter 重写对应行。
> 文件多到装不下上下文时（实测 150–200 页是上限），先读这份索引再决定打开哪几页。

"""

LOG_SEED = """# Wiki 操作日志

> 机器可解析，格式固定：`## [YYYY-MM-DD HH:MM] 动作 | 对象`。
> 动作：`init` / `approve` / `discard` / `sources`。不要改这一行的形状。

"""

SCHEMA_SEED = """# Wiki 页面规范（schema）

> 这份文件就是「给编译器的说明书」：AI 写页、你审批，都照这里。
> 它是 wiki 的 CLAUDE.md —— 改了它，下一轮编译立刻按新规范走。

## 一条页 = 一个原子概念

- 不是一篇文章、不是一个 star、不是一次对话。**一篇文章可能给 5 个概念页各添一句。**
- 判据：这页能独立回答「X 是什么 / 怎么办 / 我怎么看」。
- 一页只留**一个判断**；判断变了就改它并更新 `updated`，不要在一个页里攒着
  「以前认为…现在认为…」——那会让 lint 找不出哪句还成立。

## frontmatter（缺一不可，`approve()` 会校验）

```
---
title: 融资与资本开支
domain: invest
sources: [entry:4907, web:2026-10-31-a1b2]
updated: 2026-10-31
confidence: medium
tags: []
candidates: [12, 13]
---
```

- `title`：中文标题（页里用；文件名 slug 用 ASCII kebab）。
- `domain`：决定落在 `wiki/<domain>/`，取值与 `Meta/ontology.md` 的域标签一致。
- `sources`：这条判断从哪来。★ **反向映射 `meta/sources.json` 由它建**
  （来源 → 受影响页），删了它就断链：来源变了也没人知道该复查哪页。
- `updated`：最后一次被修改的日期（不是创建日期）。
- `confidence`：`high` / `medium` / `low`。`low` 的页在 lint 时优先复查。
- `tags`：自由多值标签，可以是空列表 `[]`（字段必须在）。
- `candidates`：这一页由哪些候选编译而来。**页面即分组**，它是分组的唯一真值；
  缺它或为空就是空页，`approve()` 直接拒。

## domain 与 tag（受控分类法 + 自由标签）

- `domain` 是**受控分类法（单继承）**：取值只能来自 `Meta/ontology.md` 的「领域」
  表（清单由 `core.wiki.ontology_domains()` 解析，代码里不硬编码）。编译时 AI
  参考已有 domain 给建议，**不许凭空造清单外的域**；清单外 → AI 只给「新增
  domain」的建议，**人确认并应用后**才晋升（**默认不新增**）。审批、入库后
  编辑环节都可调整。
- `tags` 是**自由多值标签**：AI 建议、审批可增删，不受清单约束。
- 两者正交：domain 决定「它属于哪个领域」（唯一），tag 决定「它还有什么横切
  属性」（多个）。「既 ai 又投资」→ 主 domain 选一个（如 `invest`），另一个
  作 tag（如 `ai`）。别用三套东西表达同一件事。

## 精确数据不进散文

数字、日期、价格、比例落 frontmatter 字段或 `meta/` 下的结构化文件，**正文只引用**。
散文里的数字必然漂移 —— 反过来查都查不出来（这条是拿日报 `number_audit` 的
真实事故换来的：208 家被写成 209，全库搜不到出处）。

## 只写「我的理解」

AI 的摘要放 `raw/` 或草稿里，**不进已审批页**。理由：半年后 AI 摘要一文不值
（它本来就是从别人的文章里二次压缩出来的），而「我为什么这么判断」才是资产。
"""


def root() -> Path:
    """wiki 根目录。走 settings.WIKI_DIR（可用 .env 的 WIKI_DIR 指到 Obsidian vault）。"""
    return Path(settings.WIKI_DIR)


def root_label() -> str:
    """目录的**可展示**形式：只给相对项目根的那一段，绝不回绝对路径。

    ★ 起因（2026-11-16，用户报）：首页把 `/Users/<用户名>/Documents/.../data/wiki`
      整条打出来了。页面在本机看没问题，但它会被截图、会被投到投影仪上、会被
      贴进聊天 —— 路径里带的是机器上的用户名与目录结构，属于与内容无关的泄露。
    ★ 不做字符串截断（比如替换 `Path.home()`）：换机器、换用户、WIKI_DIR 指到
      U 盘就失效，而且「截掉一半」很容易留下 `…/Documents/git/...` 这种还是能认人
      的残段。判据只认**一件事**：这个目录在不在项目根之内。
      - 项目内 → 相对段（`data/wiki`），说明「东西在哪」足够，且不含家目录；
      - 项目外（自定义 WIKI_DIR，如 Obsidian vault）→ 只给「自定义目录」四个字。
        ★ 这里**故意不显示路径本身**：vault 往往在 `~/Documents/我的笔记` 这类
          更能认出人的位置上，比默认路径更该藏。真实路径仍可从 `.env` 查到，
          页面上不需要它。
    """
    try:
        project = Path(getattr(settings, "PROJECT_ROOT", settings.BASE_DIR.parent))
        return root().resolve().relative_to(project.resolve()).as_posix()
    except (ValueError, OSError, AttributeError):
        return "自定义目录"


def ensure_dirs() -> dict:
    """幂等建目录 + 首次写种子文件（**已存在的一律不覆盖**）。

    ★ 幂等是硬要求：app 启动、每次审批、脚本里都会调，重复调用不能冲掉内容。
    """
    r = root()
    created = []
    for d in SUBDIRS:
        p = r / d
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            created.append(d)
    legacy_seeds = (
        ("index.md", INDEX_SEED),
        ("log.md", LOG_SEED),
        ("schema.md", SCHEMA_SEED),
        ("meta/sources.json", "{}\n"),
    )
    for name, text in legacy_seeds:
        p = r / name
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
            created.append(name)
    if created:
        log.info("wiki 骨架初始化：%s", ", ".join(created))
        # 只在**首次**（目录刚建出来）记 init，避免每次 ensure 都往日志里灌
        if not _has_log_action("init"):
            log_append("init", "-", f"创建 {', '.join(created)}")
    return {"root": str(r), "created": created}


# ------------------------------------------------------------------ frontmatter
#: 只支持 `key: value` / `key: [a, b]` / `key:` + 紧跟 `- item` 列表。
#: ★ 刻意不引 PyYAML：为了解析这么点结构加一个依赖不值得（依赖就是技术债），
#:   而且我们的 frontmatter 是自己写的、格式可控，不需要 YAML 全集。
_FM_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.S)


def parse_front(text: str) -> tuple:
    """返回 (frontmatter dict, 正文)。没有 frontmatter 时返回 ({}, 原文)。"""
    m = _FM_RE.match(text or "")
    if not m:
        return {}, text or ""
    data, key = {}, None
    for raw in m.group(1).splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            if key:
                if not isinstance(data.get(key), list):
                    data[key] = []
                data[key].append(stripped[2:].strip().strip("'\""))
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        key = k.strip().lower()
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            data[key] = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
        elif v:
            data[key] = v.strip("'\"")
        else:
            data[key] = []   # 值在下面的 `- item` 行里
    return data, text[m.end():]


def _cand_ids(fm: dict) -> list:
    """frontmatter 的 `candidates:` → int 列表（脏值跳过，不猜）。

    ★ 为什么收在这里：草稿和已入库页都靠它回答「这页由哪些候选编译而来」。
      编译的幂等判据要同时读这两处（见 `ingest._compiled_ids`），口径必须一份 ——
      各写一遍迟早松紧不一，症状是「草稿阶段认得出、入库之后就认不出」。
    """
    out = []
    for i in fm.get("candidates") or []:
        try:
            out.append(int(str(i).strip()))
        except (TypeError, ValueError):
            continue
    return out


def _retire_ids(fm: dict) -> list:
    """删除稿 frontmatter 的 `retire:` → int 列表（脏值跳过）。

    ★ 判据只此一份：`request_delete` 写、`_validate_draft` 读、提案面板读、
      守恒守卫读，都调它。分成两处写就迟早出现「面板说全废、commit 却按原页
      全量废弃」——而这两者在默认路径上恰好同值，测试不专门造「留一条」的
      场景根本发现不了。
    ★ 「键在但值为空」与「键不在」是两件事：`retire: []` = 全部搬走（合法），
      键不在 = 第八步的旧格式删除稿（由 `_validate_draft` 显式回退并说出来）。
    """
    out = []
    for i in fm.get("retire") or []:
        try:
            out.append(int(str(i).strip()))
        except (TypeError, ValueError):
            continue
    return out


def _safe_slug(slug: str) -> bool:
    """文件名白名单：不许路径分隔符、不许以点开头（挡 `..` 与隐藏文件）。

    ★ 不强制 ASCII：手写草稿用中文名也能过，只是导出到 Obsidian / URL 时更麻烦。
      slug 就是文件名，**唯一的安全底线是不能逃出 wiki/ 目录**。
    """
    s = (slug or "").strip()
    return bool(s) and "/" not in s and "\\" not in s and not s.startswith(".")


#: 合并前旧页的备份目录（相对 wiki 根）。★ **不进 SUBDIRS**：升级不该让已有库凭空
#: 多一个空目录（`verify_wiki` 断言 SUBDIRS 全在，但反向不成立）；用到才建。
MERGED_DIR = ".drafts/.merged"


def find_page(slug: str):
    """按 slug 在 `wiki/` 下找唯一一页。返回 (path, why)；找不到 / 重名都返回 (None, 原因)。

    ★ 重名必须当错误，不能挑一个：`wiki/invest/x.md` 与 `wiki/tech/x.md` 同时存在时，
      「merges: x」指哪一页是**不可判定**的 —— 猜一个等于静默改错别人的知识。
    """
    want = (slug or "").strip()
    if not want:
        return None, "合并目标为空"
    hits = [p for p in (root() / "wiki").rglob("*.md") if p.stem == want]
    if not hits:
        return None, f"合并目标不存在：{want}（草稿的 merges 指向一页不存在的页）"
    if len(hits) > 1:
        rels = "、".join(p.relative_to(root()).as_posix() for p in hits)
        return None, f"合并目标重名：{want} 命中多页（{rels}），不知道该改哪一页"
    return hits[0], ""


def _merge_backup(dst: Path, slug: str) -> Path:
    """覆盖前把旧页原样存一份。

    ★ 覆盖是唯一会让人「上周确认的结论不见了」的动作，所以这里不压缩、不改写，
      就是一份能直接 diff 的原文。备份失败就不许覆盖（宁可让审批失败）。
    """
    box = root() / MERGED_DIR
    box.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = box / f"{slug}-{stamp}.md"
    bak.write_text(dst.read_text(encoding="utf-8"), encoding="utf-8")
    return bak


def _strip_fm_key(text: str, key: str) -> str:
    """去掉 frontmatter 里的一行（正文原样保留，不重新序列化）。

    ★ `merges:` 只对「这次合并」有意义。带着它入库，下次编译扫到这一页会以为
      它还在等合并，于是又生成一份覆盖草稿 —— 一个字段引发的回环。
    """
    m = _FM_RE.match(text)
    if not m:
        return text
    lines = m.group(1).splitlines()
    kept = [ln for ln in lines if not re.match(rf"^{re.escape(key)}\s*:", ln)]
    if len(kept) == len(lines):
        return text
    return "---\n" + "\n".join(kept) + "\n---\n" + text[m.end():]


def _strip_retire_fm(text: str) -> str:
    """入库前剥掉**所有瞬时意图标记**（同 `_strip_fm_key` 的纪律：按行删，不重序列化）。

    ★ 搬迁稿一旦入库，`retire_from` / `retire` 就不再是「待审意图」而是脏字段；
      `stale`（第十三步）是「结论还没重编成功」的瞬时标记，更不该留在人读的页里。
      与 `edits` 留在库里会让编译链回环是同一类问题，只是更安静。
    """
    for key in TRANSIENT_FM_KEYS:
        text = _strip_fm_key(text, key)
    return text


def _set_fm_line(text: str, key: str, value: str) -> str:
    """在 frontmatter 里设置 `key: value`：已有就替换那一行，没有就在末尾追加。

    ★ `_strip_fm_key` 的对称操作。都走「按行替换、绝不重新序列化」：页面里还有
      `facts:` / 多行列表等编译链写下的行，整段重写会把它们洗掉且不报错。
    """
    m = _FM_RE.match(text)
    if not m:
        return text
    lines = m.group(1).splitlines()
    pat = re.compile(rf"^{re.escape(key)}\s*:")
    for i, ln in enumerate(lines):
        if pat.match(ln):
            lines[i] = f"{key}: {value}"
            break
    else:
        lines.append(f"{key}: {value}")
    return "---\n" + "\n".join(lines) + "\n---\n" + text[m.end():]


def _set_cands_empty(text: str) -> str:
    """把 frontmatter 的 `candidates` 整段换成 `candidates: []`。

    ★ 不能直接用 `_set_fm_line`：它只替换 `candidates:` 那一行，**多行列表的续行
      （`- 11` / `  - 11`）会留在文件里** —— 于是 frontmatter 变成
      `candidates: []` 后面跟几个孤儿 `- 11`，解析器当成别的东西或直接忽略，
      而「页已经被删空」这件事在文件里看不出来。
    ★ 只清 `candidates` 这一段，其它行了不动（同 `_strip_fm_key` 的纪律：
      按行改，绝不重新序列化 —— `facts:` / `merges:` 等机器行不能被洗掉）。
    """
    m = _FM_RE.match(text)
    if not m:
        return text
    out, dropping = [], False
    for raw in m.group(1).splitlines():
        s = raw.strip()
        if re.match(r"^candidates\s*:", s):
            out.append("candidates: []")
            dropping = True
            continue
        # 续行：列表项（`- x`）或被缩进的行。遇到下一个 key 才停。
        if dropping and (not s or s.startswith("-") or raw[:1] in (" ", "\t")):
            continue
        dropping = False
        out.append(raw)
    return "---\n" + "\n".join(out) + "\n---\n" + text[m.end():]


def pull_page(slug: str) -> tuple:
    """把一个已入库页复制成「替换稿」草稿，送进 `.drafts/` 走审批（第六步）。

    返回 (ok, payload_or_err)。payload = {draft, page, title, chars}。

    ★ 为什么是**副本**而不是「把页移回草稿」：移动会让这页在编辑期间从 `wiki/`
      消失 —— 反链断、index 缺行、别的页 `[[wikilink]]` 指向 404、来源反查少一页。
      副本则让原页继续以「当前已定稿版本」存在，人随时能看、能对照。
    ★ 打的标记是 `edits: <slug>` 而不是 `merges:`：两者都是「覆盖某页」，但松紧不同。
      merges 来自编译链（机器合并，候选只增不减）；edits 来自人（编辑，候选可减可拆）。
      用同一个字段就是把两套松紧混成一锅，迟早漂开。
    ★ 已有同名草稿时**拒绝**：那说明这页已经有一份待审批的编辑稿，再拉一次会让
      「哪份才是最新」不可判定。让人先把前一份 commit 或退稿。
    """
    want = (slug or "").strip()
    if not _safe_slug(want):
        return False, f"非法页名：{slug!r}"
    src, why = find_page(want)
    if src is None:
        return False, why
    r = root()
    dst = r / ".drafts" / f"{want}.md"
    if dst.exists():
        return False, (f"已有一份同名草稿：.drafts/{want}.md —— 先把它入库或退稿，"
                       "再拉这一页（同时存在两份会让「哪份是最新」不可判定）")

    text = src.read_text(encoding="utf-8")
    fm, body = parse_front(text)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(_set_fm_line(text, "edits", want), encoding="utf-8")

    page_rel = src.relative_to(r).as_posix()
    log_append("pull-page", page_rel, f"→ .drafts/{want}.md（编辑副本）")
    return True, {"draft": f".drafts/{want}.md", "page": page_rel,
                  "title": fm.get("title") or want, "chars": len(body)}


def request_delete(slug: str) -> tuple:
    """把一个已入库页送进审批，等一句「确认删除」（第八步 + 第十步提案）。

    返回 (ok, payload_or_err)。payload = {draft, page, title, candidates, retire}。
    `candidates` 仍是**条数**（第八步契约，`test_wiki_delete` 钉着），
    `retire` 是第十步新增的**权威 id 清单**。

    ★ 产出的是**替换稿的空形态**：`edits: <slug>` + `candidates: []`。
      不新增 `delete:` 字段 —— 删页就是「用空替换那页」，同一个动词的边界态。
      这样删页天然继承 `edits` 的全部护栏（`LOCKED_FM` 锁、必须走审批、必须人 commit），
      不需要再给「删除」单独造一套权限。
    ★ 第十步：`retire: [ids]` 把「真正废弃的集合」写进草稿，默认 = 原页全量，
      行为与第八步逐字兼容。**真相跟着草稿走**，不另立 `meta/` 状态文件 ——
      多一个文件就多一处「草稿退了、档案还在」的不同步，而那种不同步不报错。
      此时页还在、候选清单完整可读，这是「先拆走再删」唯一的时间窗口（§2.3）。
    ★ `retire` 与 `edits` 一样是程序/人独占，进 `wiki_agent.LOCKED_FM`。
    ★ 正文照抄：删之前人在审批浮层里还能看到要删的是什么；退稿时整份留在
      `.drafts/.discarded/`，事后能查「当初删掉的那页长什么样」。
    ★ 已有同名草稿时**拒绝**（同 `pull_page`）：两份并存会让「哪份是最新」不可判定。
    """
    want = (slug or "").strip()
    if not _safe_slug(want):
        return False, f"非法页名：{slug!r}"
    src, why = find_page(want)
    if src is None:
        return False, why.replace("合并目标", "删除目标")
    r = root()
    dst = r / ".drafts" / f"{want}.md"
    if dst.exists():
        return False, (f"已有一份同名草稿：.drafts/{want}.md —— 先把它入库或退稿，"
                       "再删这一页（同时存在两份会让「哪份是最新」不可判定）")

    text = src.read_text(encoding="utf-8")
    fm, _body = parse_front(text)
    ids = _cand_ids(fm)
    n = len(ids)
    draft_text = _set_fm_line(_set_cands_empty(text), "edits", want)
    draft_text = _set_fm_line(
        draft_text, "retire", "[" + ", ".join(str(i) for i in ids) + "]")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(draft_text, encoding="utf-8")

    page_rel = src.relative_to(r).as_posix()
    log_append("request-delete", page_rel,
               f"→ .drafts/{want}.md（待确认删除 · {n} 条候选默认全废，"
               "可在删页提案里逐条改去向）")
    return True, {"draft": f".drafts/{want}.md", "page": page_rel,
                  "title": fm.get("title") or want, "candidates": n,
                  "retire": ids}


def body_diff(old_body: str, new_body: str, context: int = 1) -> list:
    """两段正文的行级 diff，给审批页看「这次并进去动了什么」。

    返回 `[{"kind": "eq"|"del"|"ins"|"gap", "text": ...}]`。

    ★ 为什么在这里做而不是在模板里：模板做 diff 只能按行号硬比，插入一行之后
      整页全红全绿，等于没 diff。`SequenceMatcher` 找的是最长公共子序列。
    ★ `gap` 是折叠掉的相同段 —— 一页几十行里改动往往只有两三行，全展开会让人
      找不到改动在哪（而且他要找的正是改动）。
    ★ 空行照常进 diff：正文用空行分段，吞掉它们会让「删了一段」看起来像「并了两行」。
    """
    a = (old_body or "").splitlines()
    b = (new_body or "").splitlines()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    out = []
    for kind, i1, i2, j1, j2 in sm.get_opcodes():
        if kind == "equal":
            n = i2 - i1
            if n <= context * 2 + 1:
                out.extend({"kind": "eq", "text": t} for t in a[i1:i2])
            else:
                out.extend({"kind": "eq", "text": t} for t in a[i1:i1 + context])
                out.append({"kind": "gap", "text": f"…… {n - 2 * context} 行没变 ……"})
                out.extend({"kind": "eq", "text": t} for t in a[i2 - context:i2])
            continue
        # replace 拆成「先删后插」：审批时要能一眼看出哪句被换掉了
        if kind in ("delete", "replace"):
            out.extend({"kind": "del", "text": t} for t in a[i1:i2])
        if kind in ("insert", "replace"):
            out.extend({"kind": "ins", "text": t} for t in b[j1:j2])
    return out


# ------------------------------------------------------------------ 草稿 / 审批
def approval_active() -> bool:
    """当前是否存在 active 的审批对话 —— 判据：`.drafts/` 里有 `.md`。

    ★ 判据收在这一处（第三步）：浮层提示 / agent 状态都调它，别各写一遍。
    ★ 2026-11-09 起这只是**展示用**判据（「当前有无待审批草稿」），不再闸
      提炼/编译 —— 用户拍板：草稿分批产生，有草稿就拒编会卡死后续流水线；
      并发由编译锁 + 运行期 mutex 兜底（见 test_approval_pause.py 的新契约）。
    ★ 真相在**文件**：`.drafts/` 空了 = 全部 commit 或归档 = 本轮审批结束，
      不靠人点「结束」。DB 里的 `Conversation(draftbatch/current)`
      只是痕迹（下一轮审批复用同一条对话追加消息），拿它判「还在审批」会
      永远返回 True —— 那会把展示永久钉在「审批中」，且日志干净（静默失败最贵）。
    ★ 读不到目录时**保守返回 True 并报错**：此时若误判为「空闲」，浮层会
      谎称「当前无待审批草稿」，人可能以为裁决完了；保守至少有人能从日志
      里看见原因。
    """
    d = root() / ".drafts"
    try:
        return bool(list(d.glob("*.md")))
    except OSError:
        log.error("审批状态判据读不到 %s，按「审批中」处理以免误放行", d)
        return True


# ------------------------------------------------------------------ 草稿稳定编号
#: 自增计数器（相对 wiki 根）。★ 不进 SUBDIRS：用到才建（同 MERGED_DIR 的纪律）。
_DRAFT_SEQ = "meta/draft_seq.json"


def _draft_id(fm: dict):
    """frontmatter 的 `draft_id` → 正整数；缺失 / 脏值返回 None（不猜）。"""
    try:
        v = int(str((fm or {}).get(DRAFT_ID_FM) or "").strip())
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _max_known_draft_id() -> int:
    """现存草稿 + 退稿归档里最大的 draft_id。

    ★ 只在计数器文件丢失 / 损坏时兜底（`ensure_draft_ids`）：从已知最大号 +1 续，
      绝不把已经发出去的号再发一遍。归档也扫 —— 退稿的号同样不该被新稿复用。
    """
    r = root()
    best = 0
    for box in (r / ".drafts", r / ".drafts" / ".discarded"):
        for p in box.glob("*.md"):
            try:
                fm, _ = parse_front(p.read_text(encoding="utf-8"))
            except OSError:
                continue
            did = _draft_id(fm)
            if did and did > best:
                best = did
    return best


def _reserve_draft_ids(n: int) -> int:
    """锁内预占 n 个草稿编号，返回起始号。n<=0 返回 0。

    ★ 计数器 flock 串行化（跨进程不重号，同编译锁的选型理由）。预占是「先把
      计数器推过去，再慢慢写文件」：中途崩溃只会跳号，不会把已发出的号重发。
    ★ 计数器文件丢失 / 损坏时从 `_max_known_draft_id() + 1` 续 —— 绝不复用。
    """
    if n <= 0:
        return 0
    seq = root() / _DRAFT_SEQ
    seq.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(seq), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 65536).decode("utf-8")
        try:
            nxt = int(json.loads(raw).get("next") or 0)
        except (ValueError, TypeError, AttributeError):
            nxt = 0
        if nxt <= 0:
            nxt = _max_known_draft_id() + 1
        start = nxt
        nxt += n
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, (json.dumps({"next": nxt}) + "\n").encode("utf-8"))
        return start
    finally:
        os.close(fd)


def next_draft_id() -> int:
    """分配一个新的草稿稳定编号（显式申请，不绑定文件）。

    ★ 新链路「从已入库页拉一份并入稿」在**写盘时**就把号写进 frontmatter：
      等展示入口补号会让同一份稿在两次渲染之间跳号，调用方拿到 payload 时
      也还不知道它叫 D 几（2026-11-13 用户：「拉入的时候最好用新的草稿 ID」）。
    """
    return _reserve_draft_ids(1)


def ensure_draft_ids() -> int:
    """给缺编号的草稿补上稳定自增 id（幂等）。返回这次补了几份。

    ★ 只在**展示入口**调（`wiki_agent.read("drafts")` —— 浮层与 briefing 共用它）。
      `list_drafts()` 保持纯读：它被编译幂等扫描 / 退稿守恒 / 守卫字节快照共用，
      那些路径一个字节都不该写，读时写会污染「拒写时没动过」的反证。
    ★ 编号是草稿的**身份**不是列表位置：合并保留目标稿的号、拆分新页拿新号、
      改名 / 重排都不跳变（用户 2026-11-13 拍板，类比候选的自增 id）。
    ★ 整段「扫描 → 预占 → 写盘」都走同一把计数器锁，两个展示请求并发也不会
      给同一份稿发两个号。写盘走 tmp + os.replace：审批面板随时在读，不能让人
      读到半截文件。
    ★ 已有号的稿一律不动（包括编译 / 审批对话刚写过的）：锁内重读再判，
      别拿扫描时的旧文本把别人刚写的内容盖回去。
    """
    r = root()
    box = r / ".drafts"
    pending = []
    for p in sorted(box.glob("*.md")):
        try:
            fm, _ = parse_front(p.read_text(encoding="utf-8"))
        except OSError:
            continue
        if _draft_id(fm) is None:
            pending.append(p)
    if not pending:
        return 0
    nxt = _reserve_draft_ids(len(pending))
    done = 0
    for p in pending:
        try:
            cur = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if _draft_id(parse_front(cur)[0]) is not None:
            continue
        new = _set_fm_line(cur, DRAFT_ID_FM, str(nxt))
        nxt += 1
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(new, encoding="utf-8")
        os.replace(tmp, p)
        done += 1
    return done


def list_drafts() -> list:
    """待审批草稿（`.drafts/*.md` 平铺，不含 `.discarded`）。

    ★ 纯读：不补编号、不写任何字节。`draft_id` 缺失时给 None —— 补齐是展示入口
      （`ensure_draft_ids`）的事，编译扫描 / 守卫快照调到这里不该有副作用。
    """
    r = root()
    out = []
    for p in sorted((r / ".drafts").glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, body = parse_front(text)
        missing = missing_fm(fm)
        out.append({
            "slug": p.stem,
            "draft_id": _draft_id(fm),
            "title": fm.get("title") or p.stem,
            "domain": fm.get("domain") or "",
            "updated": fm.get("updated") or "",
            "confidence": fm.get("confidence") or "",
            "sources": fm.get("sources") or [],
            "candidates": _cand_ids(fm),
            # ★ `merges` = 这份草稿不是新页，而是「改写已有页 <slug>」（阶段 2.3d）。
            #   审批时靠它走覆盖分支；没有它就是普通新页。
            "merges": str(fm.get("merges") or "").strip(),
            # ★ `edits` = 这份草稿是「某已入库页的替换稿」（第六步）。
            #   与 merges 的差别只有一处，但很关键：merges 是编译链的**机器合并**
            #   （候选只增不减），edits 是**人主动编辑**（候选可减可拆）。
            #   审批浮层靠它显示「正在编辑 wiki/xxx.md」而不是「新草稿 xxx」。
            "edits": str(fm.get("edits") or "").strip(),
            # ★ `delete` = 这份草稿是「删掉某页」（第八步）：`edits` 的空形态。
            #   判据走 `is_delete_fm`（不在这里另算一遍 candidates 是否为空）：
            #   浮层显示「删除」与 `approve` 真去删，必须是同一个判断。
            "delete": is_delete_fm(fm),
            # ★ 第十步：删页提案的真相落在草稿 frontmatter 里。这两个字段让
            #   `wa.read("drafts")` / 面板不用再猜「哪些稿是这次搬迁出来的」。
            #   权威判据仍在 `_retire_ids` / `RETIRE_FM_KEYS`，这里只是透出。
            "retire": _retire_ids(fm),
            "retire_from": str(fm.get("retire_from") or "").strip(),
            "chars": len(body),
            "body": body,
            # ★ 缺哪些字段要**点名**给页面：只说"不完整"的话，人还得自己 diff 一遍
            #   frontmatter 才知道该补什么（审批是唯一闸门，前置提示必须具体）。
            "missing": missing,
            "complete": not missing,
            "path": str(p),
        })
    # 缺字段的排前面：那是最需要人处理的
    out.sort(key=lambda x: (x["complete"], x["slug"]))
    return out


def _merges_lost(fm: dict, old_text: str) -> list:
    """合并会丢掉的候选 id（空列表 = 只增不减，可以并）。

    ★ 判据只这一份：`_validate_draft`（入库前预检）与 `_approve_merge`（真写）
      都调它。分成两处写过一次就漏一次，而这里漏的后果是「安静地少一条判断」。
    """
    old_fm, _old_body = parse_front(old_text)
    old_ids, new_ids = _cand_ids(old_fm), _cand_ids(fm)
    return [i for i in old_ids if i not in new_ids]


def _validate_draft(slug: str) -> tuple:
    """校验一份草稿能不能入库 —— **不落任何字节**。

    返回 (ok, info_or_err)。成功时 info = {src, text, fm, body, domain, conf,
    merges, dst}；`dst` 是入库目标（merges 时是已有页）。

    ★ 存在的理由（第五步 5c）：`commit()` 要「先全验、后全动」——批量入库时
      任何一份不过就整体拒绝。若校验只长在 `approve()` 里，commit 就只能逐份
      调 approve，半途失败留下「入库一半、剩下的还是草稿」的残局，且日志干净。
    ★ 校验口径只有这一份：`approve()` 也调它，单页与批量不会漂成两套判据。
    """
    if not _safe_slug(slug):
        return False, f"非法草稿名：{slug!r}"
    r = root()
    src = r / ".drafts" / f"{slug}.md"
    if not src.exists():
        return False, f"草稿不存在：{slug}"

    text = src.read_text(encoding="utf-8")
    fm, body = parse_front(text)
    missing = missing_fm(fm)
    if missing:
        return False, f"frontmatter 缺字段：{', '.join(missing)}（规范见 schema.md）"

    # ★★ 第十三步 A：正文为空 + 挂着候选 = 「结论还没重编成功」的空壳页，
    #   绝不放进库（人点开只会看到一片空白，且日志干净）。删页稿是 `edits`
    #   的空形态（candidates 为空），天然不在此列；老页三块正文非空也不受影响。
    #   出错时谁会知道：编译收尾把失败原因写进 notes，草稿带 `stale: true`，
    #   审批浮层可见，这里再兜住「人硬点提交」的最后一步。
    if not (body or "").strip() and _cand_ids(fm):
        return False, ("结论还没生成（正文为空，草稿仍带待重编标记）："
                       "先在审批里重编结论，再提交入库")

    domain = str(fm["domain"]).strip()
    if not _safe_slug(domain):
        return False, f"domain 非法：{domain!r}"
    conf = str(fm.get("confidence", "")).strip().lower()
    if conf not in CONFIDENCE_OK:
        return False, f"confidence 只能是 {'/'.join(CONFIDENCE_OK)}，现在是 {conf!r}"

    edits = str(fm.get("edits") or "").strip()
    merges = str(fm.get("merges") or "").strip()

    if edits:
        # ---- 替换稿（第六步）：人把某页拉进草稿改，提交时**整页替换**那一页。
        # ★ 与 merges 唯一的差别、也是本分支存在的全部理由：**候选可以减**。
        #   人编辑一页本来就要能拆、能删、能打散；merges 的「只增不减」是编译链
        #   （机器合并）的保护，套到人身上就变成了「不许改」。
        # ★ 但「候选减少」不等于「候选丢失」：真有候选凭空消失，set 层的
        #   `_check_candidates` 早就拒了。所以这里不再重复拒绝，只把「移走了哪些」
        #   记进 info，让 commit 能如实写进日志（人回看时知道这次编辑撤了什么）。
        page, why = find_page(edits)
        if page is None:
            return False, why.replace("合并目标", "替换目标")
        if is_delete_fm(fm):
            # ---- 删页（第八步）：`edits` 的空形态 —— 用空替换那一页。
            # ★ 目标就是 `edits` 那一页，草稿的 domain / slug / title / 正文一概不
            #   参与判断（「删掉这页」不需要知道它被改成了什么样），所以也**不做**
            #   dst 冲突检查：不写新位置，没有可冲突的地方。
            # ★ 候选清空是**合法终态**，不是「空页」：那些候选由 `_approve_delete`
            #   一并回池废弃（用户 2026-11-06 拍板：删页 = 页内判断也不要了；
            #   判断有用的话，是删之前先拆走的事）。
            old_fm, _old = parse_front(page.read_text(encoding="utf-8"))
            source_ids = _cand_ids(old_fm)
            # ★ 第十步：`removed` 改从删除稿的 `retire` 读。**旧格式（键不在）
            #   显式回退到原页全量**，并在日志与 commit 返回值里点名 ——
            #   静默回退会让人以为「逐条编排生效了」，实际一条都没留。
            # ★ `retire: []` 是合法的「全部搬走」，不能拿真值判断当成旧格式。
            if "retire" in fm:
                retire = _retire_ids(fm)
                stray = [i for i in retire if i not in source_ids]
                if stray:
                    return False, (
                        "删除稿的 retire 里有不属于这一页的候选 %s（原页只有 %s）；"
                        "按 frontmatter 生成的权威清单为准，请检查 .drafts/%s.md"
                        % ("、".join(str(i) for i in stray),
                           "、".join(str(i) for i in source_ids) or "空", slug))
                legacy, note = False, ""
            else:
                retire = list(source_ids)
                legacy = True
                note = ("旧格式删除稿：没有 retire 字段，按整页废弃处理"
                        "（逐条去向未生效）")
                log.warning("删页校验：.drafts/%s.md %s", slug, note)
            return True, {"src": src, "text": text, "fm": fm, "body": body,
                          "domain": page.parent.name, "conf": conf, "merges": "",
                          "edits": edits, "delete": True, "dst": page,
                          "page_src": page, "removed": retire,
                          "retire_legacy": legacy, "retire_note": note}
        # 新位置由**草稿的 domain/slug** 决定：改 domain 或改 slug 都是合法的
        # 「移动」，提交时按「旧位置删、新位置写」执行。
        dst = r / "wiki" / domain / f"{slug}.md"
        if dst.exists() and dst != page:
            return False, (f"替换目标位置已存在另一页：{dst.relative_to(r).as_posix()}"
                           "（提交会覆盖它，而那不是你指定的那一页）")
        removed = _merges_lost(fm, page.read_text(encoding="utf-8"))
        return True, {"src": src, "text": text, "fm": fm, "body": body,
                      "domain": domain, "conf": conf, "merges": "",
                      "edits": edits, "delete": False, "dst": dst, "page_src": page,
                      "removed": removed}

    if merges:
        dst, why = find_page(merges)
        if dst is None:
            return False, why
        if dst.parent.name != domain:
            return False, (f"不跨域合并：草稿在 {domain}，目标页在 {dst.parent.name} 下"
                           f"（{dst.relative_to(r).as_posix()}）")
        lost = _merges_lost(fm, dst.read_text(encoding="utf-8"))
        if lost:
            return False, (f"合并会丢掉原有候选 {', '.join(str(i) for i in lost)} —— "
                           "并入只能加不能减；删判断是你的事，不是编译的事")
    else:
        dst = r / "wiki" / domain / f"{slug}.md"
        if dst.exists():
            return False, f"已存在同名页：wiki/{domain}/{slug}.md（要改写它，草稿里得有 merges 声明）"

    return True, {"src": src, "text": text, "fm": fm, "body": body,
                  "domain": domain, "conf": conf, "merges": merges, "dst": dst}


def approve(slug: str) -> tuple:
    """Gate 1：把 `.drafts/<slug>.md` 移进 `wiki/<domain>/<slug>.md`。

    返回 (ok, payload_or_err)。payload = {page, domain, title, sources}。

    ★ 校验不放水：缺 frontmatter 字段直接拒。审批是**唯一**的入库闸门，这里松一次，
      wiki 里就多一条断链的页，而断链不会自己报错（只会在某天发现反向映射漏了页）。
    ★ 目标已存在时**拒绝**而不是覆盖：覆盖等于静默删掉一页人的知识（Gate 的语义是
      "人确认过"，而被覆盖的那页可能正是他上周确认的）。要覆盖必须**显式**声明 ——
      草稿 frontmatter 带 `merges: <slug>`，那是编译链读过已有页之后写下的意图，
      审批页会把 diff 摆出来给人看（阶段 2.3d）。
    ★ `merges` 不是后门：目标必须存在、必须同域、原有候选一条都不许少。三条任一
      不满足就拒绝，并说清是哪条 —— 合并最贵的错法是「安静地少了一条判断」。
    """
    ok, info = _validate_draft(slug)
    if not ok:
        return False, info
    if info.get("delete"):
        return _approve_delete(slug, info)
    if info.get("edits"):
        return _approve_edit(slug, info)
    if info["merges"]:
        return _approve_merge(slug, info["src"], info["text"], info["fm"],
                              info["body"], info["merges"], info["dst"])

    fm, _body = info["fm"], info["body"]
    dst, domain, conf = info["dst"], info["domain"], info["conf"]
    dst.parent.mkdir(parents=True, exist_ok=True)
    if any(k in fm for k in TRANSIENT_FM_KEYS):
        # ★ 带机器字段（瞬时标记 / draft_id）的草稿走「先写目标再删草稿」，入库时剥干净。
        #   编号补齐后每份稿都带 draft_id，所以这是新页的常路；与 merge/edit 分支同口径。
        #   没有任何机器字段的手写稿仍走 os.replace 原子移动（既有行为不变）。
        dst.write_text(_strip_retire_fm(info["text"]), encoding="utf-8")
        info["src"].unlink()
    else:
        os.replace(info["src"], dst)     # 同一文件系统内原子：不会出现"移了一半"
    page_rel = dst.relative_to(root()).as_posix()

    summary = _first_sentence(_body)
    index_upsert(page_rel, fm.get("title") or slug, summary, fm.get("updated"))
    log_append("approve", page_rel,
               f"title={fm.get('title')} · confidence={conf} · sources={len(fm.get('sources') or [])}")
    rebuild_sources_index(mark_log=False)
    return True, {"page": page_rel, "domain": domain, "title": fm.get("title"),
                  "sources": fm.get("sources") or []}


def _approve_merge(slug: str, src: Path, text: str, fm: dict, body: str,
                   merges: str, dst: Path) -> tuple:
    """审批「并入已有页」：备份旧页 → 覆盖 → 记 merge。**不删任何东西**。

    ★ 前置校验（目标存在 / 同域 / 候选只增不减）已由 `_validate_draft` 做完，
      这里不再重复：commit 要「先全验、后全动」，判据分成两处写就会慢慢漂开。
    """
    r = root()
    old_text = dst.read_text(encoding="utf-8")
    old_fm, old_body = parse_front(old_text)
    old_ids, new_ids = _cand_ids(old_fm), _cand_ids(fm)

    bak = _merge_backup(dst, merges)
    # ★ 先写目标再删草稿：中途失败时草稿还在（可以重来），而不是两边都没有。
    #   带 merges 入库会让下次编译以为这页还在等合并 ⇒ 又生成一份覆盖草稿。
    dst.write_text(_strip_retire_fm(_strip_fm_key(text, "merges")), encoding="utf-8")
    src.unlink()
    page_rel = dst.relative_to(r).as_posix()

    index_upsert(page_rel, fm.get("title") or merges, _first_sentence(body), fm.get("updated"))
    log_append("merge", page_rel,
               f"候选 {len(old_ids)}→{len(new_ids)} · {len(old_body)}→{len(body)} 字 · "
               f"草稿 {slug} · 旧版 {bak.relative_to(r).as_posix()}")
    rebuild_sources_index(mark_log=False)
    return True, {"page": page_rel, "domain": dst.parent.name, "title": fm.get("title"),
                  "sources": fm.get("sources") or [], "merged": True,
                  "backup": bak.relative_to(r).as_posix()}


def _approve_edit(slug: str, info: dict) -> tuple:
    """审批「替换已入库页」：备份旧页 → 写新位置 → （旧位置若变了则删）→ 重建关联。

    ★ 与 `_approve_merge` 骨架相同（备份 → 覆盖 → 记一笔），差别只有两处：
      1. **不校验候选只增不减** —— 人编辑一页本来就要能拆、能删、能打散；
         候选会不会凭空消失由 set 层的 `_check_candidates` 保证，这里不重复拦。
      2. 允许**移动**：草稿的 domain/slug 与原标题不同时，按「旧位置删、新位置写」
         执行，两处都留痕（旧位置进 index 的行也要去掉）。
    ★ 顺序刻意是「先写新位置 → 再删旧位置 → 最后删草稿」：任何一步中断，旧页都还在，
      能重来；反过来会先制造一个「两边都没有」的窗口。
    """
    r = root()
    page_src = info["page_src"]          # 原页：备份它，可能还要删它
    dst = info["dst"]                    # 新位置：由草稿的 domain/slug 决定
    fm, body = info["fm"], info["body"]
    old_text = page_src.read_text(encoding="utf-8")
    old_fm, old_body = parse_front(old_text)
    old_ids, new_ids = _cand_ids(old_fm), _cand_ids(fm)
    removed = list(info.get("removed") or [])
    added = [i for i in new_ids if i not in old_ids]

    bak = _merge_backup(page_src, info["edits"])
    dst.parent.mkdir(parents=True, exist_ok=True)
    # ★ 两个标记都要剥：`edits` 是这次的意图，`merges` 是万一同时带上来的历史意图。
    #   任一个留在库里，下次编译都会以为这页还在等合并/替换，于是又生成一份覆盖草稿。
    # ★ 第十步：搬迁稿（edits 替换稿）带 `retire_from`，只在删页提案阶段有意义；
    #   入库时和其它意图标记一起剥掉，别让机器字段留在人读的页面里。
    new_text = _strip_retire_fm(
        _strip_fm_key(_strip_fm_key(info["text"], "edits"), "merges"))
    dst.write_text(new_text, encoding="utf-8")
    old_rel = page_src.relative_to(r).as_posix()
    if dst != page_src:
        page_src.unlink()                # 移动：旧位置删掉（上面已备份）
        index_remove(old_rel)
    info["src"].unlink()                 # 草稿功成身退

    page_rel = dst.relative_to(r).as_posix()
    index_upsert(page_rel, fm.get("title") or slug, _first_sentence(body), fm.get("updated"))
    note = (f"候选 {len(old_ids)}→{len(new_ids)} · {len(old_body)}→{len(body)} 字 · "
            f"草稿 {slug} · 旧版 {bak.relative_to(r).as_posix()}")
    if removed:
        note += " · 移走 " + ", ".join(str(i) for i in removed)
    if added:
        note += " · 新增 " + ", ".join(str(i) for i in added)
    if dst != page_src:
        note += f" · 从 {old_rel} 移来"
    log_append("edit-page", page_rel, note)
    rebuild_sources_index(mark_log=False)
    # ★ 编辑改的是**正文**，而正文里的 `[[wikilink]]` 正是 backlinks 的来源 ——
    #   不重建就会「页改了、网没跟上」，而且不报错。5c 的 save_page 漏了这一处。
    rebuild_backlinks_index(mark_log=False)
    return True, {"page": page_rel, "domain": dst.parent.name,
                  "title": fm.get("title"), "sources": fm.get("sources") or [],
                  "edited": True, "removed": removed, "added": added,
                  "backup": bak.relative_to(r).as_posix(),
                  "moved_from": old_rel if dst != page_src else ""}


def _release_candidates(ids) -> tuple:
    """把一批候选标成「已丢弃」（删页 / 退稿时用）。返回 `(真正更新的条数, 错误文案)`。

    ★ 这是 wiki 层**唯一**写 DB 的地方。模块头写「本模块只碰文件、不碰 LLM、不碰 DB」，
      删页 / 退稿是唯一必须破例的语义：页/草稿没了，它挂着的候选不能悬空 ——
      悬空（还活着但不属于任何页）意味着下次编译会把它们重新编进新页，
      **删掉的页原地复活**，而且没有任何一处会报错。破例的代价用两条纪律兜住：
      ① DB 访问只收在这一个函数里，不散落到别处；
      ② 失败**当失败返回**（不吞）：删页由 `_approve_delete` 在删页之前中止；
        退稿时草稿已归档，失败进回执（`discard` 的 `release_error`）。
    ★ 状态用 `discarded`：删页 / 退稿否定的是**这页内容**，不是「这些字没出现过」，
      所以留档而不是物理删除 —— 已处理的候选在面板里仍能翻出来看、能改判回待审。
    ★ 函数内 import：`core.ingest` 顶层 `from core import wiki`，顶层反向 import 成环。
    """
    want = sorted({i for i in (ids or []) if isinstance(i, int)})
    if not want:
        return 0, ""
    try:
        from core.models import IngestCandidate
        n = IngestCandidate.objects.filter(id__in=want).update(status="discarded")
    except Exception as exc:      # noqa: BLE001 —— 失败必须变成一句人能读的话，由调用方中止/上报
        log.error("候选废弃失败 %s：%s", want, exc, exc_info=True)
        return 0, f"候选废弃失败（{exc}）"
    return n, ""


def _approve_delete(slug: str, info: dict) -> tuple:
    """审批「删页」：`edits` 的空形态（第八步）。备份 → 候选回池 → 删页 → 清关联。

    ★ 与 `_approve_edit` 的差别只有一处：**不写新位置**，而是把目标页整个删掉。
    ★ 顺序刻意是「备份 → 候选回池废弃 → 删页 → 清关联 → 删草稿」：
      - 备份失败就不许删（覆盖/删除是唯一会让人「上周确认的结论不见了」的动作）；
      - 候选必须在删页**之前**回池：删了页就丢了「这页有哪些候选」这个信息源，
        之后再想回池只能靠猜；
      - 回池失败就中止：不能出现「页没了、候选还活着」那种半截状态。
    ★ 删页**不做候选守恒**（与拆 / 并相反）：用户 2026-11-06 定下的语义是
      「显式删页 = 页内候选一并废弃；候选有用的话，是删之前先拆走的事」。
      守恒守卫在拆 / 并那一侧（第七步），删页只管「这页不要了」。
    ★ 反链里指向这一页的**不断链也不改**：让它们留在 `backlinks.json` 里，删完把
      「还有几页链向它」写进返回值与日志 —— 悄悄连断链一起清掉，就再也没人知道
      哪些页在说一件已经不存在的事。
    """
    r = root()
    page_src = info["page_src"]
    old_rel = page_src.relative_to(r).as_posix()
    fm, body = info["fm"], info["body"]
    old_ids = list(info.get("removed") or [])

    bak = _merge_backup(page_src, info["edits"])
    released, rel_err = _release_candidates(old_ids)
    if rel_err:
        return False, rel_err + " —— 这一页没有删"
    page_src.unlink()
    index_remove(old_rel)
    info["src"].unlink()                 # 草稿功成身退

    # ★ 两个索引都要全量重建：正文里的 `[[wikilink]]` 是 backlinks 的来源，
    #   页没了它那份也要跟着更新（断链会**保留**，见函数头）。
    rebuild_sources_index(mark_log=False)
    backlinks = rebuild_backlinks_index(mark_log=False)
    dangling = list(backlinks.get(page_src.stem) or [])

    note = (f"{len(body)} 字 · 候选 {len(old_ids)} 条回池废弃"
            f"（实际更新 {released} 条） · 旧版 {bak.relative_to(r).as_posix()}")
    if dangling:
        note += " · 仍有 %d 页链向它：%s" % (len(dangling), "、".join(dangling))
    log_append("delete-page", old_rel, note)
    return True, {"page": old_rel, "domain": page_src.parent.name,
                  "title": fm.get("title") or info["edits"],
                  "deleted": True, "released": old_ids, "released_n": released,
                  # ★ 第十步：旧格式删除稿的显式回退要说出来，不能藏在日志里。
                  "retire_legacy": bool(info.get("retire_legacy")),
                  "retire_note": info.get("retire_note") or "",
                  "retire_ids": list(old_ids),
                  "dangling": dangling,
                  "backup": bak.relative_to(r).as_posix()}


# ------------------------------------------------- 退稿依赖同步（零 LLM、唯一落点）
#: frontmatter 里可能指向「被退草稿 slug」的字段。★ 只报告、不自动改：
#: merges / edits / retire_from 指向一个已消失的草稿，程序猜不出该改指向哪；
#: 悄悄改会造出一份「标了 edits 却没有目标页」的假稿（静默失败最贵）。
DISCARD_REF_FM_KEYS = ("merges", "edits", "retire_from")


def archived_title(gone_slug: str) -> str:
    """归档页的显示文本：读 `.discarded/` 里最新一份的 title，读不到退回 slug。

    ★ 2026-11-11 用户实测：解包后留一串裸英文 slug 像乱码、人读不出是哪页。
      所以显示文本优先取页标题；读不到（文件被手动清掉等）才退回 slug，
      解包本身不能因为读标题失败而中断。
    """
    box = root() / ".drafts" / ".discarded"
    for p in sorted(box.glob(f"{gone_slug}-*.md"), reverse=True):
        try:
            fm = parse_front(p.read_text(encoding="utf-8"))[0]
        except OSError:
            continue
        t = str(fm.get("title") or "").strip()
        if t:
            return t
    return gone_slug


def unwrap_wikilinks(text: str, gone_slug: str, display: str = None) -> tuple:
    """把正文里的 `[[gone]]` / `[[gone|别名]]` 解包成纯文本，返回 `(new_text, n)`。

    ★ 只动正文，frontmatter 一个字节不碰（与 `_rewrite_wikilinks` 同一形状）。
    ★ 精确匹配 `group(1) == gone`：`[[gone-extra]]` 这类前缀相同的页不误伤。
    ★ 别名优先，无别名用 display（归档页标题），再退回 slug —— 两套降级口径
      收在这里，`_rewrite_wikilinks` 也吃同一份 display 映射。
    """
    want = (gone_slug or "").strip()
    if not want:
        return text, 0
    shown = (display or "").strip() or archived_title(want)
    m = _FM_RE.match(text)
    head, body = (text[:m.end()], text[m.end():]) if m else ("", text)
    n = 0

    def sub(match):
        nonlocal n
        if match.group(1).strip() != want:
            return match.group(0)
        inner = match.group(0)[2:-2]
        alias = inner.split("|", 1)[1] if "|" in inner else ""
        n += 1
        return alias.strip() or shown

    return head + _WIKILINK_RE.sub(sub, body), n


def _body_links(text: str) -> list:
    """只读正文里的内链 target（frontmatter 不参与）。"""
    return [m.group(1).strip()
            for m in _WIKILINK_RE.finditer(parse_front(text)[1])]


def sync_discard_references(gone_slug: str) -> dict:
    """退稿后同步所有依赖：其他草稿正文 + 已入库页（走审批副本）+ frontmatter 报告。

    ★ 唯一落点（2026-11-13）：所有退稿路径（审批浮层按钮 / 对话丢弃 /
      合并归档 / 直接调 core）都经 `discard()` 到这里，调用点不用各写一遍。
      旧实现只在一处「对话合并归档」里解包过草稿，用户手点退稿完全没同步
      ⇒ 16:58 退 D3、18:34 带死链入库、批末补链又凭空多一份草稿。
    ★ 零 LLM：解包是确定性文本替换，维护路径不花钱。
    ★ 已入库页**绝不直接写 `wiki/`**：一律 `pull_page` 拉 `edits` 副本 +
      人 commit（与 `relink_after_commit` 同款「自动 ≠ 静默」）。
    ★ 一致性守卫读真实文件逐条确证 `[[gone]]` 真的不在了，不是把清单擦干净。
    """
    want = (gone_slug or "").strip()
    r = root()
    display = archived_title(want)
    report = {"drafts": [], "pages": [], "blocked": [],
              "guard_errors": [], "links": 0, "display": display}

    # ① 其他草稿：正文直接解包（不新增草稿）；frontmatter 依赖报告不自动改
    for d in list_drafts():
        slug = d["slug"]
        p = r / ".drafts" / f"{slug}.md"
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            report["blocked"].append({"page": f".drafts/{slug}.md",
                                      "why": f"读草稿失败：{e}"})
            continue
        new_text, n = unwrap_wikilinks(text, want, display)
        if n:
            try:
                p.write_text(new_text, encoding="utf-8")
            except OSError as e:
                report["blocked"].append({"page": f".drafts/{slug}.md",
                                          "why": f"写草稿失败：{e}"})
            else:
                report["drafts"].append(slug)
                report["links"] += n
                log_append("discard-sync", f".drafts/{slug}.md",
                           f"正文 [[{want}]] 已解包 {n} 处（显示为「{display}」）")
        for key in DISCARD_REF_FM_KEYS:
            if str(d.get(key) or "").strip() == want:
                report["blocked"].append({
                    "page": f".drafts/{slug}.md",
                    "why": f"frontmatter {key} 仍指向已退稿 {want}，需人工改指向",
                })

    # ② 已入库页：扫真实文件（不读可能过期的 backlinks.json），只落编辑副本
    for p in sorted((r / "wiki").rglob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if want not in _body_links(text):
            continue
        stem, rel = p.stem, p.relative_to(r).as_posix()
        # ★ 原页 candidates 为空时，`pull_page` 拉出的 edits 副本会被
        #   `is_delete_fm` 判成「删页稿」（edits + 空候选 = 用空替换那页）。
        #   退稿同步绝不能凭空造一份「人一点提交就把这页删掉」的稿 ——
        #   报告出来等人处理，不静默绕过（2026-11-13 e2e 种子实测撞到）。
        if not _cand_ids(parse_front(text)[0]):
            report["blocked"].append({
                "page": rel,
                "why": "原页候选为空，拉副本会被判成删页稿；未自动生成，请人工处理",
            })
            log.warning("退稿同步：%s 原页候选为空，跳过自动副本", rel)
            continue
        draft = r / ".drafts" / f"{stem}.md"
        if draft.exists():
            # ① 里刚解包过同一份副本就复用；否则不擅自动人正在编辑的稿
            try:
                dt = draft.read_text(encoding="utf-8")
            except OSError as e:
                report["blocked"].append({"page": rel, "why": f"读编辑副本失败：{e}"})
                continue
            if want not in _body_links(dt):
                report["pages"].append(stem)
                continue
            report["blocked"].append(
                {"page": rel, "why": "已有同名编辑副本且仍含该引用，未自动改写"})
            continue
        ok, payload = pull_page(stem)
        if not ok:
            report["blocked"].append({"page": rel, "why": str(payload)})
            continue
        try:
            dt = draft.read_text(encoding="utf-8")
            new_text, n = unwrap_wikilinks(dt, want, display)
            draft.write_text(new_text, encoding="utf-8")
        except OSError as e:
            # 这份草稿是刚建的，失败就收回 —— 不能留一份「标了 edits 却没改」的稿
            try:
                draft.unlink()
            except OSError:
                pass
            report["blocked"].append({"page": rel, "why": f"写编辑副本失败：{e}"})
            log.error("退稿同步：写 %s 失败 %s", stem, e, exc_info=True)
            continue
        report["pages"].append(stem)
        report["links"] += n
        log_append("discard-sync", rel,
                   f"→ .drafts/{stem}.md（编辑副本，解包 {n} 处；待提交入库）")

    # ③ 一致性守卫：读真实产物，逐条确证（「警告消失 ≠ 内容一致」同构）
    for slug in list(dict.fromkeys(report["drafts"] + report["pages"])):
        p = r / ".drafts" / f"{slug}.md"
        try:
            t = p.read_text(encoding="utf-8")
        except OSError as e:
            report["guard_errors"].append(f"{slug}: 草稿读不到（{e}）")
            continue
        if want in _body_links(t):
            report["guard_errors"].append(f"{slug}: 仍链向 [[{want}]]")
    if report["links"] or report["blocked"] or report["guard_errors"]:
        log_append("discard-sync", ".drafts/",
                   f"退稿 {want} · 解包 {report['links']} 处 · 草稿 "
                   f"{len(report['drafts'])} · 编辑副本 {len(report['pages'])}"
                   f" · 待处理 {len(report['blocked'])}"
                   + (f" · 守卫未通过 {len(report['guard_errors'])} 处"
                      if report["guard_errors"] else ""))
    return report


def discard(slug: str, reason: str = "", *, sync_report: dict = None) -> tuple:
    """退掉一份草稿：移进 `.drafts/.discarded/`（**不删**，留证据好回看）。

    ★ 2026-11-13（晚）：退稿 = **内容级否定**，草稿挂着的候选一并标 discarded
      （编译的预排除项）。旧语义「候选回池、下次编译再编」让退稿在下一轮编译
      原地复活（实测：16:53 退「美股情绪与杠杆」，20:10 又编出同名草稿）。
      反悔走候选面板改判回待审 —— 档案还在、判断还在，只是不再自动复活。
    ★ 仍被**其他草稿 / 已入库页**引用的候选不废弃：合并归档、编辑稿、搬迁稿
      也走本函数，那些候选已经在新家里，误杀会让新产物挂幽灵引用。
      「谁还挂着它」按产物算（与编译同口径），不看 DB 状态。
    ★ 2026-11-13：退稿不是「把一份文件挪走」就完了 —— 别处正文里的
      `[[slug]]` 会当场变成死链。同步依赖是**本函数的职责**，不是调用点的：
      四条退稿路径（审批浮层按钮 / 对话丢弃 / 合并归档 / 直接调 core）都从
      这里过，谁都不用自己记着调；旧实现只在「对话合并归档」一处解包过草稿。
    ★ `sync_report` 是可选 out 参数：成功时填入同步报告（草稿解包 / 编辑副本 /
      blocked / 守卫结果）+ 候选废弃结果（`released_candidates` /
      `release_error`），供视图组织可见回执。不传也照样同步 + 记 log.md。
      ★ 不改返回类型：`(ok, 归档路径字符串)` 是 verify_wiki / agent 的既有契约。
    ★ 同步失败不回滚退稿（文件已经挪走了），但一定写进报告与日志，不静默。
      候选废弃失败同理：草稿已归档，失败经 `release_error` 上报，那批候选
      仍留在池里 —— 回执必须说出来，不能假装已经废弃。
    """
    if not _safe_slug(slug):
        return False, f"非法草稿名：{slug!r}"
    r = root()
    src = r / ".drafts" / f"{slug}.md"
    if not src.exists():
        return False, f"草稿不存在：{slug}"
    try:
        cids = _cand_ids(parse_front(src.read_text(encoding="utf-8"))[0])
    except OSError:
        cids = []
    box = r / ".drafts" / ".discarded"
    box.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = box / f"{slug}-{stamp}.md"
    os.replace(src, dst)
    # ★ 审批标记跟着页走：页退了标记必须清掉，否则「已审 x/y」计数里留幽灵
    drop_approved(slug)
    # ★ 候选废弃在**挪走之后**算：此时 list_drafts() 已不含本稿，
    #   「仍被别处引用」才是真剩余（合并归档的候选此刻已写进合并稿）。
    released, rel_err = [], ""
    if cids:
        still = {i for d in list_drafts() for i in d["candidates"]}
        still.update(i for p in pages() for i in p["candidates"])
        released = [i for i in cids if i not in still]
        _, rel_err = _release_candidates(released)
        if rel_err:
            log.error("退稿：候选废弃失败 %s：%s", slug, rel_err)
    note = reason or "无理由"
    if released:
        note += " · 候选 %d 条一并废弃（%s）" % (
            len(released), "、".join("#%d" % i for i in released))
    if rel_err:
        note += " · 候选废弃失败：" + rel_err
    log_append("discard", f".drafts/{slug}.md", note)
    sync = sync_discard_references(slug)
    if sync_report is not None:
        sync_report.update(sync)
        sync_report["released_candidates"] = released
        if rel_err:
            sync_report["release_error"] = rel_err
    return True, dst.relative_to(r).as_posix()


def _json_map(rel: str) -> dict:
    """读一个 json 索引；缺失 / 坏掉都当空表（索引是派生品，不该让主流程抛）。"""
    p = root() / rel
    try:
        return json.loads(p.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}


def _cross_page_candidates(drafts: list) -> list:
    """这批草稿的候选里，哪些已经挂在**别的**已入库页上（影响预览）。

    ★ 只报告、不拒绝。规格原本想用它「保证这批候选不是已入库的重复」，但
      `merges` 草稿的候选**本来就该**与目标页重叠（合并只能加不能减），拿它当
      拒绝条件会让合并永远过不去。真正「不重复编译」的判据在
      `ingest._compiled_ids()`（扫产物 frontmatter），那里已经挡过一遍。
    ★ 必须在入库**前**取快照：approve 之后刚入库的页也会进 `pages()`，
      自己和自己比 ⇒ 每条都报重复，这个字段就废了。
    """
    owner = {}
    for pg in pages():
        for cid in pg["candidates"]:
            owner.setdefault(str(cid), pg["page"])
    out = []
    for d in drafts:
        # ★ 合并稿 / 替换稿的候选**本来就该**与目标页重叠：合并只能加、编辑是在
        #   原页上改，两者都比对过目标页。不跳过就会「每份编辑稿都报一遍重复」，
        #   这个字段立刻变成噪音，人就学会无视它。
        # ★ 第十步搬迁稿同理：候选此刻还挂在「待删页」上，那条重复在本批 commit
        #   里会随删页一起消失。报出来是噪音，而且会让人以为去向没编排好。
        #   这里跳过的是**报告**，不是守恒守卫（后者读真实文件、扫全集）。
        if d["merges"] or d.get("edits") or d.get("retire_from"):
            continue
        for cid in d["candidates"]:
            hit = owner.get(str(cid))
            if hit:
                out.append({"candidate": cid, "slug": d["slug"], "already_in": hit})
    return out


def retire_sources(drafts: list = None) -> dict:
    """`{被删页 slug: 原页候选 id 集合}`，只读真实 `wiki/` 页。

    ★ 第十步的权威清单只有一个来源：被删页 frontmatter 的 `candidates:`。
      删除稿清空了自己那份，搬迁稿又带着目标页原有候选，两处都**不能**当清单。
      守恒守卫与 commit 的「搬走几条」统计共用这一份，避免各自 `∩` 出不同结果。
    """
    drafts = list_drafts() if drafts is None else drafts
    out = {}
    for d in drafts:
        if not d.get("delete"):
            continue
        target = str(d.get("edits") or "").strip() or d["slug"]
        page, _why = find_page(target)
        if page is None:
            continue
        fm, _ = parse_front(page.read_text(encoding="utf-8"))
        out[target] = set(_cand_ids(fm))
    return out


def check_retire_conservation(drafts: list = None, live_ids=None,
                             overlay: dict = None, exclude=()) -> str:
    """第十步守恒守卫：删页提案里 `原页清单 = 迁走 ⊎ 废弃`，写盘前一处在内存验完。

    返回空串 = 通过，否则是能直接显示的原因（点名到 id 与草稿路径）。
    `live_ids` 传入时额外验「搬走的候选仍活着」；不传则只做纯文件集合守恒
    （`commit()` 走这条，live 由提案阶段的 `wa.apply_retire_action` 保证）。
    `overlay = {slug: 拟写入的文本}` 是**写盘前预演**：文件系统没有跨文件事务，
    提案动作必须先把全部内容备在内存里、用同一份判据验完，再原子落盘。
    overlay 里出现磁盘上还没有的 slug 也照样纳入扫描（新建页稿）。

    ★★ **不要复用 `wiki_agent._check_candidates`**：那条守恒的基准是「草稿之间
      并集不增不减」，本步是「**已入库页 → 草稿**」，跨了两个世界。硬复用会让
      它把「从原页搬进来的候选」判成「凭空多出」—— 守卫变假红比没有守卫更贵。
      两边 docstring 互指，防止后人以为漏了一处、或顺手把老守卫放宽
      （那会把第七步的保护一起拆掉）。
    ★ **读真实草稿文件**，不信任调用方传进来的解析结果：第九步
      「警告消失 ≠ 内容一致」的教训。传进来的 `drafts` 只用来定「扫哪几份」，
      内容一律现场重读。
    ★ **扫全集**：每份删除稿都对 `.drafts/` 里所有 `retire_from` 指向它的稿取并集，
      不是只看刚改的那一份。只看一份会在「两条候选搬去同一页」时算漏。
      `exclude` 是本次动作要归档掉的空搬迁稿，必须显式排除，否则预演会读到
      磁盘上的旧内容而误判冲突。
    ★ 与设计稿的一处口径收窄：`moved` 取「搬迁稿 candidates **∩ 原页清单**」，
      不是全部 candidates。「留 → 去已有页 X」的替换稿带着 X 原有候选，
      它们不属于原页；直接并集会让守恒守卫凭空报越界（假红比漏更贵）。
    ★ 本函数**只读、不写一个字节**。调用方（`apply_retire_action` / `commit()`）
      都必须在写盘前调它，拒写时一个字节不动。
    """
    r = root()
    drafts = list_drafts() if drafts is None else drafts
    overlay = overlay or {}
    excluded = set(exclude or ())
    known = {d["slug"] for d in drafts}
    # ★ exclude = 本次动作要归档掉的搬迁稿（撤回到空稿）。它磁盘上还在，
    #   若照样纳入扫描，预演会读到「旧内容仍挂着那条候选」而误判冲突 ——
    #   守卫自己变成假红。排除是显式的，调用方必须把归档清单一起传进来。
    slugs = [s for s in ([d["slug"] for d in drafts]
                         + [s for s in overlay if s not in known])
             if s not in excluded]
    rows = []
    for s in slugs:
        if s in overlay:
            fm, _ = parse_front(overlay[s])
        else:
            p = r / ".drafts" / f"{s}.md"
            try:
                fm, _ = parse_front(p.read_text(encoding="utf-8"))
            except OSError:
                return f"草稿读不到：.drafts/{s}.md（本批没有落盘）"
        rows.append((s, fm))

    for draft_slug, fm in rows:
        if not is_delete_fm(fm):
            continue
        if "retire" not in fm:
            # 旧格式删除稿：_validate_draft 会显式回退并说出来，这里不替它
            # 「猜」一份清单，否则回退事实会被守卫的静默通过盖掉。
            continue
        target = str(fm.get("edits") or "").strip()
        page, why = find_page(target)
        if page is None:
            continue     # 由 _validate_draft 报「替换目标不存在」，别在这里重复报
        old_fm, _ = parse_front(page.read_text(encoding="utf-8"))
        source = set(_cand_ids(old_fm))
        retired = set(_retire_ids(fm))
        moved, movers = set(), []
        for other_slug, other_fm in rows:
            if other_slug == draft_slug:
                continue
            if str(other_fm.get("retire_from") or "").strip() != target:
                continue
            # ★ `moved` 是「**从被删页搬来的**」候选，不是搬迁稿的全部候选。
            #   「留 → 去已有页 X」的替换稿带着 X 原有的候选，那些不属于原页；
            #   直接取并集会让守恒判据凭空报「越界」，守卫自己变假红。
            #   取交集既不漏（搬来的必在 source 里），也把目标页原有候选排除。
            moved |= set(_cand_ids(other_fm)) & source
            movers.append(f".drafts/{other_slug}.md")

        outside = (retired | moved) - source
        if outside:
            return (f"删页提案越界：候选 %s 不属于 {page.relative_to(r).as_posix()}，"
                    "却出现在废弃或搬迁清单里（原页只有 %s）；本动作没有落盘"
                    % ("、".join(str(i) for i in sorted(outside)),
                       "、".join(str(i) for i in sorted(source)) or "空"))
        both = retired & moved
        if both:
            return (f"删页提案冲突：候选 %s 既被标废弃又被搬去 %s，两边都以为"
                    "自己处理了；本动作没有落盘"
                    % ("、".join(str(i) for i in sorted(both)), "、".join(movers) or "?"))
        missing = source - (retired | moved)
        if missing:
            return (f"删页提案漏了去向：候选 %s 既没废弃也没搬走（%s 的 retire 与 "
                    "%s 的 candidates 都没有它）；本动作没有落盘"
                    % ("、".join(str(i) for i in sorted(missing)),
                       f".drafts/{draft_slug}.md", "、".join(movers) or "搬迁稿"))
        if live_ids is not None:
            ghost = moved - set(live_ids)
            if ghost:
                return (f"删页提案里有幽灵候选 %s：已丢弃的候选不能挂进目标页 "
                        "%s；本动作没有落盘"
                        % ("、".join(str(i) for i in sorted(ghost)),
                           "、".join(movers) or "?"))
    return ""


def commit() -> tuple:
    """把 `.drafts/` 里的草稿**整批**验收入库（改动 F 的物理动作）。

    返回 (ok, payload_or_err)。成功 payload =
    `{committed, merged, edited, deleted, dangling, broken_links, relink,
    needs_approval, needs_decision, pages, new_sources, new_backlinks,
    duplicate_candidates, archived}`。

    ★ 第九步：返回前会做一次**批末断链处置**（详见 `relink_after_commit`）。它会
      往 `.drafts/` 写新一轮补链草稿，所以本函数返回后 `.drafts/` **不一定为空**，
      `approval_active()` 可能重新为真。调用方（视图 / e2e）必须看 `needs_approval`，
      不能默认「commit 完就没事了」。
    ★ 第十步：**单份合法 ≠ 跨稿守恒**。`_validate_draft` 逐份验的是「这份草稿
      能不能入库」，本步的删除稿 + 多份搬迁稿还有一层「原页清单 = 迁走 ⊎ 废弃」。
      所以在 ① 全验之后、② 全动之前，额外过一次 `check_retire_conservation()`。
      不指望逐份预检替它兜底，也不等写盘后再发现（那时已经产生孤儿）。

    ★ **先全验、后全动**（本函数存在的全部理由）：先用 `_validate_draft` 把每一份
      预检一遍，全部通过才逐份 `approve()`。直接循环 approve 的错法是「前两份入库
      了、第三份失败」，状态变成「一半在 wiki、一半还是草稿」——而每一步单看都成功，
      没有任何一处会报错。
    ★ 拒绝时**一个字节不动**，并点名是第几份、为什么：批量动作的失败必须能直接指到
      要修的那份草稿上，否则人得自己逐份试。
    ★ 不碰候选 `status`、不碰编译判据：`ingest._compiled_ids()` 扫的是**产物**
      frontmatter，入库后那批候选已被页的 `candidates:` 记住，不会重复编译。
    ★ 预检后仍失败只可能是并发改动（人在另一端改了同一份草稿）——此时**返回失败**
      而不是假装成功；commit 半途失败必须是显式错误。
    """
    drafts = list_drafts()
    if not drafts:
        return False, "没有待审批草稿"

    # ① 全验：任何一份不过就整体拒绝（此刻还没动过一个字节）
    for i, d in enumerate(drafts, 1):
        ok, why = _validate_draft(d["slug"])
        if not ok:
            return False, f"第 {i} 份草稿 {d['slug']} 校验未通过：{why}"

    # ①b 同一目标只许一份并入稿（2026-11-13 用户实测：D1 / D8 先后并入
    #   「AI泡沫」，生成两份同名草稿、内容各缺一半；两份都提交时后一份覆盖
    #   前一份，先入库的候选**静默消失**）。逐份 `_validate_draft` 看不出来：
    #   每份单独都合法，错的是「同一个 merges 目标有两份稿」这件事。
    #   ★ 判据只看 frontmatter 的 merges（文件名不可靠：旧版改判稿沿用源稿名）。
    by_target = {}
    for d in drafts:
        tgt = str(d.get("merges") or "").strip()
        if not tgt:
            continue
        if tgt in by_target:
            log.warning("wiki 批量入库被重复并入守卫拒绝：%s / %s 都并入 %s",
                        by_target[tgt], d["slug"], tgt)
            return False, (f"两份草稿都要并入同一页 {tgt}（{by_target[tgt]} / "
                           f"{d['slug']}）：先并成一份再提交（审批对话可把两稿"
                           "合并），否则后一份会覆盖前一份，先入库的候选会丢")
        by_target[tgt] = d["slug"]

    # ①c 跨稿守恒（第十步）：逐份合法不代表「候选去向编排」合法。
    #    ★ 不传 live_ids：commit 是纯文件批，live 由提案阶段守卫保证；
    #      这里只验集合守恒，避免本模块为一个判据破例去读 DB。
    why = check_retire_conservation(drafts)
    if why:
        log.warning("wiki 批量入库被跨稿守恒守卫拒绝：%s", why)
        return False, why

    # 入库前记下索引规模、跨页候选、以及**本来就存在的断链**（第九步作用域）。
    old_sources = _json_map("meta/sources.json")
    old_backlinks = _json_map("wiki/backlinks.json")
    duplicate = _cross_page_candidates(drafts)
    # ★ 不传 backlinks → `broken_links()` 自己全量重扫当前 `wiki/`。读可能过期的
    #   backlinks.json 会让「人手工改过页但没重建索引」的历史断链被误判成本批新增，
    #   于是又去自动改人没要求处理的旧链接。批前真相必须是**真实产物**。
    before_broken = broken_links()

    # ★ 第十步：入库前先按草稿快照记下「这次到底搬了几条去哪、废了几条」。
    #    approve 之后草稿就没了，再想统计只能从 commit 结果里猜。
    # ★ 「搬走」只算**从被删页来的**那部分：目标页原有候选也在替换稿的
    #   `candidates` 里，全算会把「1 条已搬走」说成「2 条」（同守恒守卫的口径）。
    sources_by_target = retire_sources(drafts)
    retire_moved = []
    for d in drafts:
        if not d.get("retire_from"):
            continue
        allowed = sources_by_target.get(d["retire_from"], set())
        retire_moved.append({
            "slug": d["slug"], "target": d["retire_from"],
            "candidates": [i for i in d["candidates"] if i in allowed],
        })
    retire_retired_n = sum(len(d["retire"]) for d in drafts if d.get("delete"))

    # ② 全动
    committed, merged, edited, deleted, retire_notes = [], [], [], [], []
    for d in drafts:
        ok, payload = approve(d["slug"])
        if not ok:
            return False, (f"{d['slug']} 入库失败（预检已通过，疑似并发改动）：{payload}")
        if payload.get("deleted"):
            deleted.append(payload["page"])
            # ★ 旧格式删除稿的回退不能只躺在日志里：commit 返回值要带着它，
            #   让 toast / 面板能说出来（「加兜底前先问出错时谁会知道」）。
            if payload.get("retire_legacy"):
                retire_notes.append(f"{payload['page']}：{payload['retire_note']}")
        elif payload.get("edited"):
            edited.append(payload["page"])
        elif payload.get("merged"):
            merged.append(payload["page"])
        else:
            committed.append(payload["page"])

    # ★ commit 是整批：此刻所有草稿都已入库，带标记的页一张不剩，标记没有意义；
    #   全部清掉顺带治好历史遗留的幽灵 slug（改名/退稿没清干净的旧账）。
    write_approved([])

    # ③ 织网：两个索引都全量重建（approve 内部已重建 sources，这里再对齐一次）
    sources = rebuild_sources_index(mark_log=False)
    backlinks = rebuild_backlinks_index(mark_log=False)
    # ★ 第九步：批末用**最终态**重扫断链，不用逐份 approve 的快照。快照的错法是
    #   「删页稿排在引用页编辑稿之前时，后者已经修好链接，返回值却仍报旧断链」
    #   —— 与第七步 over_split 把替换稿混进统计同形。
    broken = broken_links(backlinks)
    # ★ 第八步契约保持：`dangling` 仍是 `{被删页 rel: [引用页 rel]}`，只报告不清洗。
    #   自动补链改的是 `.drafts/` 副本，commit 当刻 `wiki/` 一个字节没动，所以
    #   「断链留在 backlinks.json 里、仍看得见」这条旧断言继续成立。
    dangling = {}
    for rel in deleted:
        if broken.get(Path(rel).stem):
            dangling[rel] = broken[Path(rel).stem]
    chat_reset = _reset_approval_chat()
    n_all = len(committed) + len(merged) + len(edited) + len(deleted)
    log_append("commit", ".drafts/",
               f"{n_all} 页入库（新页 {len(committed)}"
               f" · 合并 {len(merged)}" + (f" · 编辑 {len(edited)}" if edited else "")
               + (f" · 删除 {len(deleted)}" if deleted else "") + "）")
    # ★ 只自动处理**本批新造成**的断链（批末 − 批前）。全量 `broken` 仍原样返回
    #   作报告，但历史断链不自动改 —— 人没要求处理的旧链接，程序不擅自替他决定。
    new_broken = {k: v for k, v in broken.items() if k not in before_broken}
    # ⑨ 批末断链处置放在 commit 记完之后：日志顺序是「入库 → 补链」，且补链稿
    #    属于**下一轮**审批，不该混进本轮 `_archive_commit` 的批次边界。
    relink = relink_after_commit(new_broken, deleted)
    return True, {
        "committed": committed,
        "merged": merged,
        "edited": edited,
        # ★ 删页单列，不混进 edited：删掉的页在 `wiki/` 里**不存在了**，
        #   混进去会让「这几页入库了」这句话变成假的（点开就是 404）。
        "deleted": deleted,
        # ★ `{被删的页: [还链向它的页]}`：删页之后唯一会「悄悄坏掉」的关系。
        #   只报告、不清洗（清洗掉等于把「谁还在提这件事」抹掉）。
        "dangling": dangling,
        # ★ 全量断链（最终态）另立字段，不塞进 dangling —— 后者是第八步既有契约。
        "broken_links": broken,
        # ★ 三段递进的产物：auto 做了什么、ask 带哪三动作、blocked 为什么没做、
        #   guard_errors 一致性守卫有没有响。
        "relink": relink,
        # ★ commit 后 `.drafts/` 可能因自动补链重新非空：浮层要靠这个标记重新打开，
        #   否则草稿静默生成、人看不见（「待你裁决」计数也会漏掉这批）。
        "needs_approval": bool(relink["drafts"]),
        "needs_decision": bool(relink["ask"] or relink["blocked"]
                               or relink["guard_errors"]),
        "pages": committed + merged + edited + deleted,
        "new_sources": len([k for k in sources if k not in old_sources]),
        "new_backlinks": sum(len(v) for k, v in backlinks.items()
                             if backlinks.get(k) != old_backlinks.get(k)),
        "duplicate_candidates": duplicate,
        "chat_reset": chat_reset,
        # ★ 第十步：「M 条已搬去 X、N 条已废弃」要单独说，不能并进「已入库」。
        #   notes 里是旧格式删除稿的显式回退，非空即代表逐条编排没生效。
        "retire": {
            "moved": retire_moved,
            "moved_n": sum(len(x["candidates"]) for x in retire_moved),
            "retired_n": retire_retired_n,
            "notes": retire_notes,
        },
    }


def _reset_approval_chat() -> bool:
    """批次落定后清空审批对话，下一轮审批从零开始（2026-11-22 用户拍板）。

    ★ 旧行为是 `archive_approval_conversation()`：往同一条固定 source_id 的
      对话里追加「本轮入库 N 篇」的批次边界，于是跨轮累积、越积越长。现在
      commit 成功即整条删掉；批次本身在 `data/wiki/log.md` 的 `commit` 记录里
      已有据可查，不靠聊天历史存档。
    ★ 清空失败**不影响 commit 结果**：页已经入库了，不能因为 DB 里一条对话
      删不掉就当没入库。但必须留日志 —— 静默吞掉才是真问题。
    ★ 函数内 import：`wiki_agent` 顶层 `from core import wiki`，这里顶层反向 import
      会成环（本模块只碰文件，不该被 DB 模块拖着）。
    """
    try:
        from core import wiki_agent as wa
        return wa.clear_approval_conversation()
    except Exception:      # noqa: BLE001 —— 清空失败不该让入库回滚
        log.exception("审批对话清空失败（入库已完成，仅对话历史未清）")
        return False


def save_draft(slug: str, body: str, *, drop_fm=(), set_fm=None) -> tuple:
    """保存草稿编辑（润色正文，不动 frontmatter 的结构字段）。

    返回 (ok, payload_or_err)。payload = {chars, slug}。

    ★ 为什么只改正文不动 frontmatter：frontmatter 是机器管理的结构信息（sources/updated/
      confidence），由审批和编译流程维护；人编辑的是「我的理解」部分（即正文）。设计文档 §5.1
      把这种编辑叫「润色」，语义和入库页编辑不同——草稿还没入 wiki，怎么改都不影响反向映射。
    ★ 第十三步：`drop_fm` 让 `set_polish` 在**真重编成功**时顺手剥掉 `stale`
      瞬时标记（按行删，不重新序列化），不额外写一次文件。
    ★ 2026-11-22：`set_fm` 写**机器状态字段**（当前唯一用途是 `POLISH_ERR_FM` 失败原因），
      与 `drop_fm` 同一通道、同一原子写：失败原因必须和「正文未改 / stale 仍在」一起落盘，
      分开两次写会留下「标记说了但原因没写」的半截真相。人改的正文仍不会被它碰。
    ★ 记账走 log.md 的 `edit-draft` 动作（设计文档 E2），和 approve/discard 同一份日志。
    """
    if not _safe_slug(slug):
        return False, f"非法草稿名：{slug!r}"
    r = root()
    src = r / ".drafts" / f"{slug}.md"
    if not src.exists():
        return False, f"草稿不存在：{slug}"

    text = src.read_text(encoding="utf-8")
    # ★ 定位 frontmatter 结束位置：第二个 `---` 之后。保留原文不重新序列化，避免格式漂移。
    #   parse_front 已经验证过 frontmatter 存在（草稿都是编译产物，必然有），这里只找位置。
    try:
        fm_end = text.index("---", text.index("---") + 3) + 3
    except ValueError:
        return False, f"草稿格式异常（无 frontmatter）：{slug}"
    fm_text = text[:fm_end]
    for key in drop_fm:
        # `_strip_fm_key` 会把结尾补成 `---\n`；这里收回去，保持和未剥时同一字节形状。
        fm_text = _strip_fm_key(fm_text, key).rstrip("\n")
    # ★ 机器字段写入走同一通道：`_set_fm_line` 按行替换/追加，不重序列化。
    #   值先压成单行（模型错误可能带换行/冒号），frontmatter 一行一个键。
    for key, value in (set_fm or {}).items():
        flat = " ".join(str(value or "").split())[:300]
        fm_text = _set_fm_line(fm_text.rstrip("\n"), key, flat).rstrip("\n")
    new_text = fm_text + "\n" + (body or "")
    if not new_text.endswith("\n"):
        new_text += "\n"
    src.write_text(new_text, encoding="utf-8")

    log_append("edit-draft", f".drafts/{slug}.md", f"{len(body)} 字")
    return True, {"chars": len(body), "slug": slug}


# ------------------------------------------------------------------ 正文形态 A（第十三步）
#: 从「候选明细素材」里抽 `#<id>`（程序校验用，不是给人看的）。只认顶层 `- \`#N\``
#: 行，不会把缩进的「原话 / 出处」续行误当成另一条候选。
_LIST_ID_RE = re.compile(r"^\s*-\s+`#(\d+)`", re.M)

#: 候选来源的中文标签（polish 素材 / 溯源区共用一份口径，别各写一遍）。
_QF_ZH = {"mine": "我的原话", "highlight": "划出的原文", "article": "正文事实"}


def candidate_list_text(ids: list, details: dict) -> str:
    """按 frontmatter `candidates` 顺序渲染**候选明细素材**：纯喂模型，不落盘。

    ★ 第十三步 A：这份文本**不再写进正文**。它只是 polish 的模型输入，以及
      `set_polish` 守卫的比对底本。正文 = 一段可轻微瑕疵的连贯导读；每条判断 /
      素材 / 在意点的**精确出处**由详情页折叠溯源区读 `IngestCandidate` 字段负责。
    ★ `ids` 与 frontmatter **一一对应**：id 不在 details（幽灵 / 已丢弃）也照列并
      标注出来 —— 静默漏掉一条比多一行危险得多，人会把「素材里没有」当成
      「这页不含它」。
    ★ 素材与写回共用这一份，别一处算一处（漏一处就是「模型照旧清单写、
      文件却换了新清单」）。
    """
    out = []
    if not ids:
        out += ["（本页暂无候选，以 frontmatter 的 `candidates` 为准。）", ""]
    for i in ids:
        d = details.get(i) or {}
        if not d:
            out.append(f"- `#{i}` （候选不存在或已丢弃）")
            continue
        out.append(f"- `#{i}` {(d.get('claim') or '').strip()}".rstrip())
        # ★ 第十三步：把候选的 `quote_from`（谁说的）显式喂进素材。三块下沉后，
        #   「判断 / 素材 / 在意」的区分就靠这一行给模型，不再靠正文分块。
        qf = str(d.get("quote_from") or "").strip()
        out.append(f"  - 来自：{_QF_ZH.get(qf, qf or '未标注')}")
        quote = (d.get("quote") or "").strip()
        if quote:
            out.append(f"  - 原话：{quote[:200]}")
        out.append(f"  - 出处：{d.get('source') or '出处未知'} · "
                   f"{d.get('published_at') or '发布时间未知'}")
    return "\n".join(out)


def _listed_ids(material: str) -> list:
    """候选明细**素材**里声明了哪些 id（守卫比对用，不从正文取）。

    ★★ ⚠️K：形态 A 下正文没有清单。若这里仍从「要写回的正文」取 id，恒得
      `[]`，空候选页 `[] == []` 会假绿。所以入参必须是喂模型的那份素材。
    """
    head, _ = split_two_layer(material)
    return [int(x) for x in _LIST_ID_RE.findall(head)]


#: 「老格式正文」的唯一读侧判据：出现 Markdown 二级及以上结构标题。
#: ★ 不枚举旧标题字面量 —— 三块（`## 我的判断` / `## 事实 / 他人的观点` /
#:   `## 我的在意点`）已整套退役，新代码再引用退役常量就是留迁移债；而旧标题
#:   一旦改名/新增，枚举式判据会静默漏判。形态 A 的正文 **不含任何结构标题**，
#:   所以「有标题 = 老页」是充分的，且不随旧格式漂移。
_LEGACY_HEAD_RE = re.compile(r"^[ \t]*#{2,}[ \t]+\S")


def is_legacy_body(body: str) -> bool:
    """这页正文是不是清零前的老格式（三块 / 旧双层），要走「老页回退」。

    ★★ 2026-11-08 实读纠偏（规格主路径 ≠ 真实库主路径，第十二步踩过一次）：
      真实 `data/wiki/wiki/` 32 页里 **31 页是三块格式、0 页含 `## 候选清单`
      或 `## 通顺话`**。原先 `views.page()` 只拿 `BODY_HEAD`/`POLISH_HEAD`
      两个字形判老页，会把真实库唯一主路径静默标成「结论」、还丢掉可见说明。
      ⇒ 判据改为「正文有没有结构标题」，并把口径收在这一个函数里：
      详情页读侧 / 静态守卫 / 迁移测试都调它，别再各写一遍。
    """
    return any(_LEGACY_HEAD_RE.match(line) for line in (body or "").splitlines())


def split_two_layer(body: str) -> tuple:
    """把**清零前老页 / 旧双层草稿**的正文拆成 (权威 body, 投影 polish)。

    ★ 第十三步 A 后降级为**只读兼容判据**：只用于认老格式（以 `## 通顺话`
      为界），任何写回路径都不许再产出这两个字形。新格式正文就是纯 polish，
      老页 / 新页的区分统一走 `is_legacy_body()`，本函数只负责拆双层。
    """
    text = body or ""
    idx = text.find(POLISH_HEAD)
    if idx < 0:
        return text.strip(), ""
    return text[:idx].strip(), text[idx + len(POLISH_HEAD):].strip()


def render_polish_only(polish: str) -> str:
    """形态 A **唯一**的正文写回口径：只写 polish 文本，不写任何结构标题。

    ★ 与 `wiki_agent._new_page_text()`（新建页正文留空）是「同口径拼正文两处」，
      改一处必 grep 另一处；否则新建页仍写清单，只有真审批才暴露分叉。
    ★ 空文本返回空串 —— 「结论还没生成」是一个**可见状态**（读侧会说明），
      不许塞「（通顺话待重生成）」这类占位字进正文，那会污染结论。
    """
    text = (polish or "").strip()
    return text + "\n" if text else ""


def set_polish(slug: str, polish_text: str, material: str) -> tuple:
    """写一页的**正文**（形态 A：纯 polish 一段），并校验素材与分组一致。

    ★ `material` 必填，必须是 `candidate_list_text(frontmatter candidates, ...)`
      生成的候选明细素材；它**绝不落盘**。正文只写 `polish_text`，frontmatter 的
      `candidates` 是分组唯一真值，精确出处由折叠溯源区读 DB 负责。
    ★ 守卫（程序做，不靠调用方自觉）：素材里的 `#id` 必须与 frontmatter 的
      `candidates` 一一对应，对不上就**拒写**并说明差异。旧正文、缺条、多出
      幽灵候选都会在这里被挡住。
    ★★ ⚠️K 防空集合假绿：守卫读的是**素材**而不是「要写回的正文」——
      形态 A 下正文没有清单，沿用旧口径会恒得 `[]`，空候选页 `[] == []`
      就记通过了。素材里必须真的有 `#id` 行才放行。
    ★ 走 `save_draft`：保留 frontmatter、落 `edit-draft` 日志。**成功才剥
      `stale`**（真重编完成，警告才消失）；失败时标记保留、审批浮层可见 ——
      「警告消失 ≠ 内容一致」的同构防线。
    """
    if not _safe_slug(slug):
        return False, f"非法草稿名：{slug!r}"
    src = root() / ".drafts" / f"{slug}.md"
    if not src.exists():
        return False, f"草稿不存在：{slug}"
    fm, _ = parse_front(src.read_text(encoding="utf-8"))
    declared, listed = _cand_ids(fm), _listed_ids(material or "")
    if listed != declared:
        return False, (f"候选明细与 frontmatter 对不上，已拒写（stale 标记保留）："
                       f"frontmatter={declared}、素材={listed}；"
                       f"素材必须由 candidate_list_text 按 frontmatter 生成")
    # ★ 空候选页（declared == listed == []）是合法形态（删页稿 / 无候选页）：
    #   显式认下这条，**不让 `[] == []` 冒充守卫通过**。真有候选却给空素材，
    #   上一行必然拦下（`[] != [1, 2]`）—— 这两条合起来才覆盖全集。
    #   （2026-11-09 删掉原先的 `if declared and not listed`：那行在
    #   `listed == declared` 之后恒为 False，是「看着有守卫、其实不会响」的死代码。）
    ok, why = save_draft(slug, render_polish_only(polish_text),
                         # ★ 成功 = 上次的失败原因已作废，和 stale 同一笔写盘剥掉
                         #   （分开剥会留下「结论好了但还挂着旧错误」的假警报）。
                         drop_fm=(STALE_FM, POLISH_ERR_FM))
    return (True, "") if ok else (False, str(why))


def save_page(page_rel: str, body: str) -> tuple:
    """保存入库页编辑（改判断 / 更新结论）。

    返回 (ok, payload_or_err)。payload = {chars, page}。

    ★ 与草稿编辑的语义差异（设计文档 §5.1）：
      - 草稿编辑是「润色」，还没入 wiki，怎么改都不影响反向映射；
      - 入库页编辑是「改判断」，已经沉淀进知识体系了，所以：
        1. 更新 frontmatter 的 `updated` 字段（告诉人这条结论最后一次修改是什么时候）；
        2. 重建 sources.json（虽然编辑正文通常不改 sources，但保险起见全量重建 ——
           成本可以忽略，且能兜住「人手动改了 sources 字段」的罕见情况）。
    """
    # ★ 先校验路径合法性（不依赖文件是否存在），再检查文件是否存在。
    #   resolve_page 对两种情况都返回 None，这里拆开判断以给出更精确的错误信息。
    s = (page_rel or "").strip().replace("\\", "/")
    if not s or s.startswith("/") or not s.endswith(".md"):
        return False, f"非法页路径：{page_rel!r}"
    r = root()
    try:
        p = (r / s).resolve()
        base = (r / "wiki").resolve()
    except OSError:
        return False, f"非法页路径：{page_rel!r}"
    if p != base and base not in p.parents:
        return False, f"非法页路径：{page_rel!r}"
    if not p.is_file():
        return False, f"页不存在：{page_rel}"

    text = p.read_text(encoding="utf-8")
    try:
        fm_end = text.index("---", text.index("---") + 3) + 3
    except ValueError:
        return False, f"页格式异常（无 frontmatter）：{page_rel}"

    fm_text = text[:fm_end]
    # ★ 更新 `updated` 字段为今天：人改了一页的结论，updated 就该跟着变。
    #   用正则替换而不是解析-重写：保留原有格式和字段顺序，只动 `updated:` 那一行。
    today = timezone.localdate().isoformat()
    if re.search(r"^updated:\s*.+$", fm_text, re.MULTILINE):
        fm_text = re.sub(r"^(updated:\s*).+$", rf"\g<1>{today}", fm_text, flags=re.MULTILINE)
    else:
        # frontmatter 里没有 updated（理论上不应该，但兜住）：追加一行
        fm_text = fm_text.rstrip("-\n") + f"\nupdated: {today}\n---"

    new_text = fm_text + "\n" + (body or "")
    if not new_text.endswith("\n"):
        new_text += "\n"
    p.write_text(new_text, encoding="utf-8")

    log_append("edit-page", page_rel, f"{len(body)} 字")
    rebuild_sources_index(mark_log=False)
    return True, {"chars": len(body), "page": page_rel}


def _first_sentence(body: str) -> str:
    """取正文第一句当索引摘要（80 字内）。

    ★ 跳过标题行 / 引用块 / 表格 / 代码块起始：草稿正文常以 `# 标题` 开头，
      直接拿首行会让索引变成标题的重复；引用块（`>`）是出处说明，也不是摘要。
    ★ `-` / `*` 开头的列表**不跳过** —— 那往往正是结论句（"一条页一个判断"，
      判断常写成列表），跳过它就会抓到更次要的整段文字。
    """
    for line in (body or "").splitlines():
        s = line.strip()
        if not s or s.startswith(("#", ">", "|", "```", "---")):
            continue
        s = re.sub(r"^[-*]\s+", "", s)          # 去掉列表符号，保留内容
        s = re.sub(r"[*_`\[\]]", "", s)
        return s[:80]
    return ""


# ------------------------------------------------------------------ index
def index_upsert(page_rel: str, title: str, summary: str = "", updated: str = "") -> None:
    """在 index.md 的对应 `<domain>` 小节里插入/替换该页那一行。

    ★ 按**页相对路径**认行，不按标题：标题会改、会重名，路径不会
      （同名判断会静默替换掉别人的行 —— 与「按标题裸名认对象」那个老坑同源）。
    """
    r = root()
    p = r / "index.md"
    text = p.read_text(encoding="utf-8") if p.exists() else INDEX_SEED
    parts = page_rel.split("/")
    domain = parts[1] if len(parts) > 2 else "_"
    tail = " · ".join(x for x in (summary.strip(), (updated or "").strip()) if x)
    line = f"- [{title}]({page_rel})" + (f" — {tail}" if tail else "")

    lines = text.splitlines()
    # 1) 删掉该页的旧行（幂等：重复 approve 同一页不会攒出两行）
    lines = [ln for ln in lines if f"]({page_rel})" not in ln]

    # 2) 找 `## <domain>` 小节；没有就在文件末尾新建
    head = f"## {domain}"
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == head)
    except StopIteration:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", head, "", line, ""]
    else:
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].startswith("## ")), len(lines))
        # 插在小节最后一个非空行之后（保持小节内顺序 = 审批顺序）
        ins = end
        while ins > start + 1 and not lines[ins - 1].strip():
            ins -= 1
        if ins == start + 1:
            lines[start + 1:ins] = ["", line]
        else:
            lines.insert(ins, line)
    p.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def index_remove(page_rel: str) -> bool:
    """从 index.md 删掉该页的行。返回是否真的删了（没有这行也不算错）。"""
    r = root()
    p = r / "index.md"
    if not p.exists():
        return False
    text = p.read_text(encoding="utf-8")
    lines = text.splitlines()
    kept = [ln for ln in lines if f"]({page_rel})" not in ln]
    if len(kept) == len(lines):
        return False
    p.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
    return True


# ------------------------------------------------------------------ log
_LOG_RE = re.compile(r"^## \[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})\] ([\w-]+) \| (.+)$")


def log_append(action: str, target: str, note: str = "") -> None:
    """追加一条操作日志。前缀形状是契约（`_LOG_RE` 就是它的解析器）。"""
    r = root()
    p = r / "log.md"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(LOG_SEED, encoding="utf-8")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    block = f"\n## [{ts}] {action} | {target}\n"
    if note:
        block += f"- {note}\n"
    with p.open("a", encoding="utf-8") as fh:
        fh.write(block)


def read_log(limit: int = 50) -> list:
    """解析 log.md 为结构化条目（最新的在前）。解析不了的行直接忽略，不猜。"""
    p = root() / "log.md"
    if not p.exists():
        return []
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            m = _LOG_RE.match(line.strip())
            if m:
                out.append({"date": m.group(1), "time": m.group(2),
                            "action": m.group(3), "target": m.group(4)})
    except OSError:
        return []
    return list(reversed(out))[:limit]


def _has_log_action(action: str) -> bool:
    return any(e["action"] == action for e in read_log(limit=10000))


# ------------------------------------------------------------------ 页 / 反向映射
def pages() -> list:
    """已审批页清单（只扫 `wiki/`，不扫 raw/.drafts）。"""
    r = root()
    out = []
    for p in sorted((r / "wiki").rglob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, body = parse_front(text)
        rel = p.relative_to(r).as_posix()
        out.append({
            "page": rel,
            "domain": p.relative_to(r / "wiki").parts[0] if p.parent != r / "wiki" else "",
            "slug": p.stem,
            "title": fm.get("title") or p.stem,
            "updated": fm.get("updated") or "",
            "confidence": (fm.get("confidence") or "").lower(),
            "sources": fm.get("sources") or [],
            "candidates": _cand_ids(fm),
            # ★ 审批浮层「相关页推荐」的 LLM 目录卡要用（2026-11-11）。加字段不改
            #   `_page_catalog` 的输出：那边只挑自己认的键，S1 目录卡一个字节不变
            #   （已实测：`_page_catalog` 只取 slug/title/domain/summary/candidates）。
            "tags": [str(t).strip() for t in (fm.get("tags") or [])
                     if str(t).strip()],
            "chars": len(body),
            # ★ 一句话摘要：编译时 S1 靠它判断「这组候选是不是在补这一页」。
            #   没有它，S1 只能按标题猜，而标题往往比内容窄得多。
            "summary": _first_sentence(body),
        })
    return out


def tag_counts(all_pages=None, limit=None) -> list:
    """全库标签词表：`[{"tag": "现金流", "n": 3}, ...]`，次数降序、同次数按字面升序。

    ★ 为什么收在这里而不是各写一份：编译期喂 S1 的 `tag_pool`（`ingest._tag_pool`）
      与审批页「输入时提示库里已有的标签」读的是**同一个事实**。各数一遍迟早
      出现「S1 看到的池子和人看到的不是一套」—— 那就等于人在往一个假词表上收敛。
    ★ `all_pages` 让编译链复用已经读好的 `pages()` 结果（`pages()` 会重读全部文件）；
      不传就现读。
    ★ `limit` 只截条数，不改善排序：调用方要的是「最常用的在前」，不是随机 40 个。
    ★ 只数 `wiki/` 下的已入库页 —— 草稿里的标签还没被任何人确认过，拿它当词表
      等于把人还没点头的建议当成既定用词。
    """
    from collections import Counter
    if all_pages is None:
        all_pages = pages()
    c = Counter()
    for p in all_pages:
        for t in (p.get("tags") or []):
            t = str(t).strip()
            if t:
                c[t] += 1
    ranked = sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))
    if limit:
        ranked = ranked[:limit]
    return [{"tag": t, "n": n} for t, n in ranked]


class WikilinkResolver:
    """把正文的 `[[slug]]` 解析成站内链接数据，供 `mdrender.render` 内联渲染。

    ★ 只返回**数据**（href / label / missing / why）；HTML 一律由渲染器 escape
      后套标签。这里拼 HTML 就等于把 frontmatter 里的标题直接塞进 `|safe` 输出。
    ★ 一次 `pages()` 建表：一页里十几个链接，逐个 rglob 会把详情页拖慢。
    ★ 重名当「没这页」并给可见原因，与 `find_page()` 同口径 —— 猜一个等于
      静默把人带到错的页（wiki/invest/x.md 与 wiki/ai/x.md 同时存在时不可判定）。
    ★ `missing` 累积这次渲染没解析上的 slug（去重保序），由详情页面板给**文字**
      说明：正文里的虚线 / tooltip 只是辅助，不能让人靠 hover 才发现死链。
    """

    def __init__(self):
        table, dup = {}, set()
        for p in pages():
            s = p["slug"]
            if s in table:
                dup.add(s)
            else:
                table[s] = p
        self._table, self._dup = table, dup
        self.missing = []

    def __call__(self, slug: str, alias: str = "") -> dict:
        slug = (slug or "").strip()
        p = self._table.get(slug)
        if p is None or slug in self._dup:
            why = "目标页重名，无法判定" if slug in self._dup else "目标页不存在"
            if slug and slug not in self.missing:
                self.missing.append(slug)
            return {"label": alias or slug, "missing": True, "why": why}
        return {
            # ★ 只生成站内相对路径；mdrender 还会再挡一次非 "/" 开头的 href。
            "href": "/wiki/page?p=" + quote(p["page"], safe="/"),
            # ★ 显示目标页的中文标题，不显示 slug —— slug 是存储用的，放
            #   `title` 属性里给人追溯就够（11-08 展示层已定的口径）。
            "label": alias or p["title"] or slug,
            "missing": False,
            "why": "",
        }


def wikilink_resolver() -> WikilinkResolver:
    """详情页 / 审批态 / 预览共用的解析器（每次调用重读一次页清单，不缓存陈旧结果）。"""
    return WikilinkResolver()


# ------------------------------------------------------------- 单页读取（页面用）
def resolve_page(rel: str):
    """页相对路径 → 绝对路径；不在 `wiki/` 下、或文件不存在，返回 None。

    ★ 这是**唯一**接受「外部传入路径」的入口（页面 `?p=` 参数）。判据不是
      「字符串看起来像相对路径」，而是 resolve 之后仍在 `wiki/` 之下 ——
      字符串检查挡不住 `a/../../Meta/ontology.md`（先拼后跳，看着人畜无害）。
    """
    s = (rel or "").strip().replace("\\", "/")
    if not s or s.startswith("/") or not s.endswith(".md"):
        return None
    r = root()
    try:
        p = (r / s).resolve()
        base = (r / "wiki").resolve()
    except OSError:
        return None
    if p != base and base not in p.parents:
        return None
    return p if p.is_file() else None


def read_page(rel: str):
    """读一个已入库页。返回 dict（含正文），读不到返回 None。

    与 `pages()` 的区别：那个只扫清单（不读全文到调用方、不认 `?p=` 输入）。
    """
    p = resolve_page(rel)
    if p is None:
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return None
    fm, body = parse_front(text)
    r = root()
    try:
        relpath = p.relative_to(r.resolve()).as_posix()
    except (ValueError, OSError):
        relpath = (rel or "").strip().replace("\\", "/")
    parts = relpath.split("/")
    return {
        "page": relpath,
        "domain": parts[1] if len(parts) > 2 else "",
        "slug": p.stem,
        "title": fm.get("title") or p.stem,
        "updated": fm.get("updated") or "",
        "confidence": (fm.get("confidence") or "").lower(),
        "sources": fm.get("sources") or [],
        "fm": fm,
        "body": body,
        "text": text,
        "chars": len(body),
    }


def rebuild_sources_index(mark_log: bool = True) -> dict:
    """扫描所有页的 frontmatter.sources，重写 `meta/sources.json`（来源 → 受影响页）。

    ★ 这是**对抗静默漂移**的核心：来源（一篇文章 / 一次对话）变了，靠它才知道要
      复查哪几页。没有它，wiki 只会越写越像 2023 年的世界，而且没人发现。
    ★ 全量重建而不是增量维护：增量维护要在每条写入路径上都记得更新它，漏一处就
      永久对不上；页数量级（几百）全量扫的成本可以忽略。
    """
    r = root()
    mapping = {}
    for pg in pages():
        for src in pg["sources"]:
            s = str(src).strip()
            if s:
                mapping.setdefault(s, [])
                if pg["page"] not in mapping[s]:
                    mapping[s].append(pg["page"])
    p = r / "meta" / "sources.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        try:
            old = json.loads(p.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            old = {}
    else:
        old = {}
    p.write_text(json.dumps(mapping, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                 encoding="utf-8")
    if mark_log and old != mapping:
        log_append("sources", "meta/sources.json",
                   f"{len(mapping)} 个来源 → {len({x for v in mapping.values() for x in v})} 页")
    return mapping


def affected_pages(source_id: str) -> list:
    """某个来源变化后需要复查的页（反向映射查询）。"""
    p = root() / "meta" / "sources.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return []
    return list(data.get(str(source_id).strip(), []))


# ---------------------------------------------------------------- 审批标记
def read_approved() -> list:
    """已审批的草稿 slug 列表（meta/approved.json）。"""
    p = root() / "meta" / "approved.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "[]")
    except (OSError, ValueError):
        return []
    return list(data) if isinstance(data, list) else []


def write_approved(slugs: list):
    """写入已审批的草稿 slug 列表。"""
    p = root() / "meta" / "approved.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(list(slugs), ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8")


def rename_approved(old_slug: str, new_slug: str):
    """草稿改 slug 时把审批标记一起搬过去。

    ★ 标记跟「这一页」走，不跟文件名走：不搬的话 meta/approved.json 里留着
      旧 slug，列表显示「已审 2/9」但一个圈都没勾（2026-11-09 实测）。
    """
    approved = read_approved()
    if old_slug in approved:
        write_approved([new_slug if s == old_slug else s for s in approved])


def drop_approved(slug: str):
    """草稿退稿时清掉它的审批标记（幽灵 slug 会把「已审 x/y」的 x 撑大）。"""
    approved = read_approved()
    if slug in approved:
        write_approved([s for s in approved if s != slug])


def toggle_approved(slug: str) -> bool:
    """切换草稿的审批状态，返回新状态（True=已审批）。"""
    approved = read_approved()
    if slug in approved:
        approved.remove(slug)
        write_approved(approved)
        return False
    else:
        approved.append(slug)
        write_approved(approved)
        return True


def is_approved(slug: str) -> bool:
    """检查草稿是否已审批。"""
    return slug in read_approved()


#: `[[wikilink]]` 的解析式：**定义在 `core.mdlines.WIKILINK`**，这里只留别名。
#: 模型侧（`wiki_loop` 的提示词）约定的形状是 `[[其它页的 slug]]`，允许 `|别名`。
#: 阅读态渲染（`core.mdrender`）也引用同一份，别在这里另写正则。


def rebuild_backlinks_index(mark_log: bool = True) -> dict:
    """扫所有页正文的 `[[wikilink]]`，重写 `wiki/backlinks.json`（被引 slug → 引用页）。

    ★ 与 `rebuild_sources_index` 同一模式：**全量重建**，不增量维护。增量维护要求
      每条写入路径都记得更新，漏一处就永久对不上；页只有几百，全量扫成本可忽略。
    ★ 扫**正文**不扫 frontmatter：`sources` 管「内容从哪来」（`meta/sources.json`），
      反链管「页与页之间怎么连」，而那只出现在通顺话里。
    ★ 自引用不算反链：A 提到 A 不产生 A→A，否则每页都自带一条恒真的噪音。
    ★ **断链也保留**（目标 slug 并不存在时照样记一行）：反链索引的价值之一就是让
      「链过去却没有这页」看得见。悄悄丢掉断链 = 断链永远发现不了。
    ★ 空库也写 `{}`：文件缺失会被读成「索引没建」，而不是「没有反链」。
    """
    r = root()
    mapping = {}
    for p in sorted((r / "wiki").rglob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        _fm, body = parse_front(text)
        rel = p.relative_to(r).as_posix()
        for m in _WIKILINK_RE.finditer(body):
            target = m.group(1).strip()
            if not target or target == p.stem:
                continue
            mapping.setdefault(target, [])
            if rel not in mapping[target]:
                mapping[target].append(rel)
    out = {k: sorted(v) for k, v in mapping.items()}

    p = r / "wiki" / "backlinks.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    old = {}
    if p.exists():
        try:
            old = json.loads(p.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            old = {}
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                 encoding="utf-8")
    if mark_log and old != out:
        log_append("backlinks", "wiki/backlinks.json",
                   f"{len(out)} 个被引页 · {sum(len(v) for v in out.values())} 条反链")
    return out


def backlinks_of(slug: str) -> list:
    """谁引用了这一页（只读索引，不扫库）。索引缺失/坏掉时返回空表，不抛。"""
    p = root() / "wiki" / "backlinks.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return []
    return list(data.get(str(slug).strip(), []))


#: 每个引用页最多展示几句。`more` 报剩余数，由模板可见写明「另有 N 句」，
#: ★ 绝不静默截断 —— 少显示一句和根本没有那句，在人眼里是一样的。
BACKLINK_QUOTES_PER_PAGE = 3


def _wikilink_display(m) -> str:
    """`[[slug|显示名]]` → 显示名，`[[slug]]` → slug。

    mdrender 不处理 wikilink，字面 `[[...]]` 会直接印在屏幕上；这里还原的是
    写作者想让人读到的那个词。**只替换链接本身，句子其余字符一个不动** ——
    改写了就不叫「引用的那句话」。
    ★ 拆法收在 `mdlines.wikilink_parts()`：阅读态渲染要用同一份别名口径。
    """
    _slug, alias = _wikilink_parts(m)
    return alias or m.group(1).strip()


def _quote_sentences(body: str) -> list:
    """正文 → 「引用句」候选列表。

    ★ 第十三步 ⚠️J：形态 A 下 polish 是**一段成文**，按 `splitlines()` 取会把
      整段当成一句，「3 句上下文」退化成一大块。这里按句末标点 + 换行切：
      新格式能拆出真正的那句话，清零前老页的行式三块 / 双层也照旧按行成立。
    ★ 不把英文句点 `.` 当边界：小数 `3.5`、slug 里的点会被切坏。
    """
    marked = re.sub(r"([。！？!?；;])\s*", r"\1\n", body or "")
    return [ln.strip() for ln in marked.splitlines() if ln.strip()]


def backlink_contexts(slug: str, per_page: int = BACKLINK_QUOTES_PER_PAGE) -> list:
    """谁引用了这一页，以及引用的那句话（第十二步，只读、不写文件）。

    返回 `[{"page": rel, "title": str, "quotes": [str, ...],
             "more": int, "missing": bool}, ...]`。

    ★ 索引格式**不变**：页列表仍来自 `backlinks_of(slug)` 的字符串数组。
      `broken_links()` / `commit()` 的 new_backlinks / 删页 dangling /
      `_relink_plan()` / `test_commit` / 两条 e2e 都吃这个形状。引用句在打开
      详情页时现扫正文得到 —— 页几百篇，全量扫成本可忽略（`rebuild_backlinks_index`
      自己就是这么写的）；为一段展示文字改索引，会牵动 4 组判据契约，不值。
    ★ 与 `rebuild_backlinks_index()` 用同一份 `_WIKILINK_RE`、同一个
      `group(1).strip() == target` 判据，不另写一套（同口径两处必漏一处）。
      正文层与 polish 层都扫：链接主要出现在通顺话里，但写在权威层也得认。
    ★ `missing=True` 表示「索引记着这页引用过，但正文里找不到引用句了」：
      人手工改过页 / 索引过期 / 页被删都会造成。模板必须给可见提示 ——
      空白会被读成「本来就没有上下文」，静默失败最贵。
    """
    target = str(slug or "").strip()
    if not target:
        return []
    limit = max(0, int(per_page or 0))
    rows = []
    for rel in backlinks_of(target):
        pg = read_page(rel)
        if pg is None:
            rows.append({"page": rel, "title": Path(rel).stem,
                         "quotes": [], "more": 0, "missing": True})
            continue
        quotes = []
        for line in _quote_sentences(pg["body"]):
            if any(m.group(1).strip() == target
                   for m in _WIKILINK_RE.finditer(line)):
                quotes.append(_WIKILINK_RE.sub(_wikilink_display, line).strip())
        rows.append({
            "page": rel,
            "title": pg["title"] or Path(rel).stem,
            "quotes": quotes[:limit],
            "more": max(0, len(quotes) - limit),
            "missing": not quotes,
        })
    return rows


# ------------------------------------------------------------------ 断链守卫与自动补链（第九步）
#: 自动改指向的相似度门槛。低于它会把 "moat" 和 "note" 也判成近似 —— 改错页比
#: 留着断链更贵：改错了正文照样能读、不报错，而断链至少在索引里看得见。
_RELINK_CUTOFF = 0.8

#: 第三段「带方案问人」的三个动作。**判据收在这一处**：核心层出方案、面板渲染、
#: e2e 断言都认这一份，别各写一遍（前几步踩过多次「同口径两处必漏一处」）。
RELINK_ACTIONS = ("改指向", "降级为纯文本", "先拆走再删")


def broken_links(backlinks: dict = None) -> dict:
    """批末**权威**断链清单：`{不存在的 target slug: [引用页 rel, ...]}`。

    ★ 判据只这一份：target slug 是否在 `wiki/**/*.md` 的文件名集合里。
      `rebuild_backlinks_index` 刻意保留断链，就是为了这里能一次扫出来。
    ★ 不传 backlinks 就自己全量重扫（不读可能过期的索引）——「读真实产物」才算
      验证；`commit()` 已扫过一遍，传进来避免重复扫。
    """
    if backlinks is None:
        backlinks = rebuild_backlinks_index(mark_log=False)
    stems = {p.stem for p in (root() / "wiki").rglob("*.md")}
    return {k: sorted(v) for k, v in (backlinks or {}).items() if k not in stems}


def _relink_plan(broken: dict, deleted_rels=()) -> dict:
    """三段递进分类（第九步 §3.2）。输入是**本批新产生**的断链清单。

    ★ 分界线只有一条：target 是不是**本批刚删掉的页**。
      - 是 → 第三段：人明确说了「这页不要了」，别的页还引用它的结论，程序不猜，
        带三个动作问人（不落草稿，不占 `.drafts/`）。
      - 不是 → 编译 / 编辑写正文时提错 slug，是笔误，程序能自动判：
        唯一高相似现存页 → 第一段自动改指向；
        多个候选难分 → 也归第三段（人挑一个）；
        没有候选 → 第二段自动降级成纯文本。
    ★ 全程零 LLM：slug 相似度是确定性判据，让模型猜「该指向哪页」会编造不存在
      的 slug；本模块的边界也只碰文件、不碰 LLM。
    ★ 只吃**本批新增**（批末 − 批前）。★ 2026-11-06 实测纠正：首版吃全量清单，
      结果人只是编辑一页，系统就把库里早就存在的 `[[cost-engineering]]` 自动改了、
      凭空冒出一份补链草稿，把审批流塞满（e2e_wiki_edit / e2e_wiki_split 当场红）。
      全量清单的价值是**报告**（`payload["broken_links"]` 与日志里都看得见），
      不是让程序擅自去改人没要求处理的旧链接。
    ★ 副作用：人退稿后，那条断链已变成「历史」，下次 commit 不再自动生成草稿 ——
      这正好避免了「每次 commit 都重提同一份被退掉的稿」的骚扰。想再处理时，
      面板的断链清单里点三动作即可（见 `apply_relink_action`）。
    """
    stems = sorted({p.stem for p in (root() / "wiki").rglob("*.md")})
    deleted = {Path(r).stem for r in (deleted_rels or ())}
    auto, ask = [], []
    for target, refs in sorted((broken or {}).items()):
        row = {"target": target, "referrers": list(refs)}
        if target in deleted:
            row.update({"reason": "deleted", "candidates":
                        difflib.get_close_matches(target, stems, n=3,
                                                  cutoff=_RELINK_CUTOFF),
                        "actions": list(RELINK_ACTIONS)})
            ask.append(row)
            continue
        matches = difflib.get_close_matches(target, stems, n=2, cutoff=_RELINK_CUTOFF)
        if len(matches) == 1:
            row.update({"action": "retarget", "new_slug": matches[0]})
            auto.append(row)
        elif len(matches) >= 2:
            row.update({"reason": "ambiguous", "candidates": matches,
                        "actions": list(RELINK_ACTIONS)})
            ask.append(row)
        else:
            row.update({"action": "demote"})
            auto.append(row)
    return {"auto": auto, "ask": ask}


def _rewrite_wikilinks(text: str, items: list, display: dict = None) -> str:
    """按方案改一份草稿正文里的 `[[wikilink]]` —— **一个字节都不碰 frontmatter**。

    ★ 用 `_WIKILINK_RE`（全项目唯一一份）精确匹配 `group(1) == target`：
      别的链接不碰，别名 `|xxx` 原样保留。
    ★ 降级显示文字：别名优先，其次 `display[target]`（通常是归档页标题），
      最后才退回裸 slug。★ 2026-11-13 收口：退稿解包 `unwrap_wikilinks` 早已
      用页标题，这里还留裸 slug，同一件事两套显示文本（11-11 实测像乱码）。
    """
    lookup = {it["target"]: it for it in items}
    titles = display or {}
    m = _FM_RE.match(text)
    head, body = (text[:m.end()], text[m.end():]) if m else ("", text)

    def sub(match):
        raw = match.group(0)
        target = match.group(1).strip()
        it = lookup.get(target)
        if not it:
            return raw
        inner = raw[2:-2]
        alias = inner.split("|", 1)[1] if "|" in inner else ""
        if it.get("action") == "retarget":
            return "[[%s%s]]" % (it["new_slug"], ("|" + alias) if alias else "")
        return alias or titles.get(target) or target

    return head + _WIKILINK_RE.sub(sub, body)


def _write_relink_drafts(plan: dict) -> tuple:
    """把第一/二段的自动方案落成 N 份 `edits:` 草稿（复用 `pull_page`）。

    返回 `(written, blocked, by_page)`：written = 成功落草稿的 slug；
    blocked = `[{page, why}]`（不能静默跳过）；by_page = `{slug: [方案项]}` 给守卫。

    ★ 一个引用页只出**一份**草稿：同一页可能被多个断链 target 引用，按页聚合
      后一次改完，否则第二份会撞 `pull_page` 的「同名草稿」拒绝。
    ★ 自动 ≠ 静默：改的是别人页的正文，一律走 `pull_page` 副本 + `edits:` + 人
      commit，绝不直接写 `wiki/`。
    """
    by_page = {}
    for it in plan.get("auto") or []:
        for rel in it.get("referrers") or []:
            by_page.setdefault(Path(rel).stem, []).append(it)
    # ★ 降级显示文字与 `unwrap_wikilinks`（退稿解包）同一口径：查得到归档页
    #   标题就用标题，查不到（笔误型断链，目标页从来没存在过）才退回裸 slug。
    display = {it["target"]: archived_title(it["target"])
               for it in plan.get("auto") or []}
    written, blocked = [], []
    for stem, items in sorted(by_page.items()):
        ok, payload = pull_page(stem)
        if not ok:
            blocked.append({"page": stem, "why": str(payload)})
            continue
        p = root() / ".drafts" / f"{stem}.md"
        try:
            p.write_text(_rewrite_wikilinks(p.read_text(encoding="utf-8"), items,
                                            display=display), encoding="utf-8")
        except OSError as exc:
            # 这份草稿是刚建的，失败就收回 —— 不能留一份「标了 edits 却没改」的稿
            try:
                p.unlink()
            except OSError:
                pass
            blocked.append({"page": stem, "why": f"写草稿失败（{exc}）"})
            log.error("补链：写 %s 失败 %s", stem, exc, exc_info=True)
            continue
        written.append(stem)
    return written, blocked, by_page


def _check_relink(by_page: dict, written: list) -> list:
    """一致性守卫：**读真实草稿文件**，逐条证明每处自动断链都有明确落点。

    ★ 「警告消失 ≠ 内容一致」的同构：不是把断链从清单里擦掉就算修好了，必须
      逐条比对「权威断链清单 ↔ 草稿正文」，证明那处 `[[target]]` 真的不在了。
    ★ 拦的是「程序以为自己改了、其实没改」——`_rewrite_wikilinks` 若漏一处，
      这里会点名是哪页哪个 target，而不是让提示悄悄变干净。
    """
    problems = []
    for stem in written:
        targets = {it["target"] for it in (by_page.get(stem) or [])}
        p = root() / ".drafts" / f"{stem}.md"
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(f"{stem}: 草稿读不到（{exc}）")
            continue
        _fm, body = parse_front(text)
        for m in _WIKILINK_RE.finditer(body):
            if m.group(1).strip() in targets:
                problems.append(f"{stem}: 仍链向 [[{m.group(1).strip()}]]")
    return problems


#: 第三段「带方案问人」的待决清单落在这里。**落盘不落 session**：人没处理就该
#: 一直看得见，切页面 / 刷新 / 重启都不该让「还有 N 处断链等你决定」悄悄消失。
RELINK_ASK_REL = "meta/relink-ask.json"


def relink_ask() -> dict:
    """第三段的待决清单 + 未能自动处理的页 + 守卫报错（面板直接渲染）。

    ★ `has_items` 是给模板的显式布尔判据：模板写 `{% if relink_ask.has_items %}`
      而不是 `{% if not relink_ask %}` —— 上下文变量万一缺失，后者会静默走进
      「没有待决项」分支，正是本项目栽过的「缺变量被当成空值」。
    ★ 读不到 / 坏 JSON 返回空三件套，不抛：面板不能因为一个坏文件整页 500。
    """
    p = root() / RELINK_ASK_REL
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        data = {}
    ask = list(data.get("ask") or [])
    blocked = list(data.get("blocked") or [])
    errors = list(data.get("guard_errors") or [])
    return {"ask": ask, "blocked": blocked, "guard_errors": errors,
            "has_items": bool(ask or blocked or errors)}


def _write_relink_ask(ask, blocked, errors):
    """写待决清单；一项都没有就**删档**（下线就删档，不留一个空横幅）。"""
    p = root() / RELINK_ASK_REL
    if not (ask or blocked or errors):
        try:
            p.unlink()
        except OSError:
            pass
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"ask": ask, "blocked": blocked,
                             "guard_errors": errors},
                            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def relink_after_commit(broken: dict, deleted_rels=()) -> dict:
    """commit 批末的唯一入口：三段递进 → 落草稿 → 一致性守卫 → 如实报告。

    ★ 失败**不回滚**（页已经入库了，跟 `_archive_commit` 同款「记账失败不该让
      入库回滚」），但必须显式写进返回值与日志 —— 静默失败最贵。
    """
    plan = _relink_plan(broken, deleted_rels)
    written, blocked, by_page = _write_relink_drafts(plan)
    problems = _check_relink(by_page, written)
    if problems:
        log.error("补链一致性守卫未通过：%s", problems)
    if written or blocked or problems:
        log_append("relink", ".drafts/",
                   f"自动补链 {len(written)} 页 · 未处理 {len(blocked)} 页"
                   + (f" · 守卫未通过 {len(problems)} 处" if problems else ""))
    _write_relink_ask(plan["ask"], blocked, problems)
    return {**plan, "drafts": written, "blocked": blocked,
            "guard_errors": problems}


def apply_relink_action(target: str, action: str, new_slug: str = "") -> tuple:
    """人点了第三段方案里的一个动作 → 把那条断链转成 `edits:` 补链草稿。

    ★ 与自动层**同一条通道**（`_write_relink_drafts` / `_check_relink`）：人点选
      和程序自动只差「谁决定的」，落地方式完全相同，不另造一套写盘逻辑。
    ★ `retarget` 的目标页必须**真的存在**：面板给的候选来自相似度，但请求可以
      手改，不校验就会「用一条断链换另一条断链」，而且不会报错。
    ★ 落不出草稿就**当失败返回**，不清理待决清单 —— 否则清单消失了、断链还在，
      人以为自己已经处理过（最贵的那种静默失败）。
    """
    target, action, new_slug = ((target or "").strip(), (action or "").strip(),
                                (new_slug or "").strip())
    if action not in ("retarget", "demote"):
        return False, f"不认识的动作：{action!r}"
    refs = (broken_links() or {}).get(target) or []
    if not refs:
        return False, f"[[{target}]] 已经没有断链了，不用补"
    if action == "retarget":
        if not new_slug:
            return False, "改指向需要一个目标页"
        stems = {p.stem for p in (root() / "wiki").rglob("*.md")}
        if new_slug not in stems:
            return False, (f"目标页不存在：wiki/*/{new_slug}.md"
                           "（不能用一条断链换另一条断链）")
    plan = {"auto": [{"target": target, "referrers": refs,
                      "action": action, "new_slug": new_slug}], "ask": []}
    written, blocked, by_page = _write_relink_drafts(plan)
    problems = _check_relink(by_page, written)
    if not written:
        why = "; ".join("%s：%s" % (b["page"], b["why"]) for b in blocked) or "没有生成草稿"
        return False, f"补链没能落草稿（{why}）—— 断链仍在，清单没清"
    if problems:
        log.error("手动补链一致性守卫未通过：%s", problems)
    data = relink_ask()
    data["ask"] = [a for a in data["ask"] if a["target"] != target]
    _write_relink_ask(data["ask"], data["blocked"], data["guard_errors"])
    log_append("relink-apply", target,
               f"{action} · {len(written)} 页草稿 · 未处理 {len(blocked)} 页")
    return True, {"target": target, "action": action, "drafts": written,
                  "blocked": blocked, "guard_errors": problems}


# ------------------------------------------------------------------ 概览
def stats() -> dict:
    """给页面 / 脚本用的概览（不含 LLM 调用）。

    ★ `root` 是**绝对路径**，只给脚本 / 日志 / 排查用；凡是进模板的，一律用
      `root_label`（相对项目根，或「自定义目录」）。`verify_wiki.py` 里有一条守卫
      钉这件事：模板出现 `stats.root` 就红。
    """
    r = root()
    pg = pages()
    return {
        "root": str(r),
        "root_label": root_label(),
        "exists": r.exists(),
        "drafts": len(list((r / ".drafts").glob("*.md"))) if (r / ".drafts").exists() else 0,
        "pages": len(pg),
        "raw": len(list((r / "raw").glob("*.md"))) if (r / "raw").exists() else 0,
        "domains": sorted({x["domain"] for x in pg if x["domain"]}),
        "low_confidence": len([x for x in pg if x["confidence"] == "low"]),
        "last": (read_log(limit=1) or [{}])[0],
    }
