"""树的写操作：分类新增/重命名、feed 新增、feed 拖动移动、三层拖动重排。

★ 边界（2026-10-20）
-------------------
- **分类 / feed 的增改是写 Miniflux**（它仍是唯一真相层），写完 `_invalidate()` + 前端重载树。
  在此之前阅读器对 Miniflux 是只读的；本地写入只发生在虚拟分组层。
- **feed 新增是同步的**：Miniflux 在 POST /v1/feeds 里会立刻抓取并解析 URL，
  慢源可能 10–30 秒。失败时什么都不写，前端也不会刷新树 —— 即"成功才进树"。
- **跨分类移动仍然拒绝**：改 Miniflux 的 `category_id` 会
  ① 清零 `parsing_error_count` ② 历史条目整体改属 ③ 判据继承立刻变化
  ④ 本地分组归属变成跨分类残留。这是独立的一次改动，不在本轮。
  前端会在拖动时先拦一次，这里再兜一次（防绕过 UI 直接调接口）。
- **拖动重排只写本地顺序表**（`core.TreeOrder`，见 `core/treeorder.py`）：Miniflux
  侧一个字节都不动，所以重排不碰上面那条雷，也随时能「恢复默认顺序」（删掉本层的行）。
  归属（哪个源在哪个分类）与顺序（同层谁先谁后）是两件事，各写各的接口。
"""
import logging

from django.views.decorators.http import require_POST

from core import readergroups as rgroups
from core import treeorder
from core import treekit
from core.httpkit import err as _err, int_param as _int_param, ok as _ok
from core.datasource import get_datasource

# 重排要按「侧栏看到的那一层」校验，所以直接借侧栏的取树口径（同一个 decorate +
# apply），不在这里再拼一遍 —— 两处口径不一致就会放过过期顺序。
from .views import tree_view

log = logging.getLogger(__name__)

MAX_TITLE = 64
MAX_URL = 2048
CROSS_CATEGORY_MSG = ("跨分类移动暂未开放：它会清零该源的解析错误计数、"
                      "把历史条目整体改属到新分类，并立刻切换判据继承")


def _upstream_error(exc) -> str:
    """Miniflux 的报错原文（它一般返回 {"error_message": ...}）。"""
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            data = None
        if isinstance(data, dict):
            msg = data.get("error_message") or data.get("error") or ""
            if msg:
                return str(msg)[:200]
        text = (getattr(resp, "text", "") or "").strip()
        if text:
            return text[:200]
    return str(exc)[:200]


def _clean_title(raw) -> str:
    return " ".join((raw or "").split())[:MAX_TITLE]


# ------------------------------------------------------------------ 分类
@require_POST
def category_create(request):
    title = _clean_title(request.POST.get("title"))
    if not title:
        return _err("分类名不能为空")
    try:
        data = get_datasource().create_category(title)
    except Exception as exc:  # noqa: BLE001
        log.warning("新建分类失败：%s", exc)
        return _err(f"新建分类失败：{_upstream_error(exc)}", 502)
    if not data.get("id"):
        return _err("Miniflux 没有返回新分类 id（可能未生效）", 502)
    log.info("新建分类 id=%s title=%s", data["id"], data.get("title") or title)
    return _ok("已新建分类", id=data["id"], title=data.get("title") or title)


@require_POST
def category_rename(request):
    cid = _int_param(request, "category_id")
    title = _clean_title(request.POST.get("title"))
    if not cid:
        return _err("缺 category_id")
    if not title:
        return _err("分类名不能为空")
    try:
        data = get_datasource().rename_category(cid, title)
    except Exception as exc:  # noqa: BLE001
        log.warning("重命名分类 %s 失败：%s", cid, exc)
        return _err(f"重命名失败：{_upstream_error(exc)}", 502)
    log.info("重命名分类 id=%s → %s", cid, title)
    return _ok("已重命名", id=cid, title=(data or {}).get("title") or title)


@require_POST
def category_delete(request):
    """移除空分类。

    ★ 只允许**空分类**（0 个 feed）：有源的分类要先把源移走。
      非空分类的删除在 Miniflux 里会牵涉 feed 归属，和跨分类移动是同一类风险，
      不在本轮放开。
    ★ 分类下的**本地虚拟分组**（以及分类级判据）跟着一起删：否则会留下
      看不见的孤儿分组 / 孤儿配置。
    """
    cid = _int_param(request, "category_id")
    if not cid:
        return _err("缺 category_id")
    try:
        tree = treekit.tree()
    except Exception as exc:  # noqa: BLE001
        return _err(f"取订阅树失败：{exc}", 502)

    cat = next((c for c in tree if c.id == cid), None)
    if cat is None:
        return _err("分类不存在（可能已被删除）", 404)
    if cat.feeds:
        return _err(f"只能移除空分类：这个分类下还有 {len(cat.feeds)} 个源", 409)

    try:
        get_datasource().delete_category(cid)
    except Exception as exc:  # noqa: BLE001
        log.warning("移除分类 %s 失败：%s", cid, exc)
        return _err(f"移除失败：{_upstream_error(exc)}", 502)

    # Miniflux 侧已删；本地分组 / 判据尽力清掉（失败只告警，不把已完成的删除报成失败）
    # ★ 级联规则（分组 + 分类判据一起清）住在 core.readergroups —— 那是分组域的事
    warning = ""
    ngroups = 0
    try:
        ngroups = rgroups.delete_category_local(cid)
    except Exception as exc:  # noqa: BLE001
        warning = f"分类已移除，但本地分组/判据清理失败：{exc}"
        log.warning("清理分类 %s 的本地数据失败：%s", cid, exc)

    log.info("移除空分类 id=%s title=%s groups=%s", cid, cat.title, ngroups)
    return _ok("已移除分类", id=cid, title=cat.title, warning=warning)


