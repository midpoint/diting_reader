"""知识库页（/wiki）：草稿审批（Gate 1）+ 已入库页浏览。


边界（写在最前面，因为它们决定这个 app 不做什么）：
  - **花钱动作一律走显式 POST**：审批链（阶段 0）一个 token 都不花；提炼 /
    审批对话 / 改判并入后的结论重编，各自只在人点对应按钮时发生。
    ★ 2026-11-15 用户定的例外：**相关页推荐改为默认调用** —— 打开审批浮层
      由模板里的 `hx-trigger="load"` 自动发一次 POST，不再等人点按钮。但这
      **不是**放宽到渲染路径：`_approval_workarea_ctx` 的 13 个调用点仍只读
      `wiki_related.load()` 缓存，模型只存在于 `approval_related` 这个 POST；
      缓存命中零调用、失败不自动重试、浮层写明「已自动查过」，成本仍看得见。
    ★★ 渲染路径零 LLM：相关页提示只读缓存（纯文件 IO），模型调用只在
      `wiki_related.recommend()` 里，由那个 POST 触发（自动或手动）。
  - **校验只有一份**：审批走 `core.wiki.approve()`、提炼走 `core.ingest.extract()`，
    本页绝不复制判据 —— 判据复制一次就会漂（页面放行、入库拒，或反过来）。
  - **失败必须说清原因**：缺哪个字段、目标页已存在、只有几个字我敲的，
    都原话显示出来。只 toast 一句「失败」会让人反复点同一个按钮
    （`/costs` 的池→键错配就是这么暴露的）。
  - 文件读写在 `core/wiki.py`（那边不依赖 Django 请求，能离线单测）。

★ 命名：本 app 叫 `wiki`（按 URL 分区，与 prefs / costs / logs 一致），存储层是
  `core.wiki`。所以这里一律 `from core import wiki as wstore`，
  「页面逻辑」与「文件存储」在代码里一眼分得开。
"""
import json
import logging
import queue
import threading

from django.conf import settings
from django.db import close_old_connections
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.utils.html import escape
from django.views.decorators.http import require_POST

from core import conversation
from core import ingest
from core import marks
from core import mdrender
from core import sourceblock
from core import wiki as wstore
from core import wiki_agent as wa
from core import wiki_loop
from core import wiki_related

log = logging.getLogger(__name__)

#: 最近操作显示多少条。日志是给人"刚那一刀落哪儿了"的，不需要全量。
LOG_SHOW = 15

#: 审批页标签下拉里最多放多少个候选词（全库词表按次数降序取前 N）。
#: ★ 为什么要有上限：下拉是人一眼扫的东西，200 个已经比屏幕长了；而且这一段是
#:   随每次工作区渲染（13 个调用点，含 OOB 刷新）塞进 `data-vocab` 的，
#:   全量塞进去纯属浪费。真实库 2026-11-16 是 95 个词，远没到上限。
TAG_VOCAB_LIMIT = 200


def _compile_running_guard() -> str:
    """提炼/编译一条龙进行中时，审批与编辑该被挡。返回 "" = 放行，否则一句人话。

    ★ 第十一步「双向硬锁」如今只剩这一个方向（2026-11-09 用户拍板去掉
      「审批中暂停提炼/编译」：草稿分批产生，有草稿就拒编会卡死后续流水线）。
      编译在写 `.drafts/`，此时人若通过 / 退稿 / 编辑同一批文件，
      「这一批」的边界就碎了 —— 所以运行期必须挡。
    ★ 判据用 `ingest.compile_state()["running"]`（状态文件 + 锁），不直接探锁：
      它是页面判断「在不在跑」的唯一入口，且自带 aborted 处理 —— 状态文件在、
      锁却空（上次被 kill）时 running=False，**不该挡人做事**。
    ★ 只看文件/锁事实，不引入 DB 对话记录（DB 记录永久存在，拿它当锁会锁死，
      `approval_active()` 的 docstring 已讲透这条教训）。
    ★ UI 也读这个函数的结果（`mutex_msg`）：服务端守卫与按钮禁用态共用同一份
      判据和同一句人话，不写第二套口径。
    """
    st = ingest.compile_state()
    if not st.get("running"):
        return ""
    phase = "正在提炼新痕迹" if st.get("phase") == "extract" else "正在编译成页"
    return ("%s（%s 开始，已 %s）—— 这一批落定前不能审批或编辑，"
            "跑完页面会自动刷新。" % (
                phase, st.get("started_at") or "?", st.get("elapsed_text") or "?"))


def _toast_only(msg: str) -> HttpResponse:
    """200 + 空体 + `wikiFlash`（用于 `hx-swap="none"` 的按钮）。

    ★ 不用 204：204 时 htmx 不解析 `HX-Trigger` 的 value，toast 会退化成兜底的
      「已更新」（第六步踩过）。守卫拒绝必须有可见文字，不能静默。
    """
    resp = HttpResponse("")
    resp["HX-Trigger"] = json.dumps({"wikiFlash": {"msg": msg}})
    return resp


def _schema_text() -> str:
    """页面规范原文（`wiki/schema.md`）。读不到就给空串，页面自己降级提示。"""
    try:
        return (wstore.root() / "schema.md").read_text(encoding="utf-8")
    except OSError:
        return ""


def _group_pages(pages) -> list:
    """按域聚合（`pages()` 已按路径排序 ⇒ 同域的页天然相邻，聚合即可保持稳定）。"""
    groups = []
    for p in pages:
        if groups and groups[-1]["domain"] == p["domain"]:
            groups[-1]["pages"].append(p)
        else:
            groups.append({"domain": p["domain"], "pages": [p]})
    return groups


def _ctx(request, flash: str = "", err: str = "",
         ingest_msg: str = "", ingest_ok: bool = True, ingest_key: str = "") -> dict:
    """整页与片段**共用**的上下文。

    ★ 必须共用：两套上下文一定会漂，典型症状是「批完之后上面数字变了、下面列表没变」。
    ★ 预览在服务端渲（`mdrender` 先 escape 再套标签）：草稿正文来自模型，也可能来自
      我手写，一律当不可信文本 —— 不引前端 markdown 库，就没有 XSS 面。
    ★ `ingest_*` 三个参数是「提炼之后那句话」：和写动作的 err/flash 分开报，
      混用会让「提炼失败」附着在一个无关动作上（上一版的坑）。
    """
    wstore.ensure_dirs()
    st = wstore.stats()

    # ★ 「已编译」必须拿**全量**算：`pages` 下面还要被 `?src=` 过滤。拿滤过的
    #   列表算，被滤掉那批候选会从「已编译」退回「待编译」—— 面板按钮亮着，
    #   点下去整批跳过。这正是本轮要消灭的「数字和真相各说各话」。
    all_drafts = wstore.list_drafts()
    all_pages = wstore.pages()
    done_ids = ingest.compiled_ids_from(all_drafts, all_pages)
    backlog = ingest.compile_backlog(done_ids=done_ids)

    # ★ 上次编译的结果要跨刷新活着（2026-11-03）：失败组原先只在 htmx 那一次
    #   响应里出现，切个页面回来就只剩一个亮着的按钮。落盘后这里读回来显示。
    compile_last = ingest.read_compile_result()
    # ★ 「现在有没有一份在跑」必须问服务端（2026-11-03，用户报：点完编译切到开销页
    #   再回来，按钮又能点了）。前端 `hx-disabled-elt` 只在**当前 DOM** 里有效，
    #   一切页重新渲染就没了 —— 按钮亮着 = 人会再点一次。真相在 core 的编译状态里。
    compile_run = ingest.compile_state()
    failed_ids = {i for g in (compile_last.get("failed_groups") or [])
                  for i in (g.get("cand_ids") or [])}
    todo_ids = {c.id for c in backlog["cands"]}
    # 只在「这一批待编译的正好就是上次全被拒的那批」时提示：来了一条新的，
    # 就不该再喊「重试也会被拒」（那会让人不敢点，同样是骗人）。
    retry_will_fail = bool(todo_ids) and todo_ids <= failed_ids \
        and len(todo_ids) == backlog["todo"]

    # ★ 首页草稿卡已下线（2026-11-09）：裁决唯一入口是审批浮层，它自己用
    #   `wa.read` 读草稿。这里不再预渲染草稿正文 / diff —— 那是工作台时代的遗物。

    src = (request.GET.get("src") or "").strip()
    tag = (request.GET.get("tag") or "").strip()
    pages = all_pages
    if src:
        pages = [p for p in pages if src in p["sources"]]
    # ★ 标签筛选（2026-11-16）：让 chip 从「装饰」变成「导航」。
    #   **精确匹配、不做大小写归一**：库里的词就是人手工敲进去的那几个字，
    #   模糊匹配会让「看起来不同的两个词」都能筛出同一批页 —— 人反而不知道
    #   自己在往哪个词上收敛（而收敛正是标签这件事的全部目的）。
    #   `src` 与 `tag` 可叠加，取交集：与已有 `?src=` 同一套语义，不新造规则。
    if tag:
        pages = [p for p in pages if tag in (p.get("tags") or [])]

    # ★ 先按来源聚合再排，而不是「相邻就归一组」：同一个来源可能在不同时间提炼过
    #   两次，两组候选之间夹着别人的，用「相邻判断」会把一篇的候选拆成两块。
    cand_groups, by_key = [], {}
    for c in ingest.list_candidates(status="pending", limit=None):
        k = (c.source_type, c.source_id)
        g = by_key.get(k)
        if g is None:
            g = by_key[k] = {"source_type": c.source_type, "source_id": c.source_id,
                             "title": ingest.title_of(c.source_type, c.source_id),
                             # ★ 键名不叫 items：DTL 里 `g.items` 先命名字典键，
                             #   但这个名字离 dict.items 太近，改模板的人一伸手就掉坑。
                             "cands": []}
            cand_groups.append(g)
        g["cands"].append(c)
    # ★ 来源跳转链接单一来源在 core.sourceblock.source_url，这里只取一次挂到组上
    #   （组内每条候选共用同一来源，不必每行都算一遍 URL）。
    #   `ai_url` 是带 ?ai=1 的变体：跳过去落地自动弹对话浮窗（「继续对话」）。
    for g in cand_groups:
        g["source_url"] = sourceblock.source_url(g["source_type"], g["source_id"])
        g["ai_url"] = sourceblock.source_url(g["source_type"], g["source_id"], ai=True)

    # ★ 「还有多少新痕迹」必须数**全量**（2026-11-09）：原来这里是
    #   `ingest.inventory()`（默认 limit=30）再数行数，来源一超过 30 —— 清零重来
    #   后真实库 33 个 —— 面板就少报成「30 处新痕迹」，而且**没有任何提示**说被截了。
    #   第十一步首页拆薄后收件箱表格已下线，这里不再传行、只留一个计数，
    #   `limit` 的职责（画几行）也就没用了。
    ingest_all = ingest.inventory(limit=None)

    # ★ 已处理的候选（采纳/丢弃）也要能翻出来看、能改判 —— 丢弃不是删除，
    #   是「这次先不沉淀」，人可能过两天想反悔。折叠在待审下面，不抢主流程。
    processed_groups, pby_key = [], {}
    for c in ingest.list_candidates(status="accepted", limit=None) + \
            ingest.list_candidates(status="discarded", limit=None):
        k = (c.source_type, c.source_id)
        g = pby_key.get(k)
        if g is None:
            g = pby_key[k] = {"source_type": c.source_type, "source_id": c.source_id,
                              "title": ingest.title_of(c.source_type, c.source_id),
                              "cands": []}
            processed_groups.append(g)
        g["cands"].append(c)
    for g in processed_groups:
        g["source_url"] = sourceblock.source_url(g["source_type"], g["source_id"])

    relink = wstore.relink_ask()

    return {
        "stats": st,
        "groups": _group_pages(pages),
        "pages_total": len(pages),
        "log_rows": wstore.read_log(limit=LOG_SHOW),
        "schema_html": mdrender.render(_schema_text()),
        "src": src,
        # ★ `tag` 与 `pages_all_total` 一起给：筛选后 `pages_total` 只剩筛出来的数，
        #   只报「已入库 7」会让人以为库变小了。页头要能说出「7 / 32」。
        "tag": tag,
        "pages_all_total": len(all_pages),
        "flash": flash,
        "err": err,
        # ---- 2.2 入库分诊 + 提取 ----
        "ingest_enabled": settings.WIKI_INGEST_ENABLED,
        "cand_groups": cand_groups,
        "processed_groups": processed_groups,
        "ingest_stats": ingest.stats(),
        # ★ 查询页给「已进草稿/页」的候选打标：判据与编译 backlog 同源
        #   （产物口径），不在这里另算一套。
        "compiled_ids": done_ids,
        # ★ 编译按钮该亮还是该灰，看的是**产物口径**（还有几条没变成页），
        #   不是 `status=accepted` 的条数 —— 后者只增不减，全编完了它还是原数。
        "compile_backlog": backlog,
        "compile_last": compile_last,
        "compile_run": compile_run,
        "compile_retry_will_fail": retry_will_fail,
        "ingest_msg": ingest_msg,
        "ingest_ok": ingest_ok,
        "ingest_key": ingest_key,
        # ★ 第九步：第三段「带方案问人」的断链清单。落盘不落 session，刷新不丢；
        #   模板认 `has_items` 这个显式布尔判据，不认「变量是否为空」。
        "relink_ask": relink,
        # ★ 第十一步：首页状态行与所有审批/编辑按钮的禁用说明，与服务端守卫
        #   `_compile_running_guard()` 共用同一句人话（同口径只写一处）。
        "mutex_msg": _compile_running_guard(),
        # ★ 计数走全量（`limit=None`），不走表格那 30 行：拿显示行数当计数会少报
        #   「还有多少活」，清零重来后真实库 33 个来源就暴露了这个坑（2026-11-09）。
        "new_trace_count": sum(1 for r in ingest_all
                               if r["state"] in ("new", "changed")),
        # 「待你裁决」= 待审批草稿 + 断链待决（两者都是「机器做完一部分，
        #   剩下要人拍板」的同一类裁决），不含新痕迹（那是软提示）。
        "pending_decision": st["drafts"] + len(relink["ask"]),
    }


def index(request):
    """知识库总览：状态行 + 已入库页表（主体）+ 断链待处理（裁决在审批浮层）。"""
    return render(request, "wiki/index.html", _ctx(request))


