"""审批态「可能相关的已入库页」：LLM **宽口径推荐** + 带指纹的缓存（2026-11-11）。

命题：编译分组的 S1 判据是「确认在回答同一个问题才可以并」（窄，并错难修），
审批的人因此看不到「库里可能已经有相关页」，一不留神就多出一页重复知识。
本模块补的是**提示**，不是决定：模型只负责列出值得看一眼的页，最后仍由人点
「改为并入」拍板，走的是 `ingest.merge_draft_into` 那条已有的替换稿 + diff 链路。

★★ 为什么不塞进 S1（用户 11-11 定的方向「推荐还是走 LLM 的好」）：
  实测 18 条候选 / 7 页的那次编译，S1 一次 `existing:` 都没给，prompt 明确写
  「拿不准就新建」。把宽口径推荐并进同一个 prompt，要么污染并入决定，要么被
  那套严判据一起带走。分开两次问，各按各的错误成本设阈值：
    并入决定：错了 = 把新判断塞进人已确认的知识里，难修 ⇒ 窄；
    相关推荐：错了 = 人看一眼否掉，漏了 = 多一页重复知识 ⇒ 宽。

★ 为什么是独立模块：`core/wiki.py` 模块头明写「只碰文件，不碰 LLM、不碰 DB」。
  本模块要读草稿明细、调模型、记账，不能塞回去；也不进 `ingest.py` 的编译链
  —— 编译刚清零重来过，推荐是**审批期**的动作，跟分组解耦。

★★ 花钱边界（本项目最贵的教训是静默花钱）：
  - 唯一花钱点是 `recommend()`；**2026-11-15 用户定：改为默认调用** —— 打开
    审批浮层时模板里的自动表单（`hx-trigger="load"`）发一次 POST，
    `apps/wiki/views.py::approval_related` 才走到这里，不再等人点按钮。
  - 但 `_approval_workarea_ctx` 有 13 个调用点（含审批对话后的 OOB 刷新），渲染
    路径**只准调 `load()`**（纯文件读），绝不许在这里起模型 ——「默认调用」是
    那个 POST 自动发，不是把模型塞进渲染。
  - 自动 POST 先查指纹缓存，fresh 直接零调用返回；结果按指纹缓存进
    `meta/related.json`。草稿或库变了（stale）按用户定的边界**也自动重查**。
  - **不开**「撞顶自动翻倍重试」：一次性动作失败/截断会在浮层说清楚；自动调用
    失败**不再自动重试**（防无限循环），要人自己再点一次。静默多打一次调用
    正是用户明确不想要的（见 11-09 拆批那次实测）。

★ 缓存为什么带指纹：指纹把「草稿内容 + 候选 claim + 已入库页清单/摘要/tags」
  全算进去。草稿被审批对话改过、库里新入库一页、某页摘要变了，fp 都会变，
  浮层改成「上次推荐已过期」而不是拿旧结论糊弄人。顺带解决一个隐患：页被删后，
  fp 一定变，缓存里那条 stale 的 slug 根本不会被显示成可点的并入按钮。
"""
import hashlib
import json
import logging

from django.conf import settings
from django.utils import timezone

from core import citeguard
from core import costreport as cr
from core import jsontool
from core import wiki as wstore
from core import wiki_agent as wa

log = logging.getLogger(__name__)

RELATED_FILE = "meta/related.json"
KIND = "wiki_related"

#: 最多列几条（2026-11-23 用户定：固定 2 条太窄，放宽到 5 条）。
#: 上限只决定「最多看几条」，不逼模型凑数：只有 1 条真相关就给 1 条。
#: ★ prompt 用同一个常量插值，旧缓存靠 `_material` 里的 limit 自动失效。
RELATED_MAX = 5

