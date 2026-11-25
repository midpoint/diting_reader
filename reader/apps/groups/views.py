"""虚拟分组的读写接口（P0：右键先行，暂不做拖动）。

★ 这一层只做**本地视图**的事：建组 / 改名 / 删组 / feed 在同分类内换组、
  清理失效归属。**完全不碰 Miniflux** —— 跨分类移动（改 feed 的 category_id）
  本期不支持，理由见 `core/readergroups.py` 模块头的第 2 条。
★ 响应统一 `{ok, detail}` / `{ok:false, error}`，与 `app.js::postForm` 的既有约定一致。
"""
import logging

from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST

from core import readergroups as rg
from core.datasource import get_datasource
from core.httpkit import err as _err, ok as _ok, require_int

log = logging.getLogger(__name__)


def _tree():
    """当前树（= Miniflux 的现状）。取不到就返回空树：写操作会被"feed 不在树里"挡住，
    这比在 Miniflux 抽风时凭本地表乱写要好。"""
    try:
        return get_datasource().list_tree()
    except Exception as exc:  # noqa: BLE001
        log.warning("取树失败（分组写操作将拒绝）：%s", exc)
        return []


@require_POST
def create(request):
    try:
        cat_id = require_int(request, "category_id")
        g = rg.create_group(cat_id, request.POST.get("title") or "")
    except ValueError as exc:
        return _err(exc)
    return _ok(f"已新建分组「{g.title}」",
               group={"id": g.id, "title": g.title, "category_id": g.category_id})


@require_POST
def rename(request):
    try:
        gid = require_int(request, "group_id")
        title = rg.rename_group(gid, request.POST.get("title") or "")
    except ValueError as exc:
        return _err(exc)
    return _ok(f"已改名为「{title}」")


@require_POST
def delete(request):
    """删除分组（**不删 feed**）：组内 feed 回到分类根。"""
    try:
        gid = require_int(request, "group_id")
        n = rg.delete_group(gid)
    except ValueError as exc:
        return _err(exc)
    if not n:
        return _err("分组不存在", status=404)
    return _ok("已删除分组，组内源已回到分类下")


@require_POST
def move(request):
    """feed 移入分组；`group_id` 空 = 移出到分类根。

    ★ 只允许同分类内移动。跨分类一律拒绝（不是"顺手做掉"）：那会改 Miniflux 归属，
      并连带清零该源的解析错误计数（见 readergroups 模块头）。
    """
    try:
        fid = require_int(request, "feed_id")
        raw_gid = (request.POST.get("group_id") or "").strip()
        g = rg.move_feed(fid, raw_gid or None, tree=_tree())
    except ValueError as exc:
        return _err(exc)
    if g is None:
        return _ok("已移出分组")
    return _ok(f"已移入「{g.title}」")


@require_POST
def cleanup(request):
    """清理失效归属。

    ★ 树为空（Miniflux 取不到 / 返回空）时**必须拒绝**：空树会让每个成员都落进
      "feed 不在树里"，一次调用就删光全部分组归属（2026-10-16 事故的接口侧成因）。
      `cleanup_orphans` 内部也有一层同样的保护，这里是第二道闸。
    """
    tree = _tree()
    if not tree:
        log.warning("groups.cleanup：当前树为空，拒绝清理以免误删全部归属")
        return _err("取不到当前树（Miniflux 可能不可用），为避免误删已拒绝清理", status=409)
    n = rg.cleanup_orphans(tree=tree)
    return _ok(f"已清理 {n} 条失效归属" if n else "没有失效归属", cleaned=n)


@require_GET
def state(request):
    """只读快照：分组清单 + 失效归属数。给脚本 / e2e 断言用（比抓 DOM 稳）。"""
    return JsonResponse({"ok": True, "groups": rg.group_summary(),
                         "orphans": rg.orphan_count(tree=_tree())})