def candidates(request):
    """只读候选查询页（第十一步）：翻看全部候选，不做审批、不触发提炼/编译。

    ★ 复用 `_ctx(request)` 而不是另写一份分组逻辑：候选按来源分组的口径只有
      一处，改一处两边都跟着变（同口径两处必漏一处）。
    ★ 2026-11-09 起**纯查询**：丢弃/改判按钮也去掉了 —— 提炼后马上编译，
      已进草稿/页的候选丢弃必被 set_status 守卫禁用，按钮恒灰 = 没有按钮。
      裁决在审批浮层；历史丢弃项改判回待审在首页回看区。
    """
    ctx = _ctx(request)
    ctx["page_msg"] = (request.GET.get("msg") or "").strip()
    ctx["page_bad"] = request.GET.get("bad") == "1"
    return render(request, "wiki/candidates.html", ctx)


def tags(request):
    """标签总览：全库词表按覆盖页数降序，**只用过 1 次的词单独成组**。

    ★ 为什么需要这一页（2026-11-16）：人把 32 页逐页过了一遍之后，词表还剩
      68 个、其中 56 个只用过 1 次。没有这一页，「还有哪些词该合」只能靠再翻
      32 次 —— 翻不到全貌，收敛就停在这里。分两组的判据是程序给的（`n > 1`），
      不是靠眼睛感觉：会用第二次的词是「已经成立的概念」，只用过一次的词是
      「还没被第二篇文章认领的说法」，两者要处理的方式完全不同。
    ★ **不做就地归并**：合并要一次改多页、还得过 `_validate_draft`，那是另一条
      写通路（与审批页单页改标签不同量级）。本页只回答「现在长什么样」，
      写动作仍走唯一的审批通路 —— 少一个写口就少一条能绕过的路。
    ★ 不复用 `_ctx()`：那边要跑 inventory / 候选分组 / 断链清单，全是为首页待办
      服务的；总览页只要词表和页标题。`/wiki` 首屏已 5.4 秒，不该再背一份。
    """
    pages = wstore.pages()
    rows = wstore.tag_counts(pages)
    # 每个词下面挂哪些页 —— 判断「该不该合」靠的就是这个（「AI风险」2 页、
    # 「安全」2 页，是同一批页还是两批？看一眼就知道）。
    by_tag = {}
    for p in pages:
        for t in (p.get("tags") or []):
            t = str(t).strip()
            if t:
                by_tag.setdefault(t, []).append(p)
    for r in rows:
        r["pages"] = by_tag.get(r["tag"], [])
    multi = [r for r in rows if r["n"] > 1]
    single = [r for r in rows if r["n"] == 1]
    inst = sum(r["n"] for r in rows)
    return render(request, "wiki/tags.html", {
        "rows": rows, "multi": multi, "single": single,
        "vocab": len(rows), "inst": inst,
        "pages_all_total": len(pages),
        # 复用率 = 1 - 词数/实例数。0 实例时不能除：空库要显示「—」而不是崩。
        "reuse_pct": round(100.0 * (1 - len(rows) / inst)) if inst else None,
        "top_n": multi[0]["n"] if multi else 0,
    })


def page(request):
    """单页详情：正文 + frontmatter + 「同一来源还影响哪些页」。"""
    rel = (request.GET.get("p") or "").strip()
    pg = wstore.read_page(rel) if rel else None
    # ★ 第十一步：详情页的「拉入审批编辑 / 提交删除请求」也是编辑动作，
    #   编译一条龙进行中要可见地灰掉（与服务端守卫同一句人话）。
    ctx = {"pg": pg, "asked": rel, "mutex_msg": _compile_running_guard()}
    if pg:
        # 反向映射在人眼里的样子：这一页引用的每个来源，另外还牵着哪些页。
        # 来源变了（文章更新 / 对话续上）→ 从任一端都能找到要复查的页。
        # ★ 在视图里拼好结构再交给模板：模板里做 dict 反查（for key, v in shared.items
        #   再 if key == s）写出来又长又容易错，而且会把逻辑藏进模板。
        src_rows = [{"id": s,
                     "others": [x for x in wstore.affected_pages(s) if x != pg["page"]]}
                    for s in pg["sources"]]
        # ★★ 第十三步（形态 A）：正文 = 纯 polish 一段（导读，措辞可轻微瑕疵）；
        #   精确出处由折叠溯源区从 `IngestCandidate` 按 `quote_from` 分组读，
        #   **必须精确**。分组唯一真值是 frontmatter `candidates`。
        # ★ 老页（清零前的 32 页三块 / 旧双层）统一走 `is_legacy_body()`：
        #   「正文有结构标题 = 老格式」。真实库 31/32 页是 `## 我的判断` 三块，
        #   一个 `## 候选清单` / `## 通顺话` 都没有，只认那两个字形会把唯一主路径
        #   静默标成「结论」。整段直显并给**可见说明**，不静默降级（第十二步的教训）。
        # ★ 正文为空 = 结论还没重编成功：明确说出来，不藏进 details、不白屏。
        body = pg["body"] or ""
        legacy = wstore.is_legacy_body(body)
        cand_ids = wstore._cand_ids(pg["fm"])
        # ★ 内链可点（2026-11-11）：`[[slug]]` 原来只是字面文本，点不动。
        #   解析器一次建表，新页 / 老页回退两支共用一个；没解析上的目标留成
        #   带说明的 span，绝不做成 404 死链，并由面板给一句可见文字说明。
        lk = wstore.wikilink_resolver()
        if legacy:
            body_layer, _ = wstore.split_two_layer(body)
            body_html = mdrender.render(body_layer or body, wikilinks=lk)
        else:
            body_html = mdrender.render(body, wikilinks=lk)
        ctx.update({
            "body_html": body_html,
            "link_missing": lk.missing,
            "body_blank": not body.strip(),
            "legacy_page": legacy,
            "trace": wa.trace_rows(cand_ids),
            # 反链索引只给引用页路径；引用句在这里读时现扫，索引格式一律不动。
            "backlink_rows": wstore.backlink_contexts(pg["slug"]),
            "src_rows": src_rows,
            "fm_rows": [(k, v) for k, v in pg["fm"].items()],
        })
    return render(request, "wiki/page.html", ctx, status=200 if pg else 404)


def _fragment(request, **kw) -> HttpResponse:
    """返回 `#wiki-body` 的替换片段（htmx `outerHTML`）。

    ★ 用 `HX-Trigger` 发 toast，而不是在片段里加一条「已入库」横幅：横幅会把下面
      整个列表顶下去，连批三份时页面会跳三次。toast 不占位。
    ★ `json.dumps` 必须保持 `ensure_ascii=True`（默认）：HTTP 头只能是 ASCII，
      中文写进头里会直接抛 UnicodeEncodeError。转义后的 \\uXXXX 前端 JSON.parse
      认得，不影响显示。
    """
    open_approval = kw.pop("open_approval", False)
    html = render_to_string("wiki/_body.html", _ctx(request, **kw), request=request)
    resp = HttpResponse(html)
    triggers = {}
    if kw.get("flash"):
        triggers["wikiFlash"] = {"msg": kw["flash"]}
    # ★ 编译确实产出了草稿（`compile_run` 里 `drafts` 非空）才发 `wikiApprovalOpen`，
    #   让前端自动弹审批浮层。判据由调用方显式传，不用 `compile_last.pages` 猜 ——
    #   `approve` / `discard` 也走 `_fragment`，此时 `compile_last.pages` 是上次编译的
    #   残留，非空却与本动作无关，会误弹。
    if open_approval:
        triggers["wikiApprovalOpen"] = {}
    if triggers:
        resp["HX-Trigger"] = json.dumps(triggers)
    return resp


# ---------------------------------------------------------------------------
# 只读审批浮层（第五步 5a）。纯展示、零花钱：只看 `.drafts/` 的分组，不写不改。
# 数据只来自 `wa.read`；浮层全程不调 LLM（不碰 wiki_loop / citeguard）。
# 通过 / 退稿等写动作仍走下面的 `approve` / `discard`，与本浮层无关。
# ---------------------------------------------------------------------------

def _approval_workarea_ctx(slug: str) -> dict:
    """单页双栏（② 主工作区）的渲染上下文。slug 为 None / 空 = 空态。"""
    # ★ 三个 return 分支都要带 mutex_msg：模板判 `{% if mutex_msg %}`，
    #   漏一个分支 → 缺上下文变量被 Django 当成空值 → 静默走进「不禁用」。
    mutex = _compile_running_guard()
    # ★ 空态也要给标签键：模板 `_approval_tags.html` 里读 `tags` / `tag_vocab_json`，
    #   缺上下文变量会被 Django 当空值静默渲染（`data-vocab=""` 会让前端 JSON.parse
    #   抛错）——「三个 return 分支都要带」是同一条教训（见 mutex_msg 的注释）。
    empty_tags = {"tags": [], "tag_vocab_json": "[]", "tag_err": "",
                  "tag_max_n": wa.TAG_MAX_N}
    if not slug:
        return {"ok": False, "empty": True, "err": "没有可预览的草稿",
                "mutex_msg": mutex, "related": [], "related_state": "none",
                "related_err": "", "related_note": "",
                "related_auto": False,
                "ordinary": False, "merge_targets": [],
                "merge_err": "", **empty_tags}
    info = wa.read("draft", slug)
    if not info.get("ok"):
        return {"ok": False, "empty": True, "slug": slug,
                "err": info.get("err", "读不到这份草稿"),
                "mutex_msg": mutex, "related": [], "related_state": "none",
                "related_err": "", "related_note": "",
                "related_auto": False,
                "ordinary": False, "merge_targets": [],
                "merge_err": "", **empty_tags}
    # ★ 草稿编号 D<draft_id>（稳定自增 id）只在左侧列表显示（_approval_list.html），
    #   工作区标题行 2026-11-11 起可点击直改标题，不再重复 Dn。
    body = info["body"] or ""
    detail = list(info.get("candidate_detail") or [])
    # ★ 形态 A：右栏只显示正文（纯 polish 结论）；左栏候选卡本就是结构化明细，
    #   按 `quote_from` 补一个分组标签，让人一眼分清「谁说的」。
    groups = dict(wa.TRACE_GROUPS)
    for c in detail:
        c["tip"] = ("理解：%s\n原话：%s\n时间：%s\n出处：%s" % (
            c.get("claim") or "（无）",
            c.get("quote") or "（无）",
            c.get("published_at") or "发布时间未知",
            c.get("source") or "出处未知"))
        c["group_label"] = groups.get(c.get("quote_from") or "", "未标注来源")
        # ★ 来源可点击（2026-11-09）：解析 "source_type:source_id" 格式，生成跳转链接。
        #   方便审批时查看原文上下文。
        # ★ 细化到划线/评论级别（2026-11-09）：传入 mark_id/msg_id，链接直接定位到具体记录。
        source_str = c.get("source") or ""
        if ":" in source_str:
            source_type, source_id = source_str.split(":", 1)
            c["source_url"] = sourceblock.source_url(
                source_type, source_id,
                mark_id=c.get("mark_id"),
                msg_id=c.get("msg_id"))
        else:
            c["source_url"] = None
    # ★ 并入草稿必须给人看 diff（阶段 2.3d，2026-11-09 从首页草稿卡搬来）：
    #   审批通过 = **覆盖一页已入库的知识**。只给新正文，等于让人凭记忆比 ——
    #   而人记不住上周那页怎么写的。旧版会备份到 .drafts/.merged/，这里只需
    #   让人看清「动了什么」。目标页读不到时显式标出来（提交会被拒）。
    # ★ 并入 / 替换都要读「被覆盖的那一页」：diff 让人看清动了什么（2.3d）；
    #   候选还要区分「本次新增」vs「历史已在页里」—— 并入一个已经积累了很多
    #   候选的页时，审批者面对一整页不分新旧，没法聚焦本批真正新来的判断。
    merge_page, diff, target_page = None, [], None
    target_slug = info.get("merges") or info.get("edits") or ""
    if target_slug:
        target, _why = wstore.find_page(target_slug)
        if target is not None:
            rel = target.relative_to(wstore.root()).as_posix()
            target_page = wstore.read_page(rel)
            if target_page and info.get("merges"):
                merge_page = target_page
                diff = wstore.body_diff(target_page["body"], body)
    # ★ 候选新旧拆分：以「被覆盖页当前 candidates」为历史真值，草稿里不在此列的
    #   即本次新增。复用 frontmatter 的 candidates（页面即分组的唯一真值），不引入
    #   第二套口径（与第十步守恒守卫同源）。
    new_cand_ids = set()
    merge_new_n = 0
    merge_hist_n = 0
    if target_page is not None:
        old_ids = set(wstore._cand_ids(target_page.get("fm") or {}))
        draft_ids = list(info.get("candidates") or [])
        new_cand_ids = set(draft_ids) - old_ids
        merge_new_n = len(new_cand_ids)
        merge_hist_n = len(set(draft_ids) & old_ids)
    for c in detail:
        c["is_new"] = c["cand_id"] in new_cand_ids
    # ★ 可能相关的已入库页（2026-11-11）：模型推荐 + 带指纹的缓存，结果在
    #   `meta/related.json`。这里**只读缓存**（纯文件 IO、零 LLM、零 DB）——
    #   本函数有 13 个调用点，含审批对话后的 OOB 刷新，任何一次渲染起模型
    #   都会变成「刷一次花一次钱」。唯一花钱点在 `approval_related` 那个 POST。
    # ★ 只有普通新页稿问这一句；准入判据收在 `wstore.draft_direction` 一处
    #   （审批浮层 / 改判并入 / LLM 推荐三处共用），别各写一份。
    ordinary = not wstore.draft_direction(info["fm"])
    # ★ 一次 `pages()` 同时服务三件事：相关页缓存的指纹、标签下拉的词表、
    #   推荐行的标题/域补全。本函数有 13 个调用点（含审批对话后的 OOB 刷新），
    #   多扫一遍库就是每次刷新都多 40–50 ms（2026-11-16 实测 `pages()` 约 48 ms）。
    pages_all = wstore.pages()
    related, related_state = [], "none"
    if ordinary:
        related, related_state = wiki_related.load(slug, info, pages_all)
        # ★ 缓存只存模型输出（slug + why），展示用的标题/domain 读时补：
        #   fp 命中就说明库没变，补出来的一定是这一页现在的名字。
        by_slug = {p["slug"]: p for p in pages_all}
        for r in related:
            p = by_slug.get(r["slug"]) or {}
            r["title"] = p.get("title") or r["slug"]
            r["domain"] = p.get("domain") or ""
    # ★ 手动指定并入目标（2026-11-16 用户实测 D87 撞上「模型推荐为空」）：
    #   推荐只列模型**想得起来**的页，想不起来就没有按钮 —— 于是人明明知道该并进
    #   哪页，也只能干看着一份草稿并不进去。「推荐 ≠ 决定」反过来同样成立：
    #   **没人推荐 ≠ 不许人决定**。目标候选直接复用上面那一次 `pages_all`
    #   （零额外 IO、零模型），仍只给普通新页稿：其它去向明确的稿由
    #   `merge_draft_into` 的 `draft_direction` 闸挡着，不给入口才是不骗人。
    merge_targets = []
    if ordinary:
        merge_targets = [{"slug": p["slug"],
                          "title": p.get("title") or p["slug"],
                          "domain": p.get("domain") or ""} for p in pages_all]
        merge_targets.sort(key=lambda t: (t["domain"], t["title"]))
    return {
        "related": related,
        "related_state": related_state,
        "related_err": "",
        "related_note": "",
        # ★ 只有 approval_related 这个 POST 的响应会把它翻成 True：那种片段里
        #   不许再带自动触发表单，否则「失败 → 重渲染 → 又自动发 → 又失败」
        #   会无限循环。三个 return 分支都必须显式给 False（模板缺上下文变量
        #   会被 Django 当空值，静默走进「继续自动点火」）。
        "related_auto": False,
        "ordinary": ordinary,
        # ★ 三个 return 分支都带 `merge_targets`：模板里是 `{% if merge_targets %}`，
        #   另给 `ordinary` 兜一层 —— 缺变量被当空值时不至于渲染出半个空表单。
        "merge_targets": merge_targets,
        "merge_err": "",
        "ok": True,
        "empty": False,
        "slug": slug,
        "title": info["fm"].get("title") or slug,
        "candidates": info.get("candidates") or [],
        # ★ 标签编辑器（2026-11-16 用户提「审核页面应该有编辑 tags 的地方」）：
        #   当前草稿的标签、全库词表（输入时提示「库里已有的」）、以及上一次
        #   拒收的原因（初始渲染恒为空）。
        #   ★ 词表用 `json.dumps` 塞进 `data-vocab`：Django 的自动转义会把引号
        #     变成 &quot;，浏览器读 dataset 时还原，JS 侧 `JSON.parse` 直接能用。
        "tags": [str(t).strip() for t in (info["fm"].get("tags") or [])
                 if str(t).strip()],
        "tag_vocab_json": json.dumps(
            wstore.tag_counts(pages_all, limit=TAG_VOCAB_LIMIT),
            ensure_ascii=False),
        "tag_err": "",
        "tag_max_n": wa.TAG_MAX_N,
        "candidate_detail": detail,
        "unknown_candidates": info.get("unknown_candidates") or [],
        "missing": info.get("missing") or [],
        "body_html": mdrender.render(body, wikilinks=wstore.wikilink_resolver()),
        "body_blank": not body.strip(),
        "body_stale": bool(info.get("body_stale")),
        # ★ 第六步：替换稿（edits）要在双栏也标出来 —— 左边候选卡与右边正文都来自
        #   草稿，但人得知道「这次提交会覆盖哪一页」，否则容易以为自己在造新页。
        "edits": info.get("edits") or "",
        # ★ 第八步：`edits` 的空形态 = 删页。浮层要把「覆盖」和「删除」说清 ——
        #   两者都不可逆，但删除连页都没了，提示语必须不同。
        "delete": bool(info.get("delete")),
        "merges": info.get("merges") or "",
        # ★ 并入 / 替换草稿：把「本次新增」与「历史已在页里」的候选数交给模板，
        #   审批者一眼知道这批真正新来几条（否则并入一个候选很多的页时一堆不分新旧）。
        "merge_new_n": merge_new_n,
        "merge_hist_n": merge_hist_n,
        "merge_page": merge_page,
        "diff": diff,
        "n_del": sum(1 for x in diff if x["kind"] == "del"),
        "n_ins": sum(1 for x in diff if x["kind"] == "ins"),
        # ★ 第十步：删页提案的逐条候选取向只在**页还在**时可读（commit 之后
        #   权威清单就丢了，第九步那个「先拆走再删」正因不可达才变成假按钮）。
        #   非删页稿给一份显式 `ok=False`，模板判 `retire.ok` 而不是「变量是否
        #   为空」—— 缺上下文变量会被 Django 当成空值，静默走进空兜底。
        "retire": (wa.retire_proposal(slug) if info.get("delete")
                   else {"ok": False, "has_items": False, "guard_rings": False,
                         "conservation": "", "err": ""}),
        "retire_err": "",
        # ★ 第十一步：编译一条龙进行中，工作区里的逐条去向按钮也要灰 + 说明。
        "mutex_msg": mutex,
    }