SYSTEM = """你是「谛听」知识库的编辑助理。任务**不是**决定要不要并入，而是列出「人审批这份草稿时值得看一眼」的已入库页。

【背景】编译分组用的是很严的判据（只有确认在回答同一个问题才并入，拿不准就新建），
因此会大量新建页。审批的人看不到「库里可能已经有相关页」，一不留神就多出一页重复知识。
你负责把这类可能补上，**最后仍由人拍板**。

【宽口径判据】命中任一条就列：
- 在回答同一个问题的不同侧面；
- 同一条产业链 / 同一条因果链的上下游；
- 同一条判断准则在不同对象上的应用；
- 草稿正文里明确引用了那一页（[[slug]]）；
- 那一页的结论会被这份草稿的内容修正、限定或推翻。
只因「主题相近」「都提到 AI / 模型」不算。
★ 宁可多列一条让人否掉，也不要漏掉一条让人新建重复页：列错的成本是看一眼，
  漏掉的成本是知识库里多一页。但也不要为了凑数硬列，确实没有就给空数组。

【输出】
只从 pages 清单里选 slug，**不许编造清单外的 slug**。
最多列 {MAX} 条（期望 2–{MAX} 条；确实只有 1 条或没有就照实给，不要为凑数硬列）。
每条的 why 要写清「在回答什么问题上重叠」，具体到事，不许写「主题相关」这类空话。
严格输出 JSON，不要解释、不要 markdown 围栏：
{"related":[{"slug":"...","why":"..."}]}
""".replace("{MAX}", str(RELATED_MAX))


def _path():
    return wstore.root() / RELATED_FILE


def _read_cache() -> dict:
    """缓存读失败一律当「没查过」：展示层的缓存不该让审批浮层打不开。"""
    try:
        got = json.loads(_path().read_text(encoding="utf-8") or "{}")
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(slug: str, rec: dict) -> None:
    """原子替换写。单人审批动作 + 编译互斥守卫，不做进程间加锁（读-改-写窗口内
    不会被第二个审批请求撞上；真撞上也只是丢一次推荐，不损坏数据）。"""
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = _read_cache()
    data[slug] = rec
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def _material(info: dict, pages: list) -> dict:
    """喂给模型的材料，**同时也是缓存指纹的底本**。

    ★ 不给全文 / 不给 quote：推荐问的是「在回答什么问题上重叠」，出处证据在那
      一步只是噪音（与 `_page_catalog` 同源）。给正文是因为形态 A 下正文就是
      结论本身，`[[slug]]` 引用也写在里面；给 claim 是因为标题往往比内容窄。
    ★ 指纹必须包含 pages：库里新增/删掉一页、某页摘要改了，旧推荐就该过期。
    ★ limit 也进指纹（2026-11-23）：放宽 2→5 条后旧缓存必须自动 stale 重查，
      否则浮层还会显示上次那 2 条，人以为改动没生效。模型也从这里看到同一个上限。
    """
    fm = info.get("fm") or {}
    return {
        "limit": RELATED_MAX,
        "draft": {
            "slug": info.get("slug") or "",
            "title": str(fm.get("title") or "").strip()[:80],
            "domain": str(fm.get("domain") or "").strip(),
            "tags": [str(t).strip() for t in (fm.get("tags") or [])
                     if str(t).strip()],
            "body": (info.get("body") or "").strip()[:1200],
            "claims": [str(c.get("claim") or "").strip()[:120]
                       for c in (info.get("candidate_detail") or [])
                       if str(c.get("claim") or "").strip()][:12],
        },
        "pages": [
            {"slug": p.get("slug") or "", "title": p.get("title") or "",
             "domain": p.get("domain") or "",
             "summary": (p.get("summary") or "")[:120],
             "tags": p.get("tags") or []}
            for p in pages
        ],
    }