# ------------------------------------------------------------------ feed
@require_POST
def feed_create(request):
    feed_url = (request.POST.get("feed_url") or "").strip()
    category_id = _int_param(request, "category_id")
    group_id = _int_param(request, "group_id")

    if not category_id:
        return _err("缺 category_id")
    if not feed_url:
        return _err("Feed URL 不能为空")
    if len(feed_url) > MAX_URL:
        return _err("Feed URL 过长")
    if not feed_url.lower().startswith(("http://", "https://")):
        return _err("Feed URL 必须以 http:// 或 https:// 开头")

    try:
        data = get_datasource().create_feed(feed_url, category_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("新增 feed 失败 url=%s：%s", feed_url, exc)
        return _err(f"新增失败：{_upstream_error(exc)}", 502)

    # ★ 实测（Miniflux 2.3.x）：POST /v1/feeds 返回的是 `{"feed_id": 57}`，
    #   不是完整 feed 对象；分类才是 `{"id": ...}`。两种键都兜一下。
    feed_id = data.get("id") or data.get("feed_id")
    if not feed_id:
        return _err("Miniflux 没有返回新 feed id（可能未生效）", 502)

    warning = ""
    if group_id:
        # ★ feed 必须已经在树里才能挂分组；create_feed 已 _invalidate，这里拉的是新树。
        try:
            rgroups.move_feed(feed_id, group_id)
        except Exception as exc:  # noqa: BLE001
            warning = f"feed 已创建，但加入分组失败：{exc}"
            log.warning("新增 feed %s 后挂分组 %s 失败：%s", feed_id, group_id, exc)

    log.info("新增 feed id=%s url=%s category=%s group=%s",
             feed_id, feed_url, category_id, group_id or "-")
    return _ok("已添加", feed_id=feed_id,
               title=data.get("title") or feed_url, warning=warning)


@require_POST
def feed_rename(request):
    """重命名 feed（**只改标题**）。

    ★ 不支持切换分类：这里连 `category_id` 参数都不读 —— 改归属会清零解析错误
      计数、让历史条目改属、并立刻切换判据继承（模块头的跨分类条款）。
      要挪分类将来单做一次，不做"顺手"。
    """
    fid = _int_param(request, "feed_id")
    title = _clean_title(request.POST.get("title"))
    if not fid:
        return _err("缺 feed_id")
    if not title:
        return _err("源名称不能为空")

    try:
        tree = treekit.tree()
    except Exception as exc:  # noqa: BLE001
        return _err(f"取订阅树失败：{exc}", 502)
    node = next((f for c in tree for f in c.feeds if f.id == fid), None)
    if node is None:
        return _err("这个源不在当前树里（可能已被删除）", 404)

    try:
        data = get_datasource().rename_feed(fid, title)
    except Exception as exc:  # noqa: BLE001
        log.warning("重命名 feed %s 失败：%s", fid, exc)
        return _err(f"重命名失败：{_upstream_error(exc)}", 502)

    log.info("重命名 feed id=%s：%s → %s", fid, node.title, title)
    return _ok("已重命名", feed_id=fid, title=(data or {}).get("title") or title)


@require_POST
def feed_delete(request):
    """删除订阅（**不可逆**：Miniflux 会连这个源的全部条目一起删）。

    ★ `confirm=1` 是**契约的一部分**，不是前端的礼貌：菜单项、脚本、误触的
      双击都得带上它；后端不认没确认的删除请求。
    ★ Miniflux 侧删成功后才清本地（`rgroups.delete_feed_local`）；清不掉只
      warning —— 订阅已经没了，不能报成"删除失败"让人再点一次。
    """
    fid = _int_param(request, "feed_id")
    if not fid:
        return _err("缺 feed_id")
    if (request.POST.get("confirm") or "").strip() != "1":
        return _err("删除需要确认（confirm=1）")

    try:
        tree = treekit.tree()
    except Exception as exc:  # noqa: BLE001
        return _err(f"取订阅树失败：{exc}", 502)
    node = next((f for c in tree for f in c.feeds if f.id == fid), None)
    if node is None:
        return _err("这个源不在当前树里（可能已被删除）", 404)

    try:
        get_datasource().delete_feed(fid)
    except Exception as exc:  # noqa: BLE001
        log.warning("删除 feed %s 失败：%s", fid, exc)
        return _err(f"删除失败：{_upstream_error(exc)}", 502)

    warning, cleaned = "", {}
    try:
        cleaned = rgroups.delete_feed_local(fid)
    except Exception as exc:  # noqa: BLE001
        warning = f"订阅已删除，但本地配置清理失败：{exc}"
        log.warning("清理 feed %s 的本地数据失败：%s", fid, exc)

    log.info("删除 feed id=%s title=%s 本地清理=%s", fid, node.title, cleaned)
    return _ok("已删除订阅", feed_id=fid, title=node.title,
               cleaned=cleaned, warning=warning)


# ------------------------------------------------------------------ 拖动
@require_POST
def feed_move(request):
    """拖动落点：`group:<id>` = 移入该组；`cat:<id>` = 移出分组到该分类根。

    ★ 同分类内才允许；跨分类一律拒绝（见模块头）。
    """
    feed_id = _int_param(request, "feed_id")
    target = (request.POST.get("target") or "").strip()
    if not feed_id:
        return _err("缺 feed_id")

    try:
        tree = treekit.tree()
    except Exception as exc:  # noqa: BLE001
        return _err(f"取订阅树失败：{exc}", 502)

    if target.startswith("group:"):
        gid = int(target.split(":", 1)[1]) if target.split(":", 1)[1].isdigit() else None
        if not gid:
            return _err("目标分组非法")
        try:
            rgroups.move_feed(feed_id, gid, tree=tree)
        except ValueError as exc:
            msg = str(exc)
            if "本分类" in msg or "跨分类" in msg:
                return _err(CROSS_CATEGORY_MSG, 409)
            return _err(msg)
        except Exception as exc:  # noqa: BLE001
            return _err(f"移动失败：{exc}", 502)
        return _ok("已移入分组")

    if target.startswith("cat:"):
        cid = int(target.split(":", 1)[1]) if target.split(":", 1)[1].isdigit() else None
        if not cid:
            return _err("目标分类非法")
        current = treekit.cat_of(feed_id, tree)
        if current is None:
            return _err("这个源不在当前树里（可能已被删除）")
        if current != cid:
            return _err(CROSS_CATEGORY_MSG, 409)
        try:
            rgroups.move_feed(feed_id, None, tree=tree)
        except Exception as exc:  # noqa: BLE001
            return _err(f"移出分组失败：{exc}", 502)
        return _ok("已移出分组")

    return _err("目标非法（应为 group:<id> 或 cat:<id>）")


# ------------------------------------------------------------------ 拖动重排
def _id_list(raw):
    """`"3,1,2"` → ([3,1,2], "")。任何一个不是纯数字就整串拒（顺序缺一都可能洗错）。"""
    ids = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            return [], part
        ids.append(int(part))
    return ids, ""


@require_POST
def tree_reorder(request):
    """整层落库：`kind` = cat/grp/feed，`scope` = "" / `cat:<id>` / `grp:<id>`，
    `ids` = 拖动后**这一层全部**项的 id（逗号分隔，靠前的在前）。

    ★ 送整层而不是"从第几位挪到第几位"：顺序是这一层的属性，逐项挪位会把校验
      变成猜谜（谁排第几取决于别人）。后端拿侧栏同一份口径（`views.tree_view()`）
      比一遍，项集对不上就整笔拒 —— 前端随后 reload，页面不会停在假顺序上。
    ★ 只写本地 `TreeOrder`，不碰 Miniflux（归属和顺序分两个接口，各自的坑不同）。
    """
    kind = (request.POST.get("kind") or "").strip()
    scope = (request.POST.get("scope") or "").strip()
    if kind not in treeorder.KINDS:
        return _err(f"未知的层类型：{kind}")
    ids, bad = _id_list(request.POST.get("ids"))
    if bad:
        return _err(f"顺序里有非法 id：{bad[:20]}")
    if not ids:
        return _err("缺少 ids（这一层拖动后的全部项）")

    try:
        tree = tree_view()
    except Exception as exc:  # noqa: BLE001
        return _err(f"取订阅树失败：{exc}", 502)

    try:
        n = treeorder.set_order(kind, scope, ids, tree)
    except ValueError as exc:
        msg = str(exc)
        # 层内容变了 = 前端那份 DOM 过期了，409 让调用方知道该刷新而不是改参数
        return _err(msg, 409 if "已经变了" in msg else 400)
    except Exception as exc:  # noqa: BLE001
        log.warning("重排 %s %s 失败：%s", kind, scope or "-", exc)
        return _err(f"重排失败：{exc}", 502)

    return _ok("已重排", kind=kind, n=n)


@require_POST
def tree_order_reset(request):
    """恢复这一层的默认顺序（分类按名称、目录按创建序、feed 按未读优先）。"""
    kind = (request.POST.get("kind") or "").strip()
    scope = (request.POST.get("scope") or "").strip()
    if kind not in treeorder.KINDS:
        return _err(f"未知的层类型：{kind}")
    try:
        n = treeorder.reset(kind, scope)
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001
        log.warning("恢复默认顺序 %s %s 失败：%s", kind, scope or "-", exc)
        return _err(f"恢复失败：{exc}", 502)
    return _ok("已恢复默认顺序" if n else "这一层本来就没有手动顺序", n=n)