def _approval_shell_ctx(request) -> dict:
    """浮层壳上下文：① draft 列表 + ② 默认选中第一页 + 已审批列表。"""
    info = wa.read("drafts")
    drafts = list(info.get("drafts") or [])
    selected = drafts[0]["slug"] if drafts else None
    ctx = {
        "count": info.get("count", 0),
        "drafts": drafts,
        "selected_slug": selected,
        "approved": wstore.read_approved(),
        # ★ 浮层壳的「提交入库」按钮也读同一个 mutex_msg（与服务端守卫同口径）
        "mutex_msg": _compile_running_guard(),
    }
    ctx.update(_approval_workarea_ctx(selected))
    return ctx


def approval(request):
    """GET：审批浮层（壳 + ① 列表 + ② 默认选中第一页 + ③ 对话 panel）。"""
    ctx = _approval_shell_ctx(request)
    chat = _approval_chat_ctx(selected_slug=ctx["selected_slug"])
    # ★ 只有 `err` 撞名：shell 的 err 是「读不到草稿」，chat 的 err 是「对话失败
    #   原因」。直接 update 会让空态工作区的提示被对话区的空串盖掉（两套 ctx
    #   合一处必漂）。这里把 chat 的 err 另存 chat_err，其余键合并进同一 ctx，
    #   _approval.html 用 include with err=chat_err 覆盖给 ③。
    ctx.update({k: v for k, v in chat.items() if k != "err"})
    ctx["chat_err"] = chat["err"]
    return render(request, "wiki/_approval.html", ctx)


def approval_draft(request):
    """GET ?slug=xx：单页双栏片段（② 主工作区，htmx 局部替换 #approval-workarea）。
    
    ★ 同时返回①列表的 oob 更新（2026-11-09 修复选中状态）：用户点击草稿时，
      列表的高亮状态要跟着变，否则顶部草稿始终高亮，人分不清当前选中哪份。
    ★ 顺序：先返回工作区内容（主响应，htmx 替换 #approval-workarea），
      再返回列表的 oob 更新（htmx 按 id 替换 #approval-list）。
    """
    slug = (request.GET.get("slug") or "").strip()
    shell = _approval_shell_ctx(request)
    # 找到当前选中的草稿（如果存在）
    selected = slug if any(d["slug"] == slug for d in shell["drafts"]) else shell["selected_slug"]
    # 先返回工作区内容（主响应）
    html = render_to_string("wiki/_approval_workarea.html", _approval_workarea_ctx(slug), request=request)
    # 再返回列表的 oob 更新（★ 必须传 approved，否则圆圈状态会丢）
    html += render_to_string(
        "wiki/_approval_list.html",
        {"drafts": shell["drafts"], "count": shell["count"],
         "selected_slug": selected, "approved": shell["approved"], "oob": True},
        request=request)
    return HttpResponse(html)


# ---------------------------------------------------------------------------
# 审批浮层标题直改（2026-11-11）：标题是高频改动，点一下标题行就地编辑，
# 不必再走审批对话。改名链路与对话完全一致（wa.set("title")：slug 跟随标题、
# 其他草稿引用同步、审批标记迁移），所以没有第二套写路径。
# ---------------------------------------------------------------------------

def approval_title(request):
    """GET ?slug=x[&cancel=1]：标题行两种形态的片段（根节点 .approval-title）。

    默认返回编辑态（输入框 + 保存/取消）；cancel=1 返回展示态（纯取消用）。
    ★ GET 只读：只渲染片段，不写 .drafts/。
    """
    slug = (request.GET.get("slug") or "").strip()
    ctx = _approval_workarea_ctx(slug)
    if not ctx.get("ok"):
        # 草稿没了（并发退稿 / 入库）：不回片段、不换节点，旧标题原地留着
        return HttpResponse("", status=404)
    ctx["edit"] = request.GET.get("cancel") != "1"
    ctx["title_value"] = ctx["title"]
    return render(request, "wiki/_approval_title.html", ctx)


@require_POST
def approval_title_save(request):
    """POST：应用标题修改 → 回整个工作区 + 左侧列表 oob。

    ★ 改名会换 slug：选中必须跟着新 slug 走（列表 oob 的 selected_slug），
      否则下一次对话/下一次直改又落到默认第一份（与 _track_slug_change 同病）。
    ★ 失败回工作区片段并挂 title_err（编辑态保持、输入不丢），模式同 discard_err。
    """
    slug = (request.POST.get("slug") or "").strip()
    title = (request.POST.get("title") or "").strip()

    def fail(text):
        log.warning("wiki 审批标题直改被拒：%s（%s）", slug, text)
        ctx = _approval_workarea_ctx(slug)
        ctx["edit"] = True
        ctx["title_value"] = title
        ctx["title_err"] = text
        return HttpResponse(render_to_string(
            "wiki/_approval_workarea.html", ctx, request=request))

    if msg := _compile_running_guard():
        return fail(msg)
    if not slug:
        return fail("缺少 slug")
    if not title:
        return fail("标题不能为空")
    ok, payload = wa.set("title", {"slug": slug, "title": title[:60]})
    if not ok:
        return fail(str(payload))
    new_slug = payload.get("slug") or slug
    log.info("wiki 审批标题直改：%s → %s（%s）", slug, title[:60], new_slug)
    shell = _approval_shell_ctx(request)
    html = render_to_string("wiki/_approval_workarea.html",
                            _approval_workarea_ctx(new_slug), request=request)
    html += render_to_string(
        "wiki/_approval_list.html",
        {"drafts": shell["drafts"], "count": shell["count"],
         "selected_slug": new_slug, "approved": shell["approved"], "oob": True},
        request=request)
    resp = HttpResponse(html)
    resp["HX-Trigger"] = json.dumps(
        {"wikiFlash": {"msg": payload.get("note") or "标题已更新"}})
    return resp


@require_POST
def approval_tags(request):
    """POST：改当前草稿的标签，返回 chip 片段（前端换掉 `#tag-chips`）。

    2026-11-16 用户要求：「审核页面应该有编辑 tags 的地方，参考微信好友 tag ——
    输入 tag 的时候要能选择已有的 tag」。有了就地改标签，存量 wiki 才能一页页过一遍。

    ★ 返回**片段**而不是 JSON：chip 的 HTML 只有一份（`_approval_tags_chips.html`），
      初始渲染与这里共用。返回 JSON 就得在前端再拼一遍同样的 DOM，样式/数据属性
      迟早分叉，症状是「刚打开能删、改一次就点不动」。
    ★ 判据全在 `wiki_agent.set("tags")` → `clean_tags`（最多 8 个 / 单个 20 字 /
      不许带逗号）。本视图不复制这些判据：复制一份就会漂（页面放行、写盘拒绝）。
    ★ 拒收时回的是**这一批提交的标签** + `tag_err`，不是磁盘上的旧值 ——
      人刚敲了第 9 个，只回 8 个会让他不知道自己加的是哪个；页面上的「未保存」
      就是这次的诚实信号（下一次成功保存时它自然消失）。
    ★ 草稿读不到（并发退稿/入库）回 404：前端保持原样并报错，绝不回一个空 chip 集
      —— 那看起来像「标签被清空了」。
    """
    slug = (request.POST.get("slug") or "").strip()
    want = request.POST.getlist("tags")
    if not slug:
        return HttpResponse("缺少 slug", status=400)
    info = wa.read("draft", slug)
    if not info.get("ok"):
        return HttpResponse(info.get("err") or "读不到这份草稿", status=404)

    def frag(ctx):
        return HttpResponse(render_to_string(
            "wiki/_approval_tags_chips.html", ctx, request=request))

    ctx = {"slug": slug, "tags": want, "tag_err": ""}
    if msg := _compile_running_guard():
        # ★ 编译在写 .drafts/，此时改标签会和「这一批」的边界打架：拒，但把原因
        #   摆在 chips 后面（静默不动会让人以为点坏了）。
        ctx["tag_err"] = msg
        return frag(ctx)
    ok, payload = wa.set("tags", {"slug": slug, "tags": want})
    if not ok:
        log.warning("wiki 标签改不动：%s（%s）", slug, str(payload))
        ctx["tag_err"] = str(payload)
        return frag(ctx)
    ctx["tags"] = [str(t).strip() for t in (payload.get("value") or [])
                   if str(t).strip()]
    log.info("wiki 标签：%s → %s", slug, "、".join(ctx["tags"]) or "（空）")
    return frag(ctx)