def fingerprint(obj: dict) -> str:
    return hashlib.sha256(
        json.dumps(obj, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()[:20]


def load(slug: str, info: dict, pages=None) -> tuple:
    """`(rows, state)`，state ∈ `fresh` / `stale` / `none`。

    ★★ 纯文件读，零 LLM、零 DB —— 这是**唯一**允许被渲染上下文调用的函数。
    ★ `fresh` 才显示推荐；`stale` 说明草稿或库变了，旧结论不能拿来给人点按钮。
    """
    if pages is None:
        pages = wstore.pages()
    fp = fingerprint(_material(info, pages))
    rec = _read_cache().get(slug)
    if not isinstance(rec, dict):
        return [], "none"
    if rec.get("fp") != fp:
        return [], "stale"
    return list(rec.get("rows") or []), "fresh"


def _parse(text: str, valid: set, own: str) -> tuple:
    """`(rows, discarded, err)`。err 非空 = 整份输出不合格，绝不静默当空数组。

    ★ 硬校验不靠 prompt「不许编 slug」：模型编的、指向待审草稿的、指向自己的、
      重复的、没给理由的，一律丢掉并**带出来让人看见**（`log.warning` + 浮层）。
      静默丢掉等于下次还不知道模型在这个环节不老实。
    """
    obj = None
    for got in jsontool.iter_json(text):
        if isinstance(got, dict) and isinstance(got.get("related"), list):
            obj = got
            break
    if obj is None:
        return [], [], "模型输出不是约定的 JSON（没找到 related 数组），这次没有采信"
    out, seen, discarded = [], set(), []
    for item in obj["related"]:
        if not isinstance(item, dict):
            discarded.append("一条不是对象")
            continue
        s = str(item.get("slug") or "").strip()
        why = str(item.get("why") or "").strip()
        if s not in valid:
            discarded.append(s or "(空 slug)")
            continue
        if s == own:
            discarded.append(s + "(指向自己)")
            continue
        if s in seen:
            discarded.append(s + "(重复)")
            continue
        if not why:
            discarded.append(s + "(没给理由)")
            continue
        seen.add(s)
        out.append({"slug": s, "why": why[:160]})
        if len(out) >= RELATED_MAX:
            break
    return out, discarded, ""


def recommend(slug: str) -> tuple:
    """跑一次 LLM 推荐并落缓存。返回 `(ok, payload)`；失败时 payload 是给人看的话。

    ★ 唯一的写 + 花钱入口。调用方（视图）必须先过编译互斥守卫。
    ★ 失败 / 截断**都不写缓存**：截断既不算成功也不算失败（项目铁律），
      让它停在「还没查过」，人说不定会再点一次；写进去只会得到一个半截的结论。
    """
    if not wstore._safe_slug(slug):
        return False, "草稿名非法"
    info = wa.read("draft", slug)
    if not info.get("ok"):
        return False, info.get("err") or "读不到这份草稿"
    direction = wstore.draft_direction(info.get("fm") or {})
    if direction:
        return False, ("这份草稿已有明确去向（%s），不需要再找相关页" % direction)
    pages = wstore.pages()
    obj = _material(info, pages)
    fp = fingerprint(obj)
    if not pages:
        # 库里 0 页时答案必然是空，别花这次钱；仍落一条缓存让浮层说「查过」。
        _write_cache(slug, {"fp": fp, "rows": [], "discarded": [],
                            "at": timezone.localtime().isoformat(timespec="seconds"),
                            "no_pages": True})
        return True, {"rows": [], "discarded": [], "no_pages": True}

    stats = citeguard.fresh_stats()
    text, err = citeguard._call(
        KIND, SYSTEM, json.dumps(obj, ensure_ascii=False, indent=1), stats,
        max_tokens=int(settings.WIKI_RELATED_MAX_TOKENS), temperature=0.1,
        pool=cr.POOL_WIKI, tag="wiki 相关页推荐", label=slug,
        max_calls=1,
        # ★ 一次性动作，不静默翻倍多打一次调用（见模块头「花钱边界」）。
        retry_on_empty=False, retry_on_truncated=False)
    if stats.get("truncated"):
        log.warning("wiki 相关页推荐被截断，不写缓存：%s", slug)
        return False, "这次输出被预算截断（JSON 可能不完整），没有采信；可以再查一次"
    if err or not text:
        return False, "推荐没成功：%s" % (err or "模型没有返回内容")
    rows, discarded, why = _parse(text, {p["slug"] for p in pages}, slug)
    if why:
        log.warning("wiki 相关页推荐输出不合格 %s：%s", slug, why)
        return False, why
    if discarded:
        log.warning("wiki 相关页推荐丢掉 %d 条不合法结果（%s）：%s",
                    len(discarded), slug, "、".join(discarded))
    _write_cache(slug, {"fp": fp, "rows": rows, "discarded": discarded,
                        "at": timezone.localtime().isoformat(timespec="seconds")})
    return True, {"rows": rows, "discarded": discarded}