@require_POST
def approval_merge_into(request):
    """POST：把当前**新页草稿**改判为「并入某已入库页」。

    ★ 只在人点按钮时发生，且会重编一次结论（花钱）。判据全在
      `ingest.merge_draft_into` 一处，视图不复制「目标是不是已入库页」这类判断。
    ★ 失败回工作区片段并挂 `merge_err`（可见），不 toast 一句「失败」了事。
    ★ 同所有写视图：先过 `_compile_running_guard()`（静态守卫扫全集）。
    """
    slug = (request.POST.get("slug") or "").strip()
    target = (request.POST.get("target") or "").strip()

    def fail(text):
        log.warning("wiki 审批改判并入被拒：%s → %s（%s）", slug, target, text)
        ctx = _approval_workarea_ctx(slug)
        ctx["merge_err"] = "改判未生效：" + text
        return HttpResponse(render_to_string(
            "wiki/_approval_workarea.html", ctx, request=request))

    if msg := _compile_running_guard():
        return fail(msg)
    if not slug or not target:
        return fail("缺少草稿名或目标页")
    ok, payload = ingest.merge_draft_into(slug, target)
    if not ok:
        return fail(str(payload))
    # ★ 第一次点 → 新建 `.drafts/<target>.md`（新身份），源稿归档；第二次点 →
    #   在已有并入稿里追加（源稿同样归档）。两种情况**结果草稿都不一定是入参
    #   slug**（2026-11-13），列表/工作区/选中必须跟着 payload 的 draft 走，
    #   否则人看到的是空工作区或刚被归档的旧稿。
    selected = str(payload.get("draft") or "").strip() or slug
    # ★ 列表 OOB 也要刷：一份草稿入库变并入稿、另一份被归档，左栏都要跟上。
    shell = _approval_shell_ctx(request)
    ctx = _approval_workarea_ctx(selected)
    if not payload.get("polished"):
        # ★ 写盘成功、polish 失败也要说出口：草稿带 stale，提交会被拒，但人
        #   若只看到「已改判」会以为可以直接提交。
        ctx["merge_err"] = ("已改为并入，但结论重编失败：%s —— 草稿仍带待重编标记，"
                            "先在审批对话里重编结论再提交。"
                            % payload.get("polish_err"))
    html = render_to_string("wiki/_approval_workarea.html", ctx, request=request)
    html += render_to_string(
        "wiki/_approval_list.html",
        {"drafts": shell["drafts"], "count": shell["count"],
         "selected_slug": selected, "approved": shell["approved"], "oob": True},
        request=request)
    # ★ 源稿被归档、并入稿可能是新的一份：头部计数与 ③ 对话区的隐藏
    #   selected_slug 都要跟着刷（与退稿 / 对话发送同一 OOB 形状），否则人接着
    #   发话会作用到已归档的草稿上。
    html += render_to_string(
        "wiki/_approval_head.html",
        {"count": shell["count"], "mutex_msg": shell["mutex_msg"], "oob": True},
        request=request)
    chat = _approval_chat_ctx(selected_slug=selected)
    chat["oob"] = True
    html += render_to_string("wiki/_approval_chat.html", chat, request=request)
    resp = HttpResponse(html)
    flash = "已并入 %s" % target
    if payload.get("archived"):
        flash += "（源稿已归档）"
    resp["HX-Trigger"] = json.dumps({"wikiFlash": {"msg": flash}})
    return resp


@require_POST
def approval_repolish(request):
    """POST：按当前 candidates 重编这份草稿的结论（审批浮层「重编结论」按钮）。

    ★ 唯一花钱入口：显式 POST 才调 `wiki_loop._polish_one`，渲染路径零 LLM
      （`_approval_workarea_ctx` 的 13 处调用点只读 frontmatter 的失败原因）。
    ★ 判据全在 core：本视图不复制「能不能重编 / 引用守不守得住」的任何一条。
      `stale` 与 `polish_error` 由 `set_polish` 在重编成功后同一笔剥掉 / 落盘。
    ★ 失败原因必须摆在工作区里（`repolish_err`），不是只 toast 一句「失败」——
      否则人不知道是死引用、数字还是预算闸，会反复点同一个按钮。
    """
    slug = (request.POST.get("slug") or "").strip()
    if msg := _compile_running_guard():
        # ★ 带上 slug：错误要留在工作区里（光发 toast 三秒就没，人会以为没点中）
        return _repolish_workarea(request, slug, msg)
    if not slug:
        return _repolish_workarea(request, "", "缺少草稿名，无法重编结论")
    ok, why = wiki_loop._polish_one(slug)
    return _repolish_workarea(
        request, slug, "" if ok else str(why),
        flash=("结论已重编" if ok else None))


def _repolish_workarea(request, slug: str, err: str, *, flash=None) -> HttpResponse:
    """重编后整块刷工作区（换正文 / 剥 stale / 显示原因）+ 列表 OOB。

    ★ 与 `approval_draft` 同形状：主响应替换 `#approval-workarea`，列表 OOB 刷
      完成状态（重编不改草稿身份，故不刷 head / chat 面板）。
    ★ `err` 放进工作区上下文（`repolish_err`）而不是只发 toast：原因要能留在
      页面上给人看、给人复制；toast 三秒就没了。
    """
    ctx = _approval_workarea_ctx(slug)
    ctx["repolish_err"] = err
    html = render_to_string("wiki/_approval_workarea.html", ctx, request=request)
    shell = _approval_shell_ctx(request)
    html += render_to_string(
        "wiki/_approval_list.html",
        {"drafts": shell["drafts"], "count": shell["count"],
         "selected_slug": slug or shell["selected_slug"],
         "approved": shell["approved"], "oob": True},
        request=request)
    resp = HttpResponse(html)
    msg = flash or ("结论重编失败：%s" % err if err else "")
    if msg:
        resp["HX-Trigger"] = json.dumps({"wikiFlash": {"msg": msg}})
    return resp


@require_POST
def approval_related(request):
    """POST：跑一次 LLM 推荐「可能相关的已入库页」，结果落带指纹的缓存。

    ★ 2026-11-15 用户定：相关页推荐改为**默认调用**。打开审批浮层时由模板里
      `hx-trigger="load"` 的隐藏表单（auto=1）自动发这个 POST，人不必点按钮。
      渲染路径零 LLM 不变：GET 与 13 个 OOB 渲染点仍只读 `wiki_related.load()`。
    ★ auto=1 先查指纹缓存，fresh 就直接回去（**零模型调用**）：OOB 刷新会反复
      把自动表单插回 DOM，不短路就成了「刷一次花一次钱」。人点「重新查一次」
      是显式 force，不短路、照旧重查。
    ★★ 防循环：本视图返回的**所有**片段都带 `related_auto=True`，模板据此不再
      渲染自动表单。失败 / 编译中不自动重试，原因摆在浮层上，人点按钮才重试。
    ★ 判据全在 `wiki_related.recommend()` 一处：目标页必须已入库、去重、最多 5 条、
      截断不采信；且不开「撞顶自动翻倍」—— 一次性动作失败就明说。
    ★ 失败 / 丢弃都回工作区片段并挂可见文字，不 toast 一句「失败」了事。
    ★ 同所有写视图：先过 `_compile_running_guard()`（静态守卫扫全集）。
    """
    slug = (request.POST.get("slug") or "").strip()
    auto = request.POST.get("auto") == "1"

    def respond(ctx):
        # ★ 防循环的闸门，绝不能省：模板靠它判断「这段就是那次自动 POST 的回包」。
        ctx["related_auto"] = True
        return HttpResponse(render_to_string(
            "wiki/_approval_workarea.html", ctx, request=request))

    def fail(text):
        log.warning("wiki 相关页推荐失败 %s：%s", slug, text)
        ctx = _approval_workarea_ctx(slug)
        ctx["related_err"] = text
        if auto:
            ctx["related_note"] = "自动查没成功，已停下不再自动重试；可点下方按钮手动重查。"
        return respond(ctx)

    if msg := _compile_running_guard():
        return fail(msg)
    if not slug:
        return fail("缺少草稿名")

    if auto:
        # ★ 缓存命中就零调用：自动表单会被 OOB 反复点火，命中态不该再花一次钱。
        info = wa.read("draft", slug)
        if info.get("ok") and wiki_related.load(slug, info)[1] == "fresh":
            ctx = _approval_workarea_ctx(slug)
            ctx["related_note"] = "相关页推荐命中指纹缓存，本次没有调用模型。"
            return respond(ctx)

    ok, payload = wiki_related.recommend(slug)
    if not ok:
        return fail(str(payload))
    ctx = _approval_workarea_ctx(slug)
    notes = []
    if auto:
        # ★ 成本看得见：写明这是自动查、花了一次，以及缓存什么时候会再省掉它。
        notes.append("打开审批时已自动查过（一次模型调用）；结果按指纹缓存，"
                     "草稿或库没变时再打开不再花钱。")
    if payload.get("discarded"):
        # ★ 模型编了清单外的 slug / 没给理由，丢掉要让人看见：否则下次还不知道
        #   模型在这个环节不老实（与提炼的 `dropped` 同口径）。
        notes.append("模型给的结果里有 %d 条没采信（%s），已丢掉；其余推荐仍可用。"
                     % (len(payload["discarded"]),
                        "、".join(str(x) for x in payload["discarded"])))
    ctx["related_note"] = "".join(notes)
    return respond(ctx)


# ---------------------------------------------------------------------------
# 审批对话（第五步 5b）。③ 对话 panel 接 wiki_loop.run（多轮 read/set），
# set 成功后用 hx-swap-oob 一次刷新 ① draft 列表 + ② 主工作区。
# ★ run 内部自己落库 user/assistant 消息，视图只取历史渲染，不重复落。
# ★ 花钱出口是 wiki_loop.run（测试桩打 core.wiki_loop.run，不是 citeguard._call）。
# ---------------------------------------------------------------------------

def _approval_chat_ctx(err: str = "", selected_slug: str = None) -> dict:
    """③ 对话 panel 的数据：审批对话历史（复用 _wc_msg_ctx 逐条渲染）。

    ★ get_or_create_approval_conversation() 会建空对话；历史为空时前端显示
      空状态，但「建了空对话」本身无害（source_id 固定 "current"）。
    ★ 消息由 wiki_loop.run 落库，视图不重复落 —— 同一句话不会在历史里出现两次。
    ★ selected_slug：当前选中的草稿，传给前端表单作为隐藏字段。
    """
    conv = wa.get_or_create_approval_conversation()
    rows = conversation.history(conv) if conv else []
    return {
        # ★ 审批对话暂时不做划线/批注：数据不查、DOM 不挂标记锚点（见 `_wc_msg_ctx`）
        "messages": [_wc_msg_ctx(m, show_marks=False) for m in rows],
        "err": err,
        "enabled": settings.WIKI_CHAT_ENABLED,
        "approval_active": wstore.approval_active(),
        "selected_slug": selected_slug,
        # ★ 第十一步：编译中连「审批对话」也不能发（它是裁决过程的一部分）
        "mutex_msg": _compile_running_guard(),
    }


#: 会改变 `.drafts/` 内容、因此必须让列表/工作区 OOB 跟刷的**写动作**。
#: ★ 2026-11-13 补 `merge_into`：它生成/更新一份并入稿、并把源稿整份归档，
#:   却不是 `set`；只认 set 会让「一句话并入已入库页」成功落盘、界面却什么都
#:   没变 ——「自动卷入审批」必须看得见。收成一处，别在调用点再写一遍。
PANEL_WRITE_TOOLS = frozenset(("set", "merge_into"))


def _set_succeeded(res: dict) -> bool:
    """★ 面板刷新判据收在这一处：run 返回 ok 且 actions 里有成功的写动作。

    纯 read 对话不刷面板（刷了多余）；预算失败 / 撞轮次（ok=False）也不刷
    —— 此时 .drafts/ 未必变了，刷了是骗人。反证：放宽成「每次都刷」，
    test_pure_read_does_not_refresh 会 RED。
    """
    return bool(res.get("ok")) and any(
        a.get("tool") in PANEL_WRITE_TOOLS and a.get("ok")
        for a in (res.get("actions") or []))


def _track_slug_change(actions: list, selected_slug: str) -> str:
    """跟踪 actions 里的 slug 改名，返回新的 selected_slug。

    ★ 如果选中的草稿改了 slug，返回新 slug；否则返回原 selected_slug。
    ★★ set 的返回存在 `act["result"]`（`_exec_tool` 落键），不是 `act["payload"]`。
      读错键 = 永远匹配不上 = 改名后选中跳回第一份（2026-11-09 实测：
      改完 D8 标题选中变 D1，第二次对话就改了 D1）。
    """
    for act in actions:
        if act.get("tool") == "set" and act.get("ok"):
            payload = act.get("result")
            if not isinstance(payload, dict):
                continue
            old_slug = payload.get("old_slug")
            new_slug = payload.get("slug")
            if old_slug == selected_slug and new_slug and new_slug != old_slug:
                return new_slug
    return selected_slug


def approval_chat(request):
    """GET：懒加载 ③ 对话片段（独立端点，供单独刷新 / 回看）。"""
    shell = _approval_shell_ctx(request)
    return render(request, "wiki/_approval_chat.html",
                  _approval_chat_ctx(selected_slug=shell["selected_slug"]))


@require_POST
def approval_chat_send(request):
    """POST：调 wiki_loop.run（唯一花钱出口）→ 重新取历史渲染 ③。

    ★ 返回整个 _approval_chat.html（htmx outerHTML 换 #approval-chat）。
    ★ err 写对话区（不是 toast）：用户回头看还知道是预算用尽还是网络失败。
    ★ run 内部已落库 user/assistant，这里不重复落。
    ★ 有成功 set 时，响应尾部拼 ① + ② 两个 hx-swap-oob 片段：htmx 原生
      out-of-band 一次请求同时刷新两个面板，无需 JS（浮层片段内联 <script>
      不执行，走顶层 HX-Trigger 监听反而更绕）。
    """
    if msg := _compile_running_guard():
        # ★ 拒绝也要回**同一块片段**（htmx 的 outerHTML 换 #approval-chat）：
        #   回 JSON / 空体会让浮层里那块对话区凭空消失，比拒绝本身更难懂。
        shell = _approval_shell_ctx(request)
        return HttpResponse(render_to_string(
            "wiki/_approval_chat.html",
            _approval_chat_ctx(err=msg, selected_slug=shell["selected_slug"]),
            request=request))
    body = (request.POST.get("body") or "").strip()
    selected_slug = (request.POST.get("selected_slug") or "").strip() or None
    # ★ 选中链路全程可查（2026-11-09）：改错草稿时先看这条日志是「前端没带上」
    #   还是「带上了但 run 没用上」，别再靠浏览器 console 猜。
    log.info("wiki 审批对话收信：selected_slug=%s q=%s", selected_slug, body[:60])
    res = wiki_loop.run(body, selected_slug=selected_slug)
    if not res.get("ok"):
        log.warning("wiki 审批对话未完成: %s", res.get("err"))
    return HttpResponse(_approval_chat_result_html(request, res, selected_slug))


def _approval_chat_result_html(request, res, selected_slug: str) -> str:
    """一轮审批对话的权威渲染：chat 面板 +（有成功 set 时）三块 OOB。

    ★ 单一来源（2026-11-19）：非流式 `approval_chat_send` 与流式
      `approval_chat_stream_send` 共用这一段「结果 → HTML」逻辑。它在非流式里
      原本内联，抽出来后两处一致，改一处不会漏另一处。
    """
    # ★ 计算新的 selected_slug（可能被 slug 改名影响）
    new_selected = _track_slug_change(res.get("actions") or [], selected_slug or "")
    shell = _approval_shell_ctx(request)
    # ★ merge_into 产出/更新了一份并入稿：选中切过去，工作区当场显示
    #   并入 diff 与「可提交」状态 —— 人发完一句话就该看见结果，不必再点列表。
    # ★ 结果草稿不一定叫 `<to>`：已有并入稿可能是旧版「原地改判」留下的源稿名
    #   （2026-11-13），所以认 payload 的 `draft`，`to` 只作旧形状兜底。
    for act in res.get("actions") or []:
        if act.get("tool") != "merge_into" or not act.get("ok"):
            continue
        payload = act.get("result")
        if isinstance(payload, dict):
            picked = str(payload.get("draft") or payload.get("to") or "").strip()
            if picked and any(d["slug"] == picked for d in shell["drafts"]):
                new_selected = picked
        break
    # ★ 选中的草稿被归档（候选全移走 / 合并进别的页）→ 选中跟随到接收页
    #   （written[0]），而不是跳回第一份（2026-11-11 实测：全移走后选中落空，
    #   人不知道刚才那页去哪了）。
    if new_selected and not any(d["slug"] == new_selected for d in shell["drafts"]):
        for act in res.get("actions") or []:
            if act.get("tool") != "set" or not act.get("ok"):
                continue
            payload = act.get("result")
            if not isinstance(payload, dict):
                continue
            archived = payload.get("archived")
            if isinstance(archived, list) and new_selected in archived:
                written = payload.get("written") or []
                if written:
                    new_selected = written[0]
                break
    # ★ 如果选中的草稿被删了，跳到第一个
    if new_selected and not any(d["slug"] == new_selected for d in shell["drafts"]):
        new_selected = shell["selected_slug"]
    # ★ 渲染 chat 面板时用 new_selected（用户实际选中的），不是 shell["selected_slug"]（默认第一个）
    html = render_to_string(
        "wiki/_approval_chat.html",
        _approval_chat_ctx(err=res.get("err") or "",
                           selected_slug=new_selected),
        request=request)
    if _set_succeeded(res):
        html += render_to_string(
            "wiki/_approval_list.html",
            {"drafts": shell["drafts"], "count": shell["count"],
             "selected_slug": new_selected, "approved": shell["approved"], "oob": True},
            request=request)
        wc = _approval_workarea_ctx(new_selected)
        wc["oob"] = True
        html += render_to_string("wiki/_approval_workarea.html", wc,
                                 request=request)
        # ★ 头部两处计数也要跟着归档/合并后的真实数量走（2026-11-11）
        html += render_to_string(
            "wiki/_approval_head.html",
            {"count": shell["count"], "mutex_msg": shell["mutex_msg"],
             "oob": True},
            request=request)
    return html


# 审批对话的流式版：与非流式 send 同一出口（wiki_loop.run，写 draft），
# 故同样在开头接 _compile_running_guard —— 编译进行中一律拒绝。
@require_POST
def approval_chat_stream_send(request):
    """流式版 `approval_chat_send`：逐字吐最终答复，done 帧带权威片段 + OOB。

    ★ 与非流式走同一套 `_compile_running_guard` 和 `_approval_chat_result_html`，
      判据/后处理不复制；只有「怎么把文字吐出去」不同。
    """
    if msg := _compile_running_guard():
        # 拒绝也走 done 帧把 err 显示在对话区（与老端点「回同一块片段」同理由）
        def denied(on_delta):
            return {"ok": False, "err": msg, "reply": "", "actions": []}
        return _sse_conversation(
            denied,
            lambda res: _approval_chat_result_html(request, res, ""))
    body = (request.POST.get("body") or "").strip()
    selected_slug = (request.POST.get("selected_slug") or "").strip() or None
    log.info("wiki 审批流式对话收信：selected_slug=%s q=%s", selected_slug, body[:60])

    def work(on_delta):
        return wiki_loop.run(body, selected_slug=selected_slug, on_delta=on_delta)

    return _sse_conversation(
        work,
        lambda res: _approval_chat_result_html(request, res, selected_slug or ""))


# mutex-ok: 只清空审批对话记录，不动 .drafts/ 或 wiki/，也不参与裁决。
@require_POST
def approval_chat_clear(request):
    """POST：清空审批对话（连消息一起删，不可恢复）。前端 hx-confirm 二次确认。"""
    wa.clear_approval_conversation()
    shell = _approval_shell_ctx(request)
    return render(request, "wiki/_approval_chat.html",
                  _approval_chat_ctx(selected_slug=shell["selected_slug"]))


 # mutex-ok: 只切换审批标记，不改 .drafts/ 内容，不参与裁决。
def approval_toggle(request):
    """GET/POST：切换草稿的审批标记状态。返回更新后的列表片段。"""
    # ★ 支持 GET（htmx hx-get）和 POST（兼容）
    slug = (request.GET.get("slug") or request.POST.get("slug") or "").strip()
    if not slug:
        return HttpResponse("缺少 slug", status=400)
    # ★ 保持当前选中的草稿，不跳回第一个
    selected_slug = (request.GET.get("selected_slug") or request.POST.get("selected_slug") or "").strip() or None
    wstore.toggle_approved(slug)
    shell = _approval_shell_ctx(request)
    # 如果传入的 selected_slug 有效，用它覆盖默认的
    if selected_slug and any(d["slug"] == selected_slug for d in shell["drafts"]):
        shell["selected_slug"] = selected_slug
    # ★ 不设 oob=True，让 htmx 用 hx-target="#approval-list" 直接替换
    html = render_to_string(
        "wiki/_approval_list.html",
        {"drafts": shell["drafts"], "count": shell["count"],
         "selected_slug": shell["selected_slug"], "approved": wstore.read_approved()},
        request=request)
    return HttpResponse(html)


@require_POST
def approval_commit(request):
    """POST：把 `.drafts/` 整批验收入库（第五步 5c 的 commit 物理动作）。

    ★ 返回 **JSON**（走 `data-async` 统一处理），不返回片段：入库成功后 `.drafts/`
      就空了 ⇒ `approval_active()` 为假 ⇒ 整个审批浮层本就不该存在。换片段只会留下
      一个空壳浮层，用户还得自己再关一次。
    ★ 失败把「第几份、为什么」原话给前端 —— 批量失败必须能直接指到要修的那份草稿，
      否则人只能逐份试（`commit()` 已做到「一个字节不动 + 点名」）。
    ★ 二次确认在前端（模板内联 `confirm`，N 是真实数量）。服务端**不**强求
      `confirm=1`：日报重跑要那一层，是因为 `force` 可能来自旧链接/别的页面；
      commit 只存在于这个浮层的一次点击，没有「被别的路径误触发」的入口。
    ★ 校验只有一份：全部在 `core.wiki.commit()`（含 `_validate_draft` 预检），
      本视图只转发结果，不复制任何判据。
    """
    if msg := _compile_running_guard():
        # ★ 形状必须是 JSON：`data-async` 前端会 JSON.parse，回片段会直接挂掉。
        return JsonResponse({"ok": False, "error": msg})
    ok, payload = wstore.commit()
    if not ok:
        log.warning("wiki 批量入库被拒：%s", payload)
        return JsonResponse({"ok": False, "error": str(payload)})
    log.info("wiki 批量入库：%s 页（新 %s / 合并 %s / 编辑 %s / 删除 %s）",
             len(payload.get("pages") or []),
             len(payload.get("committed") or []), len(payload.get("merged") or []),
             len(payload.get("edited") or []), len(payload.get("deleted") or []))
    # ★ 第九步：toast 不能只说「已入库」。commit 之后 `.drafts/` 可能因自动补链
    #   重新非空、也可能有断链等方向判断 —— 静默生成草稿 / 静默失败是最贵的两种。
    rel = payload.get("relink") or {}
    n_draft = len(rel.get("drafts") or [])
    n_ask = len(rel.get("ask") or [])
    n_bad = len(rel.get("blocked") or []) + len(rel.get("guard_errors") or [])
    if n_bad:
        flash = f"已入库 · {n_bad} 处断链未能自动处理（见页面上的断链清单）"
    elif n_draft:
        flash = f"已入库 · 自动补好 {n_draft} 页的链接，请在审批浮层确认"
    elif n_ask:
        flash = f"已入库 · {n_ask} 处断链需要你定方向（见断链清单）"
    else:
        flash = "已入库"
    # ★ 第十步：「M 条已搬去 X、N 条已废弃」必须单独说，不能并进「已入库」。
    #   notes 非空 = 有旧格式删除稿走了整页废弃回退，逐条编排没生效 ——
    #   静默回退比拒绝贵得多，所以这句话不能省。
    retire = payload.get("retire") or {}
    if retire.get("moved_n"):
        flash += (f" · {retire['moved_n']} 条候选已搬去别的页、"
                  f"{retire['retired_n']} 条已废弃")
    if retire.get("notes"):
        flash += " · " + "；".join(retire["notes"])
    return JsonResponse({"ok": True, **payload, "flash": flash})


@require_POST
def relink_action(request):
    """第三段方案里的一个动作：把那条断链转成 `edits:` 补链草稿（第九步）。

    ★ 返回 `_body.html` 片段（不是 JSON）：动作落地后草稿出现在「待审批」，面板
      要立刻能看到它、能点「打开审批浮层」。`open_approval` 让前端自动弹出浮层，
      不用人自己去找刚生成的草稿。
    ★ 失败也返回片段 + `err`，不能只发一句 toast —— 断链清单还在页面上，人得
      看见「为什么没生效」，否则会以为自己已经处理过了。
    ★ 判据全在 `wstore.apply_relink_action`（目标页存在性、同一条写盘通道、
      一致性守卫），本视图只转发，不复制任何判据。
    """
    if msg := _compile_running_guard():
        return _fragment(request, flash=msg)
    target = (request.POST.get("target") or "").strip()
    action = (request.POST.get("action") or "").strip()
    new_slug = (request.POST.get("slug") or "").strip()
    ok, payload = wstore.apply_relink_action(target, action, new_slug)
    if not ok:
        log.warning("wiki 手动补链被拒：[[%s]] %s（%s）", target, action, payload)
        return _fragment(request, err=str(payload))
    n = len(payload.get("drafts") or [])
    log.info("wiki 手动补链：[[%s]] %s → %s 份草稿", target, action, n)
    return _fragment(
        request, flash=f"已生成 {n} 份补链草稿：[[{target}]] → {action}",
        open_approval=bool(payload.get("drafts")))


@require_POST
def retire_apply(request):
    """删页提案里的一次逐条动作：「留 → 去 X / 新建页」或「撤回 → 废弃」（第十步）。

    ★ 返回的是**审批浮层的工作区片段**，不是 `#wiki-body`：动作发生在浮层里，
      人要立刻看到「这条候选从待废弃变成了已搬去 X」。同时拼一份
      `hx-swap-oob` 的草稿列表（搬迁稿凭空出现，列表数量会变）。
    ★ 失败也返回片段 + 面板内可见的 `retire_err`，不能只发一句 toast：
      守恒守卫 / 目标已有草稿 / 跨域这几种拒绝各有各的修法，话必须留在
      人眼前，否则会以为自己已经点过了。
    ★ 判据全在 `wa.apply_retire_action`（原子落盘、守恒预演、三种拒绝分开说），
      本视图只转发，不复制任何判据。
    """
    slug = (request.POST.get("slug") or "").strip()
    cand_id = (request.POST.get("cand_id") or "").strip()
    action = (request.POST.get("action") or "").strip()
    target = (request.POST.get("target") or "").strip()
    title = (request.POST.get("title") or "").strip()
    new_slug = (request.POST.get("new_slug") or "").strip()
    if msg := _compile_running_guard():
        # ★ 回的是浮层工作区片段（不是 #wiki-body）：动作发生在浮层里，
        #   面板内要留着可见的拒绝原因，不能只发一句会飘走的 toast。
        ctx = _approval_workarea_ctx(slug)
        ctx["retire_err"] = msg
        html = render_to_string("wiki/_approval_workarea.html", ctx, request=request)
        resp = HttpResponse(html)
        resp["HX-Trigger"] = json.dumps({"wikiFlash": {"msg": msg}})
        return resp
    ok, payload = wa.apply_retire_action(slug, cand_id, action, target, title, new_slug)

    ctx = _approval_workarea_ctx(slug)
    if not ok:
        log.warning("wiki 删页提案动作被拒：%s #%s %s（%s）",
                    slug, cand_id, action, payload)
        ctx["retire_err"] = str(payload)
        html = render_to_string("wiki/_approval_workarea.html", ctx, request=request)
        resp = HttpResponse(html)
        resp["HX-Trigger"] = json.dumps({"wikiFlash": {"msg": str(payload)}})
        return resp

    log.info("wiki 删页提案动作：%s", payload.get("note"))
    shell = _approval_shell_ctx(request)
    html = render_to_string("wiki/_approval_workarea.html", ctx, request=request)
    html += render_to_string(
        "wiki/_approval_list.html",
        {"drafts": shell["drafts"], "count": shell["count"],
         # ★ 高亮仍留在人正在操作的那份删页提案上。新增搬迁稿后按 slug 排序
         #   可能把第一份换掉，直接用 shell 的 selected_slug 会跳到别的稿。
         "selected_slug": slug, "oob": True},
        request=request)
    # ★ 「留 → 新建页」会多出一份草稿，头部计数要跟着变（2026-11-11）
    html += render_to_string(
        "wiki/_approval_head.html",
        {"count": shell["count"], "mutex_msg": shell["mutex_msg"], "oob": True},
        request=request)
    resp = HttpResponse(html)
    resp["HX-Trigger"] = json.dumps(
        {"wikiFlash": {"msg": payload.get("note") or "去向已更新"}})
    return resp


@require_POST
def approve(request):
    """Gate 1：通过草稿 → 移进 `wiki/<domain>/`（校验全在 core，本视图只转发）。"""
    if msg := _compile_running_guard():
        return _fragment(request, flash=msg)
    slug = (request.POST.get("slug") or "").strip()
    ok, payload = wstore.approve(slug)
    if ok:
        log.info("wiki 审批通过：%s → %s", slug, payload.get("page"))
        return _fragment(request, flash="已入库 %s" % payload.get("page"))
    log.warning("wiki 审批被拒：%s（%s）", slug, payload)
    return _fragment(request, err=str(payload))


def _discard_flash(sync: dict) -> str:
    """退稿回执：把依赖同步的结果说清楚（同步了什么 / 还要做什么）。

    ★ 只说「已退稿」会把「凭空多出一份编辑副本」变成下一次的困惑
      （2026-11-13 用户问「点击提交入库怎么最后还剩一个 draft」同款）。
    """
    msg = "已退稿（在 .discarded/，没删）"
    if not sync:
        return msg
    bits = []
    if sync.get("released_candidates"):
        bits.append("候选 %d 条一并废弃（可改判回待审）"
                    % len(sync["released_candidates"]))
    if sync.get("release_error"):
        bits.append("候选废弃失败：%s" % sync["release_error"])
    if sync.get("links"):
        bits.append(f"同步解包 {sync['links']} 处引用")
    if sync.get("pages"):
        bits.append(f"生成 {len(sync['pages'])} 份编辑副本，待提交入库")
    if sync.get("blocked"):
        bits.append(f"{len(sync['blocked'])} 处待人工处理")
    if sync.get("guard_errors"):
        bits.append(f"守卫未通过 {len(sync['guard_errors'])} 处")
    return msg + (" · " + " · ".join(bits) if bits else "")


@require_POST
def discard(request):
    """退稿 → 移进 `.drafts/.discarded/`（**不删**，留证据好回看）。

    ★ 2026-11-13 起 `from=approval` 成功也**不关浮层**：不再回整页刷新头
      （整页重载会把人正在用的浮层一起关掉，退完一份通常还要接着审下一份）。
      改回工作区片段 + 列表 / 头部计数 / ③ 对话区三个 hx-swap-oob 片段，
      一次刷全 —— 只换工作区的话，列表高亮、提交入库(N) 和对话区隐藏的
      selected_slug 都还指着已归档那份，接着发话会作用到错草稿上。
      选中落到下一份；退完最后一份则工作区显示空态，浮层仍在。
      失败回本片段挂 `discard_err`；首页 htmx 老路径（不带 from）保留。
    """
    from_approval = request.POST.get("from") == "approval"
    slug = (request.POST.get("slug") or "").strip()

    def fail(text):
        log.warning("wiki 退稿失败：%s（%s）", slug, text)
        if from_approval:
            ctx = _approval_workarea_ctx(slug)
            ctx["discard_err"] = text
            return HttpResponse(render_to_string(
                "wiki/_approval_workarea.html", ctx, request=request))
        return _fragment(request, err=text)

    if msg := _compile_running_guard():
        if from_approval:
            return fail(msg)
        return _fragment(request, flash=msg)
    # 理由走表单字段（POST body, utf-8），不走 htmx 的 hx-prompt ——
    # 后者塞进 `HX-Prompt` 头，而 HTTP 头只能 latin-1，中文理由会让请求发不出去（实测）。
    reason = (request.POST.get("reason") or "").strip()
    sync = {}
    ok, payload = wstore.discard(slug, reason, sync_report=sync)
    if ok:
        log.info("wiki 退稿：%s → %s · 依赖同步 %s", slug, payload, sync)
        flash = _discard_flash(sync)
        if from_approval:
            # ★ 四个面板一次刷全（与 approval_chat_send 的 OOB 形状一致，
            #   将来再加面板时两处都要扫）：主响应换 ②，其余走 hx-swap-oob。
            shell = _approval_shell_ctx(request)
            selected = shell["selected_slug"]
            # ★ 依赖同步生成了编辑副本时，选中直接切过去（不靠列表排序巧合）：
            #   `list_drafts()` 把「缺字段」的草稿排前面，靠默认第一份会落在别的
            #   稿上，人看不到刚生成、正等他确认的那份 diff。
            for s in (sync.get("pages") or []):
                if any(d["slug"] == s for d in shell["drafts"]):
                    selected = s
                    break
            html = render_to_string(
                "wiki/_approval_workarea.html",
                _approval_workarea_ctx(selected), request=request)
            html += render_to_string(
                "wiki/_approval_list.html",
                {"drafts": shell["drafts"], "count": shell["count"],
                 "selected_slug": selected, "approved": shell["approved"],
                 "oob": True}, request=request)
            html += render_to_string(
                "wiki/_approval_head.html",
                {"count": shell["count"], "mutex_msg": shell["mutex_msg"],
                 "oob": True}, request=request)
            chat = _approval_chat_ctx(selected_slug=selected)
            chat["oob"] = True
            html += render_to_string("wiki/_approval_chat.html", chat,
                                     request=request)
            resp = HttpResponse(html)
            resp["HX-Trigger"] = json.dumps({"wikiFlash": {"msg": flash}})
            return resp
        # 首页老路径：若同步生成了编辑副本，顺手把浮层弹出来让人看见
        return _fragment(request, flash=flash,
                         open_approval=bool(sync.get("pages")))
    return fail(str(payload))


# ---------------------------------------------------------------------------
# 草稿编辑（阶段 2.3b）。润色正文，不动 frontmatter；记账走 log.md 的 edit-draft。
# ---------------------------------------------------------------------------

# mutex-ok: 只读渲染草稿正文预览，不写任何文件或 DB。
@require_POST
def draft_preview(request):
    """实时预览：返回渲染后的正文 HTML 片段（htmx 替换预览容器）。

    ★ 只渲染正文（不含 frontmatter）：编辑器改的是「我的理解」部分，frontmatter 是机器
      管理的结构信息。预览和最终落盘一致（都走 mdrender），不会出现「预览对的、保存后错」。
    """
    body = request.POST.get("body") or ""
    # ★ 预览与详情页 / 审批态同一个渲染口径：内链也要可点，否则「预览对的、
    #   保存后错」（draft_preview 的 docstring 本来就承诺两边一致）。
    html = mdrender.render(body, wikilinks=wstore.wikilink_resolver())
    return HttpResponse(html)


@require_POST
def draft_save(request):
    """保存草稿编辑：写文件 + 记 log + 返回更新后的草稿卡片段。

    ★ 返回整个 #wiki-body 片段（和 approve/discard 一致）：编辑完草稿后，用户可能接着
      审批或继续改，保持上下文连贯比局部刷新草稿卡更重要（局部刷新会让其他草稿卡闪烁）。
    ★ 错误用 toast（flash）而不是 inline：草稿不存在或 slug 非法时，没有对应的
      草稿卡可以挂错误信息，toast 更合适。
    """
    if msg := _compile_running_guard():
        return _fragment(request, flash=msg)
    slug = (request.POST.get("slug") or "").strip()
    body = request.POST.get("body") or ""
    ok, payload = wstore.save_draft(slug, body)
    if ok:
        log.info("wiki 草稿编辑：%s（%d 字）", slug, payload.get("chars", 0))
        return _fragment(request, flash="已保存 %s（%d 字）" % (slug, payload.get("chars", 0)))
    log.warning("wiki 草稿编辑失败：%s（%s）", slug, payload)
    return _fragment(request, flash="编辑失败：%s" % payload)


# mutex-ok: 只读渲染入库页正文预览，不写任何文件或 DB。
@require_POST
def page_preview(request):
    """入库页实时预览：同 draft_preview，渲染正文 HTML 片段。"""
    body = request.POST.get("body") or ""
    # ★ 预览与详情页 / 审批态同一个渲染口径：内链也要可点，否则「预览对的、
    #   保存后错」（draft_preview 的 docstring 本来就承诺两边一致）。
    html = mdrender.render(body, wikilinks=wstore.wikilink_resolver())
    return HttpResponse(html)


@require_POST
def page_edit(request):
    """把已入库页拉进草稿区走审批编辑（第六步）。

    返回 200 + 空体 + `HX-Trigger`：成功发 `wikiApprovalOpen`（前端顶层监听里已有
    openModal("/wiki/approval") 的处理器），失败只发 toast。
    配合模板的 `hx-swap="none"`，空体不会被塞进任何容器。

    ★ 为什么不是 204：**204 时 htmx 不解析 `HX-Trigger` 的 value**（实测：toast
      退化成兜底的「已更新」，也就是说事件发了、内容丢了）。项目里 `wikiFlash`
      此前全部走「200 + HTML 片段」，从没在 204 上用过 —— 这是第一个，也是踩到的。
    ★ 为什么不是跳转/片段：按钮在**单页详情**上，它不属于 `#wiki-body`，没有可替换
      的容器；而编辑的下一步是「在审批浮层里对话」，那是个弹层不是页面。
    """
    if msg := _compile_running_guard():
        return _toast_only(msg)
    page_rel = (request.POST.get("page") or "").strip()
    slug = page_rel.rsplit("/", 1)[-1]
    if slug.endswith(".md"):
        slug = slug[:-3]
    ok, payload = wstore.pull_page(slug)
    if ok:
        log.info("wiki 拉入编辑：%s → %s", page_rel, payload.get("draft"))
    else:
        log.warning("wiki 拉入编辑失败：%s（%s）", page_rel, payload)
    triggers = {"wikiFlash": {"msg": (f"已拉入审批：{slug}" if ok else str(payload))}}
    if ok:
        triggers["wikiApprovalOpen"] = {}
    resp = HttpResponse("")
    resp["HX-Trigger"] = json.dumps(triggers)
    return resp


@require_POST
def page_delete(request):
    """把已入库页送进审批，等一句「确认删除」（第八步）。

    ★ 与 `page_edit` 同一条响应契约：200 + 空体 + `HX-Trigger`（不用 204，
      204 时 htmx 不解析 trigger 的 value，toast 会退化成兜底的「已更新」）。
    ★ 只生成**草稿**，不删任何东西：删除的生效点只有一个 —— 审批浮层里 commit。
      这是它敢复用 `edits` 这条通路的原因（`LOCKED_FM` 早把 `edits` 锁成人/程序独占）。
    """
    if msg := _compile_running_guard():
        return _toast_only(msg)
    page_rel = (request.POST.get("page") or "").strip()
    slug = page_rel.rsplit("/", 1)[-1]
    if slug.endswith(".md"):
        slug = slug[:-3]
    ok, payload = wstore.request_delete(slug)
    if ok:
        log.info("wiki 请求删除：%s → %s", page_rel, payload.get("draft"))
        msg = f"已提交删除请求：{slug}（在审批里确认后才真删）"
    else:
        log.warning("wiki 请求删除失败：%s（%s）", page_rel, payload)
        msg = str(payload)
    triggers = {"wikiFlash": {"msg": msg}}
    if ok:
        triggers["wikiApprovalOpen"] = {}
    resp = HttpResponse("")
    resp["HX-Trigger"] = json.dumps(triggers)
    return resp


@require_POST
def page_save(request):
    """保存入库页编辑：写文件 + 更新 updated + 重建 sources.json + 跳回页详情。

    ★ 与草稿编辑的响应差异：草稿编辑返回 #wiki-body 片段（因为用户在列表页），
      入库页编辑跳回详情页（因为用户在单页 view，编辑完自然要看更新后的结果）。
    ★ 不用 Django messages（中间件配置复杂）：成功时直接跳回，页面自然显示新内容；
      失败时也跳回，但带 err 参数，页面可以据此显示错误（目前先简单处理）。
    """
    from django.shortcuts import redirect
    from django.urls import reverse

    page_rel = (request.POST.get("page") or "").strip()
    body = request.POST.get("body") or ""
    if msg := _compile_running_guard():
        # ★ 这个端点没有页面容器（第六步后已无模板引用，只剩脚本/直连），
        #   不能回空体（白屏）也不能回 JSON（前端不是 data-async）。给一段
        #   带可见文字的 HTML，拒绝原因至少留在屏幕上。
        return HttpResponse('<div class="alert-bar is-bad">%s</div>' % escape(msg))
    ok, payload = wstore.save_page(page_rel, body)
    if ok:
        log.info("wiki 入库页编辑：%s（%d 字）", page_rel, payload.get("chars", 0))
        return redirect(reverse("wiki_page") + "?p=" + page_rel)
    log.warning("wiki 入库页编辑失败：%s（%s）", page_rel, payload)
    # ★ 失败也跳回，但带 err 参数（页面可以据此显示错误）
    from urllib.parse import quote
    return redirect(reverse("wiki_page") + "?p=" + page_rel + "&err=" + quote(str(payload)))


# ---------------------------------------------------------------------------
# 入库分诊 + 提取（阶段 2.2）。判据全在 `core.ingest`，本页只转发 + 把结果说人话。
# ★ 与审批的区别：审批「移文件」，提炼「花钱」。所以提炼必须由人点，
#   绝不挂到「保存划线」「发完消息」这类高频动作上（那等于每次操作都扣一次钱）。
# ---------------------------------------------------------------------------

# mutex-ok: 自己就是「提炼」这一端；并发由 core.ingest 的编译锁管，不挡自己。
@require_POST
def ingest_run(request):
    """对某个来源跑一次「分诊 + 提取」，把候选落库。

    ★★ 2026-11-16 用户拍板撤销 UI 入口：文章正文里噪声太大（一段新闻里
      能提十几条「事实」，稀释掉真正属于「我的判断」的东西），wiki 只接收
      **划线 / 批注** 这类高质量信号。所以阅读列表右键里的「发送到 wiki」
      已删除（见 `static/app.js`），这条端点保留给脚本 / 测试 / 未来入口。
    ★ 随之而来的一个好处：`force=1` 从界面不可达后，`ingest._material()` 里
      按 `WIKI_INGEST_ARTICLE_CHARS` 截正文就**不再是缺陷** —— 正文只作上下文，
      真正的信号（划线 / 我的原话）另成段落、不靠这段截断传递。
      但仍**不能**顺手把这条端点变成「无信号也能提炼」的公共入口：`force`
      必须由调用方显式给出，默认 False。
    """
    st = (request.POST.get("source_type") or "").strip()
    sid = (request.POST.get("source_id") or "").strip()
    force = request.POST.get("force") == "1"  # ★ 2026-11-02：人主动发送时跳过信号检查
    key = f"{st}:{sid}"
    res = ingest.extract(st, sid, force=force)

    if not res.get("ok"):
        log.warning("wiki 提炼失败 %s：%s", key, res.get("err"))
        return _fragment(request, ingest_msg=str(res.get("err") or "提炼失败"),
                         ingest_ok=False, ingest_key=key)

    created, dropped = res.get("created", 0), res.get("dropped") or []
    if created:
        msg = "提炼出 %d 条候选，去下面「待审候选」看" % created
    else:
        # ★ 0 条不是失败，必须这么说：写成「失败」会让人反复点，
        #   而模型认为「这些话里没有可沉淀的判断」本来就是合法结论。
        msg = "这次没有可沉淀的判断（模型返回 0 条候选 —— 这是合法结果，不是出错）"
    if dropped:
        msg += "；另有 %d 条因「引用对不上我的原话」被丢弃" % len(dropped)
    log.info("wiki 提炼 %s：%d 条候选，丢 %d 条", key, created, len(dropped))
    return _fragment(request, ingest_msg=msg, ingest_ok=True, ingest_key=key)


@require_POST
def candidate_status(request):
    """改候选状态（现在只放行「丢弃」/「改判回待审」）。

    ★ 候选免审（2026-11-05）：pending 直接进编译，逐条「采纳」不再是前置动作。
      旧链接、手改表单若还带 `status=accepted`，给一句人话并**不改库** ——
      悄悄写进去也没用，但人会以为自己做了个有效操作。
    ★ 丢弃不删行：留证据看「模型提过什么」，也留着以后改判回来。
    ★ 第十一步：`from=candidates` 的原生 form POST 重定向回查询页（`/wiki/candidate`
      默认返回 #wiki-body 片段，查询页没有那个容器）。2026-11-09 起查询页已纯
      查询、不再有这个表单；这条响应分支留给直接调 API 的脚本/旧链接，首页
      回看区的 htmx 路径（不带 from）是现存唯一 UI 入口。
    """
    from urllib.parse import quote
    from django.shortcuts import redirect

    def back(text, bad=False):
        return redirect("/wiki/candidates?msg=%s%s" % (
            quote(text), "&bad=1" if bad else ""))

    from_query = request.POST.get("from") == "candidates"
    if msg := _compile_running_guard():
        # ★ 编译开头已经把 backlog 快照算出来了，中途丢弃候选不会改变这一批，
        #   却会让人以为「我明明丢了、怎么还在草稿里」—— 直接挡住并说明。
        if from_query:
            return back(msg, bad=True)
        return _fragment(request, flash=msg)
    raw = (request.POST.get("id") or "").strip()
    status = (request.POST.get("status") or "").strip()
    try:
        cid = int(raw)
    except ValueError:
        text = "候选 id 非法：%r" % raw
        if from_query:
            return back(text, bad=True)
        return _fragment(request, ingest_msg=text, ingest_ok=False)
    if status == "accepted":
        text = "候选已无需逐条采纳：未丢弃的候选会直接进编译；要排除某条，在审批浮层让助手把它移出分组。"
        if from_query:
            return back(text, bad=True)
        return _fragment(request, ingest_msg=text, ingest_ok=False)
    ok, err = ingest.set_status(cid, status)
    if not ok:
        log.warning("wiki 候选状态修改失败：%s（%s）", cid, err)
        if from_query:
            return back(str(err), bad=True)
        return _fragment(request, ingest_msg=str(err), ingest_ok=False)
    text = "已标记为「%s」" % (
        {"pending": "待审", "discarded": "已丢弃"}.get(status, status))
    if from_query:
        return back(text)
    return _fragment(request, ingest_msg=text, ingest_ok=True)


# mutex-ok: 自己就是「编译」这一端；重复点击由 flock 编译锁挡，不挡自己。
@require_POST
def compile_run(request):
    """把新痕迹提炼成候选、再把未丢弃候选编译成草稿（第十一步：一条龙）。

    ★ 提炼对用户**隐身**：首页只有这一枚「编译」按钮，点下去由
      `ingest.extract_then_compile()` 把「痕迹 → 候选 → 草稿」一次做完。
      按来源单独编译的旧参数仍保留在代码层（脚本 / 测试可用），但首页不再
      暴露逐来源入口 —— 那种入口会把提炼过程重新摆回用户面前。
    ★ 花钱次数必须如实报：一条龙可能调 N 次提炼 + 一次编译，反馈要说
      「先提炼了 X 处、编出 Y 页」，不能只报编译结果（提炼阶段被守卫丢掉的
      候选、没提炼成的来源也不能静默）。
    """
    # 可选：只编译某个来源的未丢弃候选（代码层备用，UI 已不再暴露）
    st = (request.POST.get("source_type") or "").strip()
    sid = (request.POST.get("source_id") or "").strip()
    src = (st, sid) if st and sid else None
    res = (ingest.compile_candidates(source=src) if src
           else ingest.extract_then_compile())

    if not res.get("ok"):
        log.warning("wiki 编译未完成：%s", res.get("err"))
        return _fragment(request, ingest_msg="编译未完成：%s" % res.get("err"),
                         ingest_ok=False)

    drafts = res.get("drafts") or []
    dropped = res.get("dropped") or []
    parts = []
    # ★ 提炼那一段先说：花了几次钱、守卫丢了几条、哪几处没提炼成。
    #   `extract_dropped` / `extract_skipped` 只有一条龙路径才会带，按来源
    #   单独编译时是 0 / []，不该凭空冒出一段「提炼了 0 处」。
    if not src:
        extracted = res.get("extracted") or 0
        xd = res.get("extract_dropped") or 0
        xs = res.get("extract_skipped") or []
        lead = "先提炼了 %d 处新痕迹" % extracted
        if xd:
            lead += "，其中 %d 条候选被引文守卫丢弃" % xd
        # ★ 一次最多提炼 EXTRACT_MAX_SOURCES 个来源：没轮到的必须报出来，
        #   否则「点了、跑完了、痕迹还在」会被当成 bug（2026-11-09）。
        left = res.get("extract_left") or 0
        if left:
            lead += "；这一轮最多提炼 %d 个来源，还有 %d 处没轮到，再点一次编译接着来" % (
                ingest.EXTRACT_MAX_SOURCES, left)
        # ★ 提炼输出被预算硬截断的来源：它既不算提炼成功（不记台账）也不算失败
        #   （不是报错），必须**单独一句**说明，否则人只会看到「痕迹还在、什么都没变」，
        #   而真实原因是那篇正文太长、模型输出被切在半截（2026-11-09）。
        cut = res.get("extract_cut") or 0
        if cut:
            lead += ("；另有 %d 处正文太长、提炼时输出被截断，这次的内容不算数"
                     "（下次点编译会重新提炼它）" % cut)
        if xs:
            names = "、".join("%s（%s）" % (
                x.get("title") or x.get("source_id"), x.get("err") or "提炼失败")
                for x in xs[:3])
            if len(xs) > 3:
                names += " 等 %d 处" % len(xs)
            lead += "；%d 处没提炼成：%s" % (len(xs), names)
        parts.append(lead)
    if not drafts:
        # 「都已经编译过」是合法结果，不是失败 —— 说清楚，否则人会继续点
        parts.append(res.get("msg") or "没有新内容需要编译")
        return _fragment(request, ingest_msg="；".join(parts), ingest_ok=True)
    parts.append("生成了 %d 页草稿（在「待审批」里）：%s"
                 % (len(drafts), "、".join(drafts)))
    # ★ 并入与新建必须分开说：一个覆盖已有页、一个加新页。混成「生成了 3 页草稿」，
    #   人就不知道有 2 页会改写他已经确认过的知识（阶段 2.3d）。
    merged = [g for g in (res.get("groups") or [])
              if g.get("target") and not g.get("err") and not g.get("split")]
    if merged:
        parts.append("其中 %d 页是并入已有页（通过后会覆盖原页，旧版备份在 .drafts/.merged/）：%s"
                     % (len(merged), "、".join(g["target"] for g in merged)))
    # ★ 拆页必须单独说（第七步）：它同时产出「原页的替换稿」和「一页新草稿」，
    #   说成「并入」或「新建」都会让人漏看另一半 —— 而漏看的那一半正好是
    #   「我确认过的某一页会少几条判断」。
    splits = [g for g in (res.get("groups") or [])
              if g.get("split") and g.get("target") and not g.get("err")]
    if splits:
        parts.append("其中 %d 页是拆页重组（原页会生成替换稿、候选减少，拆出的候选与新"
                     "候选组成新页）：%s"
                     % (len(splits), "、".join(g["target"] for g in splits)))
    if res.get("noop"):
        parts.append("另有 %d 组没有新增内容（目标页里已有同样的话）：%s"
                     % (len(res["noop"]), "、".join(res["noop"])))
    for n in (res.get("notes") or []):
        parts.append("注意：%s" % n)
    # ★ 被守卫拒掉的组必须报（2026-11-03）：原先只念 drafts / dropped，
    #   `groups[].err` 一个字都不提 —— 人看到「生成了 12 页」，以为全编完了，
    #   切回页面按钮还亮，却不知道为什么。
    failed = [g for g in (res.get("groups") or []) if g.get("err")]
    if failed:
        def _one(g):
            ids = "、".join(str(i) for i in (g.get("cand_ids") or []))
            return "「%s」%s%s" % (g["title"], g["err"], "（候选 %s）" % ids if ids else "")
        parts.append("另有 %d 组没编成：%s" % (len(failed), "；".join(_one(g) for g in failed)))
    if dropped:
        # ★ 不静默：丢弃说明守卫在起作用，但人要知道模型想干什么
        why = {}
        for d in dropped:
            why[d.get("why") or "?"] = why.get(d.get("why") or "?", 0) + 1
        parts.append("另有 %d 条被守卫丢弃（%s）" % (
            len(dropped), "；".join("%s ×%d" % (k, v) for k, v in why.items())))
    skipped = res.get("skipped") or 0
    if skipped:
        parts.append("%d 条之前已编译过（草稿或已入库），本次跳过" % skipped)
    if res.get("over_split"):
        # ★ 分组退化 = 这次分组的价值没兑现，要让人当场看见（否则只是一堆半页）
        parts.append("注意：几乎是一条候选一页，分组可能拆得过细 —— "
                     "读草稿时留意该合并的页，可直接在草稿里并起来再审批")
    collisions = res.get("collisions") or []
    if collisions:
        # ★ 改名不是小事：人下次找这一页会按原名搜不到。而且撞名通常意味着
        #   「这个概念已经有页了」，真正该做的是合并，不是并存两页 —— 说出来，
        #   别让它变成一个安静的 `-2` 后缀。
        parts.append("注意：%s 与已入库页同名，已改名入库 —— 更该做的是把那两页合并"
                     % "、".join("%s→%s" % (c["wanted"], c["used"]) for c in collisions))
    log.info("wiki 编译：%d 页草稿，丢 %d 条", len(drafts), len(dropped))
    return _fragment(request, ingest_msg="；".join(parts), ingest_ok=True,
                     open_approval=True)


def compile_watch(request):
    """编译中状态条（htmx 每 5 秒 GET 一次，替掉自己）。

    ★ 为什么需要这个端点（2026-11-03）：按钮该不该灰，是**服务端的事实**
      （有一份编译在跑），不是前端记性。页面切走再回来只能重新问一次服务端；
      而人如果一直停在页面上，也要能从「编译中」变回「可以点」—— 只靠人手动
      刷新，等于把「它到底跑完没有」交给运气（停在旧状态上的人会一直点不动，
      而这看起来和「坏了」一模一样）。
    ★ 跑完不回片段，而是回 `HX-Refresh: true` 让 htmx 整页重载：
      编译的产出是**草稿**，它出现在页面另一处（待审批），局部换一块换不出来。
      这里也不自己做一次「跳过重载」，因为那会把新草稿藏起来 —— 人点编译就是为了看它。
    """
    st = ingest.compile_state()
    if not st.get("running"):
        resp = HttpResponse("")
        resp["HX-Refresh"] = "true"
        return resp
    return render(request, "wiki/_compile_busy.html", {"compile_run": st})


# ---------------------------------------------------------------------------
# 划线 / 批注（阶段 1 的 1.3）。锚定 (source_type, source_id)，软引用。
# 组件 `wiki/_article_tools.html` 挂到文章详情（reader/_entry_detail.html）与
# 日报/周报详情（summary/detail.html、weekly_detail.html）三处，同一个 partial。
# ---------------------------------------------------------------------------

def _render_body(source_type: str, source_id: str):
    """按 (source_type, source_id) 重渲染正文（带 inline 高亮）的 HTML。

    ★ 这是 marks 端点「局部刷新正文」的关键：新增/删除划线后，正文里的高亮
      也要跟着变，但**不能整页刷新**（会跳回顶部、打断阅读）。所以端点返回
      一个 `#article-body-*` 的 htmx OOB 片段，这里负责算出片段内容。
    ★ 正文从哪来、怎么渲染，是各数据源自己的事；本函数只做「按类型分发」，
      不复制 reader/summary 的取数与渲染判据。
    ★ 返回的是**完整正文容器元素**（含正确标签 + class + id），不是 innerHTML：
      htmx 的 hx-swap-oob 用 outerHTML 替换，若这里给 `<div>` 而页面是
      `<article class="report">`，替换后标签和 class 全丢、样式崩掉（实测）。
    """
    data = marks.list_marks(source_type, source_id)
    tag = "article" if source_type == "report" else "div"
    cls = "report" if source_type == "report" else "detail-content"
    oid = "article-body-%s-%s" % (source_type, source_id)
    undisplayed = []          # 没能在正文里落位的标记 id（交给底部列表兜底）
    # ★ 取数分发只有一份（core.sourceblock）：对话也要正文，各写一份必然漂
    #   ——「页面看到的」和「模型看到的」会变成两篇不同的文章。
    blk = sourceblock.load(source_type, source_id)
    if blk is None:
        return None
    try:
        inner = marks.annotate_html(blk["html"], data, undisplayed)
        return ('<%s class="%s" id="%s" hx-swap-oob="true">%s</%s>' % (
            tag, cls, oid, inner, tag), undisplayed)
    except Exception as exc:  # noqa: BLE001  取不到正文就只刷新列表，不拖垮整个动作
        log.warning("marks 正文重渲染失败 %s/%s: %s", source_type, source_id, exc)
    return None


def _mark_fragment_chat(request, msg_id, flash="", err=""):
    """对话消息上的标记：**只返回这一条消息的 OOB 片段**（连同它自己的标记列表）。

    ★ 与文章的区别：文章的正文是「一整篇」，消息是「一条」。所以换的是那一条消息，
      而不是 `#article-body-*`；列表结构仍复用同一个 `_marks.html`，hover 改/删
      与文章完全一致 —— 少一套 UI 就少一处会漂的实现。
    """
    from core.models import ChatMessage

    msg = None
    if (msg_id or "").isdigit():
        msg = ChatMessage.objects.filter(pk=int(msg_id)).first()

    if msg is None:
        # 消息被清空了：没有可刷新的对象，直接说清楚（别静默成功）
        html = render_to_string("wiki/_marks.html", {
            "source_type": "chat", "source_id": msg_id,
            "highlights": [], "annotations": [], "undisplayed": [],
            "err": err or "这条对话消息不存在（可能已被清空）", "flash": "",
        }, request=request)
        return HttpResponse(html)

    ctx = _wc_msg_ctx(msg)
    # ★ 对话这条路**只返回 OOB 的那条消息**，主响应不换任何东西（HX-Reswap: none）：
    #   消息的 `.marks` 容器就在消息里面，返回「列表片段 + OOB 消息」会让 htmx 先
    #   替换列表、再整条替换消息，同一块 DOM 被换两次（第二次拿到的是已销毁的节点）。
    #   只换一次，结构上不存在这个顺序问题。
    html = render_to_string("wiki/_wc_msg.html", dict(ctx, oob=1), request=request)

    resp = HttpResponse(html)
    resp["HX-Reswap"] = "none"
    if flash:
        resp["HX-Trigger"] = json.dumps({"wikiFlash": {"msg": flash}})
    return resp


def _mark_fragment(request, source_type, source_id, flash="", err=""):
    """某内容块「划线 + 批注」列表的替换片段。

    ★ 与 _fragment 同一条纪律：toast 不占位、中文走 \\uXXXX（HTTP 头 latin-1）。
    ★ 片段只依赖 (source_type, source_id)，不关心内容从哪来 —— 这就是组件能
      三处复用的根。
    ★ 新增/删除划线后，正文 inline 高亮也要同步变。所以这里除了列表片段，还
      **追加一个 `#article-body-<type>-<id>` 的 OOB 片段**（重新渲染带高亮的正文），
      让前端一次 htmx 请求就把「列表 + 正文高亮」两处都刷新，且**不整页跳顶**。
    ★ 列表只装「正文里落不了位」的标记（undisplayed）。所以必须先渲染正文拿到这个
      集合，再渲染列表 —— 顺序反了列表就永远为空。
    ★ `chat` 走另一条渲染路径（消息 → 单条消息 + 它自己的列表），见
      `_mark_fragment_chat`；同一条消息的 OOB 目标不是 `#article-body-*`。
    """
    if (source_type or "").strip() == "chat":
        return _mark_fragment_chat(request, source_id, flash=flash, err=err)

    data = marks.list_marks(source_type, source_id)
    # 正文 OOB：只有确实取到正文才带（entry 被删 / 报告不存在时静默只刷列表）
    # ★ _render_body 返回 (完整元素, 未落位 id 列表)，元素已含 hx-swap-oob="true"。
    body_html, undisplayed = None, []
    got = _render_body(source_type, source_id)
    if got is not None:
        body_html, undisplayed = got

    html = render_to_string("wiki/_marks.html", {
        "source_type": source_type, "source_id": source_id,
        "highlights": data["highlights"], "annotations": data["annotations"],
        "undisplayed": undisplayed,
        "err": data["err"] or err, "flash": flash,
    }, request=request)
    if body_html is not None:
        html += body_html

    resp = HttpResponse(html)
    if flash:
        resp["HX-Trigger"] = json.dumps({"wikiFlash": {"msg": flash}})
    return resp


# mutex-ok: 痕迹记录。用户口径是「暂停的是触发，不是记录」：编译期间划线照常落库。
@require_POST
def highlight(request):
    """存一条划线（摘录）。返回该内容块的 marks 片段（新增后整体刷新）。"""
    st = (request.POST.get("source_type") or "").strip()
    sid = (request.POST.get("source_id") or "").strip()
    ok, payload = marks.add_highlight(
        st, sid, request.POST.get("quoted_text"),
        note=request.POST.get("note"), anchor=request.POST.get("anchor"))
    if ok:
        # 复活 = 同一条痕迹回来了，说清是「恢复」而不是「又新建了一条」
        return _mark_fragment(request, st, sid,
                              flash="已恢复这条划线" if payload.get("restored") else "已划线")
    return _mark_fragment(request, st, sid, err=payload["err"])


# mutex-ok: 痕迹记录。用户口径是「暂停的是触发，不是记录」：编译期间批注照常落库。
@require_POST
def annotation(request):
    """存一条批注（我的判断）。"""
    st = (request.POST.get("source_type") or "").strip()
    sid = (request.POST.get("source_id") or "").strip()
    ok, payload = marks.add_annotation(
        st, sid, request.POST.get("body"), anchor=request.POST.get("anchor"))
    if ok:
        return _mark_fragment(request, st, sid,
                              flash="已恢复这条批注" if payload.get("restored") else "已记批注")
    return _mark_fragment(request, st, sid, err=payload["err"])


# mutex-ok: 痕迹记录的维护动作，只动 marks 表，不碰 .drafts/ 或 wiki/。
@require_POST
def mark_delete(request):
    """软删一条划线或批注（按类型 + id）。返回 marks 片段。

    ★ 删之前先问「有没有候选引用这条痕迹」：软删后它不再出现在页面、也不再
      进提炼，若 wiki 里还有候选的 `mark_id` 指向它，人得知道 —— 静默断掉
      溯源最贵。计数只用于提示，不拦截删除。
    """
    st = (request.POST.get("source_type") or "").strip()
    sid = (request.POST.get("source_id") or "").strip()
    kind = (request.POST.get("kind") or "").strip()
    mark_id = (request.POST.get("id") or "").strip()
    cites = (marks.mark_ref_count(kind, mark_id)
             if kind in ("highlight", "annotation") else 0)
    if kind == "highlight":
        ok = marks.delete_highlight(mark_id)
    elif kind == "annotation":
        ok = marks.delete_annotation(mark_id)
    else:
        ok = False
    flash = "已删除" if ok else "这条痕迹不存在，或已经删过了"
    if cites:
        flash += "；注意：它已被 %d 条候选引用，wiki 页的溯源仍指向它" % cites
    return _mark_fragment(request, st, sid, flash=flash)


# mutex-ok: 痕迹记录的维护动作，只动 marks 表，不碰 .drafts/ 或 wiki/。
@require_POST
def mark_edit(request):
    """编辑一条划线（改备注）或批注（改正文）。返回 marks 片段。"""
    st = (request.POST.get("source_type") or "").strip()
    sid = (request.POST.get("source_id") or "").strip()
    kind = (request.POST.get("kind") or "").strip()
    mark_id = (request.POST.get("id") or "").strip()
    ok, err = marks.edit_mark(kind, mark_id, request.POST.get("body"))
    if ok:
        return _mark_fragment(request, st, sid, flash="已更新")
    return _mark_fragment(request, st, sid, err=err)


# ---------------------------------------------------------------------------
# 对话（阶段 2.1）。组件 `wiki/_chat.html` 由 `_article_tools.html`
# 用 hx-get 懒加载 —— 三处详情页（文章/日报/周报）**一行都不用改**，
# 也不用在每个视图里多传一份对话上下文（两套上下文一定会漂）。
#
# ★ 流式（SSE，2026-11-19）：新增 `chat_stream_send` / `approval_chat_stream_send`
#   两个端点，与下面的非流式 send 端点**并存**。前端 fetch 走流式，失败自动回退
#   到非流式 htmx form（老端点一行没动）—— 这样流式是「增强」不是「替换」。
#   ★ 权威渲染仍是服务端模板：流式只逐字吐正文，末尾 `done` 帧带整块渲染好的
#     片段（markdown/marks/OOB 全对），前端用它 outerHTML 覆盖 —— 绝不在前端
#     重复实现 markdown 渲染（那必然与服务端漂）。
# ---------------------------------------------------------------------------

def _sse_frame(obj: dict) -> str:
    """一条 SSE data 帧（JSON 编码，中文不转义；前端 JSON.parse 后取字段）。"""
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _sse_conversation(work, on_done):
    """在后台线程跑一次生成，主线程边收边 yield SSE 帧。

    `work(on_delta) -> result`：真正的生成函数（conversation.ask / wiki_loop.run），
      它内部落库；on_delta 把文字增量推进队列。
    `on_done(result) -> str`：生成结束后，主线程据此渲染**权威 HTML 片段**，
      作为 `done` 帧发回（渲染必须在主线程：request 对象不跨线程）。

    ★ 为什么后台线程：StreamingHttpResponse 的 generator 是同步迭代的，生成函数
      阻塞十几秒；把生成放线程、用 queue 传增量，generator 才能边收边吐而不是
      等全部算完再一次性返回（那就退回「傻等」了）。
    ★ ORM 跨线程：worker 开头 close_old_connections()，Django 会给本线程新连接；
      ask/run 自己落库，结束时再 close —— 不复用主线程的连接。
    """
    q = queue.Queue()

    def on_delta(piece):
        if piece:
            q.put(("delta", piece))

    def worker():
        try:
            close_old_connections()
            res = work(on_delta)
            q.put(("result", res))
        except Exception as exc:                      # 不让线程异常变成永久挂起
            log.exception("wiki 流式生成线程异常")
            q.put(("error", str(exc)))
        finally:
            close_old_connections()
            q.put(("__end__", None))

    threading.Thread(target=worker, name="wiki-chat-stream", daemon=True).start()

    def gen():
        while True:
            kind, payload = q.get()
            if kind == "__end__":
                break
            if kind == "delta":
                yield _sse_frame({"t": "delta", "v": payload})
            elif kind == "error":
                yield _sse_frame({"t": "err", "v": payload})
            elif kind == "result":
                try:
                    yield _sse_frame({"t": "done", "html": on_done(payload)})
                except Exception:
                    log.exception("wiki 流式 done 帧渲染失败")
                    yield _sse_frame({"t": "err", "v": "渲染回复失败，请重试"})

    resp = StreamingHttpResponse(gen(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"       # nginx 关缓冲，否则逐字效果被吞
    return resp

def _wc_msg_ctx(m, *, show_marks: bool = True) -> dict:
    """一条消息的渲染上下文。

    ★ 消息级标记单独一套 source：`source_type="chat"`、`source_id=<消息 id>`。
      理由：锚点是「在这段文字里的位置」，而对话消息与文章正文是两段不同的文字；
      混用同一个 source_id 会让文章渲染器去找根本不在正文里的句子 —— 全部落位失败，
      于是每条都掉进「未在正文中定位」列表，看着像坏了。
    ★ `show_marks=False`（2026-11-22 审批对话）：连查都不查痕迹，渲染也不过
      `annotate_html`。模板据此不输出 `data-mark-*` / marks 容器 —— 不只是「看不见」，
      连「选中审批消息后冒出划线 toolbar、存了条看不见的痕迹」这条路也一起断掉。
      文章对话走默认值，行为不变。
    """
    if show_marks:
        data = marks.list_marks("chat", str(m.id))
        undisplayed = []
        html = marks.annotate_html(mdrender.render(m.body), data, undisplayed)
    else:
        data = {"highlights": [], "annotations": [], "err": ""}
        undisplayed = []
        html = mdrender.render(m.body)
    return {
        "m": {"id": m.id, "role": m.role,
              "label": "我" if m.role == "user" else "AI",
              "html": html},
        "show_marks": show_marks,
        "marks": data,
        "marks_undisplayed": undisplayed,
    }


def _chat_ctx(source_type: str, source_id: str, err: str = "") -> dict:
    """对话片段的数据。消息在服务端渲成 HTML（mdrender 先 escape，无 XSS 面）。"""
    conv = conversation.get_or_none(source_type, source_id)
    rows = conversation.history(conv) if conv else []
    return {
        "source_type": source_type,
        "source_id": source_id,
        "chat_id": "wiki-chat-%s-%s" % (source_type, source_id),
        "messages": [_wc_msg_ctx(m) for m in rows],
        "err": err,
        "enabled": settings.WIKI_CHAT_ENABLED,
    }


def chat(request):
    """GET：懒加载对话片段（历史 + 输入框）。"""
    st = (request.GET.get("source_type") or "").strip()
    sid = (request.GET.get("source_id") or "").strip()
    return render(request, "wiki/_chat.html", _chat_ctx(st, sid))


# mutex-ok: 阅读侧文章对话，与 wiki 审批 / 编辑文件无关。
@require_POST
def chat_send(request):
    """POST：存下我的问题 → 调模型 → 存回复 → 返回整个对话片段。

    ★ 等待时间可能十几秒（非流式）。前端用 hx-indicator 显示「思考中…」并
      禁用按钮 —— 没有指示的长等待，用户会以为点了没反应而重复点，白花钱。
    ★ 失败原因**写在对话区里**（`err`），不用 toast：toast 几秒就消失，
      用户回头看时只剩下「怎么没回复」，不知道是预算用尽还是网络失败。
    """
    st = (request.POST.get("source_type") or "").strip()
    sid = (request.POST.get("source_id") or "").strip()
    res = conversation.ask(st, sid, request.POST.get("body"))
    if not res["ok"]:
        log.warning("wiki 对话未完成 %s/%s: %s", st, sid, res["err"])
    return render(request, "wiki/_chat.html", _chat_ctx(st, sid, err=res["err"]))


# mutex-ok: 阅读侧文章对话，与非流式 send 同一入口，只是边生成边吐。
@require_POST
def chat_stream_send(request):
    """流式版 `chat_send`：逐字吐正文，末尾带权威片段。生成走 conversation.ask(on_delta)。"""
    st = (request.POST.get("source_type") or "").strip()
    sid = (request.POST.get("source_id") or "").strip()
    body = request.POST.get("body")

    def work(on_delta):
        return conversation.ask(st, sid, body, on_delta=on_delta)

    def on_done(res):
        if not res["ok"]:
            log.warning("wiki 流式对话未完成 %s/%s: %s", st, sid, res["err"])
        return render_to_string("wiki/_chat.html",
                                _chat_ctx(st, sid, err=res["err"]), request=request)

    return _sse_conversation(work, on_done)


# mutex-ok: 阅读侧文章对话的清空，与 wiki 审批 / 编辑文件无关。
@require_POST
def chat_clear(request):
    """清空该内容块的对话（连消息一起删，不可恢复）。"""
    st = (request.POST.get("source_type") or "").strip()
    sid = (request.POST.get("source_id") or "").strip()
    conversation.clear(st, sid)
    return render(request, "wiki/_chat.html", _chat_ctx(st, sid))
