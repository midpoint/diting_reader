"""零成本验证「虚拟分组层」（P0：右键路径，暂不做拖动）。

用法：python scripts/verify_groups.py

覆盖：
  A. 源码契约：模板 / 静态资源 / JS 行为（不依赖网络，也不依赖 Miniflux）
  B. 装配层：`decorate()` 的**回归保证**（`cat.feeds` 全量不变、未读数不变）、
     孤儿成员不渲染、空组仍在、幂等
  C. 组内 feed id：跨分类残留被过滤
  D. ★ 零污染：往树里注入真分组后，`summary._meta_index()` / `costs._tree_maps()`
     的返回值与不注入时**完全一致**（证明分组进不了总结与开销）
  E. 视图层：`sel=group:N` 走多源归并；分组范围标记已读**不会退化成全量**
  F. 多 feed 归并的口径（每源 N 条 → 全局排序 → 取前 limit）

★ 本脚本只在**假分类 id 999901**下写测试数据，结束时删干净，不碰真实分组。
"""
import inspect
import os
import sys
from pathlib import Path

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "apps"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402
django.setup()

import _dbtest  # noqa: E402

# ★ 分组是用户手建的结构，脚本造的临时组绝不能落进真实库（见 _dbtest.py）。
_dbtest.isolate()

from datetime import datetime, timedelta, timezone as dt_tz  # noqa: E402

from django.test import RequestFactory  # noqa: E402

import core.summary as sm  # noqa: E402
import costs.views as cv  # noqa: E402
import reader.views as rv  # noqa: E402
from core import readergroups as rg  # noqa: E402
from core.datasource.base import CategoryNode, Entry, FeedNode  # noqa: E402
from core.models import FeedGroup, FeedGroupMember  # noqa: E402

ok = bad = 0
fails = []


def check(name, cond, detail=""):
    global ok, bad
    if cond:
        ok += 1
        print("  PASS  %s" % name)
    else:
        bad += 1
        fails.append(name)
        print("  FAIL  %s   %s" % (name, detail))


def read(rel):
    with open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


TREE_BODY = read("templates/reader/_tree_body.html")
TREE_FEED = read("templates/reader/_tree_feed.html")
ROWS_HTML = read("templates/reader/_rows.html")
CSS = read("static/app.css")
JS = read("static/app.js")
RG_SRC = inspect.getsource(rg)

CAT = 999901           # 假分类（不碰真数据）
TMP_GROUP = "验证用临时组"
# ★ 本脚本只允许用这些**假 feed id** 造数据（真源的 id 都很小）。
TEST_FEED_IDS = (900001, 900002, 900003, 900009)


def db_members():
    """真库里已有的分组归属（**不含测试假 feed**）。

    ★★ 2026-10-16 事故留下的教训：本脚本曾在真库上调用
      `cleanup_orphans(tree=fake_tree())`，而 fake_tree 里没有用户的真实源，
      于是用户的全部归属都被判为"孤儿"删光（连删两次）。
      现在用这个快照做**零改动守卫**：脚本跑完后真库必须一字不差。
    """
    return {(f, g) for f, g in FeedGroupMember.objects.values_list("feed_id", "group_id")
            if f not in TEST_FEED_IDS}


MEMBERS_AT_START = db_members()

# ★ 本脚本造的是假数据，快照不能写进真备份目录（否则 data/backups 里会混进
#   "900001 → 假组"这种条目，将来误恢复会把测试残留带回来）。重定向到临时目录。
import tempfile  # noqa: E402

from core import groupbackup  # noqa: E402

_TMP_BK = Path(tempfile.mkdtemp(prefix="verify-groups-bk-"))
groupbackup.backup_dir = lambda: _TMP_BK


# ------------------------------------------------------------------ 假树
def fake_tree():
    f1 = FeedNode(id=900001, title="假源甲", site_url="", category_id=CAT,
                  category_title="假分类", unread=3)
    f2 = FeedNode(id=900002, title="假源乙", site_url="", category_id=CAT,
                  category_title="假分类", unread=0)
    f3 = FeedNode(id=900003, title="真分类里的源", site_url="", category_id=2,
                  category_title="投资", unread=1)
    c1 = CategoryNode(id=CAT, title="假分类")
    c1.feeds = [f1, f2]
    c2 = CategoryNode(id=2, title="投资")
    c2.feeds = [f3]
    return [c2, c1]


def snapshot(tree):
    """树的"统计等价类"：每个分类的 feed id 集合与未读数。"""
    return {c.id: (sorted(f.id for f in c.feeds), c.unread) for c in tree}


print("\n== A. 源码契约 ==")
check("_tree_body.html 渲染 cat.groups", "{% for g in cat.groups %}" in TREE_BODY)
check("_tree_body.html 分组用 grp-<id> 做折叠 key", 'data-toggle="grp-{{ g.id }}"' in TREE_BODY)
check("_tree_body.html 分组可点击（sel=group:N）",
      'sel=group:{{ g.id }}' in TREE_BODY)
check("_tree_body.html 未分组的 feed 才裸挂在分类下（切给 cat.root_feeds）",
      "{% for f in cat.root_feeds %}" in TREE_BODY
      and "{% for f in cat.feeds %}" not in TREE_BODY)
check("★ 「未分组」筛选只有一处实现（core/treeorder.root_feeds）",
      "def root_feeds(cat):" in read("core/treeorder.py"))
check("分组里的 feed 与分类下共用同一段模板",
      TREE_BODY.count('{% include "reader/_tree_feed.html" %}') == 2)
check("_tree_feed.html 带 data-feed / data-group（右键菜单用）",
      'data-feed="{{ f.id }}"' in TREE_FEED and 'data-group="{{ f.group_id }}"' in TREE_FEED)
check("空分组仍有可见行", "空分组" in TREE_BODY)
check("多源视图在列表底部标注口径", "多源归并口径" in ROWS_HTML)
check("CSS 有分组行样式且缩进小于分类级",
      ".node.group > .node-row > a" in CSS and ".children.grp { padding-left: 10px; }" in CSS)

check("JS 有建组 / 改名 / 删组 / 移入四个动作",
      all(k in JS for k in ('"grp-create"', '"grp-rename"', '"grp-delete"', '"grp-move"')))
# ★ 取**整个分组菜单分支**（到下一语句为止），不按固定字符数切片 ——
#   旧写法钉死 420 字符窗口，菜单加了「立即刷新本组 / 添加 feed」两项后
#   grp-rename 被推出窗口，断言假红（行为其实没变）。
_i = JS.index("if (isGroup) {")
_seg = JS[_i:JS.index("var items =", _i)]
check("★ 分组右键**不给**分级配置入口（分组没有判据）",
      '"config"' not in _seg and "grp-rename" in _seg)
check("右键菜单用 textContent 拼装（组名不会被当 HTML 解析）",
      "b.textContent = it.label" in JS and "menu.innerHTML = items.map" not in JS)
# ★ 拖动已落地（原断言「JS 里没有 draggable」钉的是 P0 不做拖动的旧决策，
#   后来做进来了）：模板可拖 + JS 处理拖放；跨分类的双闸（前端预检 + 后端 409）
#   的**行为**断言在 core/tests/test_treeviews_move.py，这里只钉机制存在。
check("拖动机制在：模板可拖 + JS 处理 dragstart/drop",
      'draggable="true"' in TREE_FEED and "dragstart" in JS and "drop" in JS)
# ★ 拖动重排（2026-11-24）：三层都能拖，但**只有手柄**能起拖分类 / 目录行。
#   钉的是机制（POST 整层、撤销入口按 data-ord 出现），行为断言在
#   core/tests/test_treeorder.py。
check("重排接口在：/tree/reorder + /tree/order/reset",
      '"/tree/reorder"' in JS and '"/tree/order/reset"' in JS
      and "tree/reorder" in read("apps/reader/urls.py")
      and "tree/order/reset" in read("apps/reader/urls.py"))
_li_cat = TREE_BODY.split('<li class="node cat')[1].split(">")[0]
_li_grp = TREE_BODY.split('<li class="node group')[1].split(">")[0]
check("★ 分类行 / 目录行本身不可拖（只有 .grip 带 draggable，防误排一整层）",
      "draggable" not in _li_cat and "draggable" not in _li_grp
      and TREE_BODY.count('class="grip" draggable="true"') == 2)
check("每层带 data-ord（排过序才给撤销项）",
      'data-ord="{% if cat.layer_ordered %}' in TREE_BODY
      and 'data-ord="{% if g.layer_ordered %}' in TREE_BODY
      and 'data-ord="{% if f.layer_ordered %}' in TREE_FEED)
check("「恢复默认顺序」三项共用一个入口，且不排就整个不出现",
      JS.count("pushOrderReset(") == 4 and 'getAttribute("data-ord") !== "1"' in JS)
check("重排是「整层」送后端，不是「从第几挪到第几」",
      'layerIds(ul, kind).join(",")' in JS)
check("★ 无分组的分类下，feed 右键不加空提示（不留 noop 占位项）",
      "还没有分组" not in JS and '"noop"' not in JS)
check("★ 装配层不做跨分类写（无 Miniflux update）",
      "update_feed" not in RG_SRC and "/v1/feeds" not in RG_SRC)

print("\n== B. 装配层（假树 + 真 DB）==")
before = snapshot(fake_tree())
FeedGroup.objects.filter(category_id=CAT).delete()
g = FeedGroup.objects.create(category_id=CAT, title=TMP_GROUP, sort=1)
FeedGroupMember.objects.update_or_create(feed_id=900001, defaults={"group_id": g.id})
FeedGroupMember.objects.update_or_create(feed_id=900003, defaults={"group_id": g.id})  # 跨分类残留
# 900009 **不在** fake_tree 里 ⇒ 属于「feed 不在树里」，默认必须保留（fail-safe）
FeedGroupMember.objects.update_or_create(feed_id=900009, defaults={"group_id": g.id})
try:
    tree = rg.decorate(fake_tree())
    after = snapshot(tree)
    check("★ 回归：decorate 后 cat.feeds 与未读数**完全不变**", before == after,
          f"{before} != {after}")
    cat = [c for c in tree if c.id == CAT][0]
    check("分类下挂出了 1 个分组", len(cat.groups) == 1)
    check("组内只有同分类的有效成员", [f.id for f in cat.groups[0].feeds] == [900001])
    check("组未读数 = 组内 feed 之和", cat.groups[0].unread == 3)
    check("分组内的 feed 被标了 group_id",
          [f.group_id for f in cat.feeds if f.id == 900001] == [g.id])
    check("跨分类残留的 feed 不渲染（group_id 保持 0）",
          [f.group_id for f in cat.feeds if f.id == 900002] == [0])

    other = [c for c in tree if c.id == 2][0]
    # ★ 不能断言 other.groups == []：脚本跑在真库上，投资分类本来就可能
    #   有真实分组（2026-10-24 实测被「其他」这个真分组弄红过）。钉的是
    #   **本脚本的测试组不泄漏**到别的分类 + 残留源的 group_id 保持 0。
    check("跨分类残留不会挂到别的分类下",
          all(gv.id != g.id for gv in other.groups) and
          [f.group_id for f in other.feeds if f.id == 900003] == [0])

    check("decorate 幂等", snapshot(rg.decorate(rg.decorate(fake_tree()))) == after)

    # 空组
    g2 = FeedGroup.objects.create(category_id=CAT, title="空的组", sort=2)
    tree2 = rg.decorate(fake_tree())
    cat2 = [c for c in tree2 if c.id == CAT][0]
    check("空分组仍然出现在树上（否则没法往里拖）",
          len(cat2.groups) == 2 and cat2.groups[1].feeds == [] and cat2.groups[1].unread == 0)

    print("\n== C. 组内 id 与移动 ==")
    check("group_feed_ids 过滤跨分类残留",
          rg.group_feed_ids(g.id, tree=fake_tree()) == [900001])
    check("移出分组（group_id 空）只删成员",
          rg.move_feed(900001, None, tree=fake_tree()) is None and
          not FeedGroupMember.objects.filter(feed_id=900001).exists())
    rg.move_feed(900001, g.id, tree=fake_tree())
    check("移回分组成功", rg.group_feed_ids(g.id, tree=fake_tree()) == [900001])
    try:
        rg.move_feed(900003, g.id, tree=fake_tree())      # 900003 在真分类 2 里
        check("★ 跨分类移动被拒绝", False, "没有抛错")
    except ValueError as exc:
        check("★ 跨分类移动被拒绝", "本分类内" in str(exc), str(exc))
    try:
        rg.move_feed(999999, None, tree=fake_tree())
        check("不在树里的 feed 被拒绝", False, "没有抛错")
    except ValueError:
        check("不在树里的 feed 被拒绝", True)
    # ★ 这三条是 2026-10-16 事故的回归防线（当时 cleanup 把真实成员当孤儿全删了）
    check("★ cleanup_orphans 空树下拒绝清理（树不完整时宁可不动手）",
          rg.cleanup_orphans(tree=[], scope_group_ids=[g.id]) == 0)
    check("★ feed 不在树里时默认保留（不把『树不完整』当孤儿误删）",
          rg.cleanup_orphans(tree=fake_tree(), scope_group_ids=[g.id]) == 1
          and FeedGroupMember.objects.filter(feed_id=900009).exists())
    check("清理后没有孤儿（口径与 cleanup_orphans 一致）",
          rg.orphan_count(tree=fake_tree(), scope_group_ids=[g.id]) == 0)
    check("并发/重复清理是幂等的",
          rg.cleanup_orphans(tree=fake_tree(), scope_group_ids=[g.id]) == 0)
    check("删组不删 feed（成员关系被清掉）",
          rg.delete_group(g2.id) == 1 and
          not FeedGroupMember.objects.filter(group_id=g2.id).exists())

    print("\n== D. ★ 零污染：总结 / 开销拿到的树不受分组影响 ==")

    class FakeDS:
        """只实现下游用到的 list_tree()，返回**带分组**的真树。"""

        def __init__(self, tree):
            self.tree = tree

        def list_tree(self):
            return rg.decorate(self.tree)

    plain = FakeDS(fake_tree())
    sm.get_datasource = lambda: plain
    cv.get_datasource = lambda: plain
    meta_plain, tree_plain = sm._meta_index()
    maps_plain = cv._tree_maps()

    rg.decorate(fake_tree())              # 注入分组（member 已在库里）
    withgrp = FakeDS(fake_tree())
    sm.get_datasource = lambda: withgrp
    cv.get_datasource = lambda: withgrp
    meta_grp, tree_grp = sm._meta_index()
    maps_grp = cv._tree_maps()

    check("summary._meta_index 的 feed 元数据一字不差", meta_plain == meta_grp)
    check("summary 拿到的树，分类未读数一致",
          snapshot(tree_plain) == snapshot(tree_grp))
    check("costs._tree_maps 的 (feed→title, cat→title, feed→cat) 一字不差",
          maps_plain == maps_grp)
    for mod_name, fn in (("summary._meta_index", sm._meta_index),
                         ("costs._tree_maps", cv._tree_maps)):
        check(f"{mod_name} 源码里根本不出现 groups",
              "groups" not in inspect.getsource(fn))

    print("\n== E. 视图层：范围解析 / 多源归并 / 标记已读 ==")
    check("_scope 认 group:N", rv._scope("group:77") == ("group", 77))
    check("_scope 旧的 cat/feed 不受影响",
          rv._scope("cat:1") == ("cat", 1) and rv._scope("feed:2") == ("feed", 2))
    check("_title_for 用组名做工具栏标题",
          rv._title_for("group:%d" % g.id, rg.decorate(fake_tree())) == TMP_GROUP)

    class RecDS(FakeDS):
        def __init__(self, tree):
            super().__init__(tree)
            self.calls = []

        def list_entries(self, **kw):
            self.calls.append(("single", kw))
            return []

        def list_entries_multi(self, feed_ids, **kw):
            self.calls.append(("multi", list(feed_ids), kw))
            return []

        def iter_unread(self, **kw):
            self.calls.append(("iter", kw))
            return iter([])

        def mark_scope_read(self, **kw):
            self.calls.append(("scope", kw))
            return -1

        def mark_read_before(self, **kw):
            self.calls.append(("before", kw))
            return 0

    rf = RequestFactory()
    rec = RecDS(fake_tree())
    rv.get_datasource = lambda: rec
    req = rf.get("/hx/entries", {"sel": "group:%d" % g.id})
    ctx = rv._entries_context(req, rv._filters(req))
    kinds = [c[0] for c in rec.calls]
    check("分组范围走 list_entries_multi", "multi" in kinds and "single" not in kinds)
    check("传给多源查询的是**有效**组内 id",
          [c for c in rec.calls if c[0] == "multi"][0][1] == [900001])
    check("分组视图不会被当成单源视图（要显示来源列）", ctx["single_feed"] is False)
    check("分组视图标注了多源口径", ctx["multi_source"] is True and ctx["per_source"] > 0)

    rec.calls = []
    req2 = rf.post("/hx/mark-read", {"sel": "group:%d" % g.id, "days": "0"})
    rv.hx_mark_read(req2)
    check("★ 分组范围标记已读：逐源调用，绝不退化成标记全部",
          [c for c in rec.calls if c[0] == "scope"] and
          all(c[1]["feed_id"] == 900001 for c in rec.calls if c[0] == "scope") and
          not [c for c in rec.calls if c[0] == "iter"])

    rec.calls = []
    req3 = rf.post("/hx/mark-read", {"sel": "group:%d" % g.id, "days": "7"})
    rv.hx_mark_read(req3)
    check("分组范围按天标记：也是逐源",
          [c for c in rec.calls if c[0] == "before"] and
          all(c[1]["feed_id"] == 900001 for c in rec.calls if c[0] == "before"))

    print("\n== F. 多 feed 归并口径 ==")
    from core.datasource.miniflux import MinifluxDataSource

    ds = MinifluxDataSource.__new__(MinifluxDataSource)   # 不碰网络，只测归并
    now = datetime(2026, 10, 16, 12, 0, tzinfo=dt_tz.utc)
    pool = {
        1: [Entry(id=1, feed_id=1, feed_title="", title="a",
                  url="", published_at=now - timedelta(hours=1), created_at=None)],
        2: [Entry(id=2, feed_id=2, feed_title="", title="b",
                  url="", published_at=now - timedelta(minutes=5), created_at=None),
            Entry(id=3, feed_id=2, feed_title="", title="c",
                  url="", published_at=None, created_at=None)],
    }
    ds.list_entries = lambda **kw: list(pool.get(int(kw.get("feed_id") or 0), []))
    merged = ds.list_entries_multi([1, 2], limit=10)
    check("归并按 published_at 倒序", [e.id for e in merged] == [2, 1, 3],
          str([e.id for e in merged]))
    check("空 feed 列表直接返回空", ds.list_entries_multi([], limit=10) == [])
    check("limit 生效", len(ds.list_entries_multi([1, 2], limit=2)) == 2)
    check("published_at 为空的条目不会让排序崩（排在最后）",
          merged[-1].id == 3)
finally:
    FeedGroupMember.objects.filter(feed_id__in=TEST_FEED_IDS).delete()
    FeedGroup.objects.filter(category_id=CAT).delete()
    after = db_members()
    check("★★ 守卫：全程没动真库中已有的任何分组归属（零改动）",
          after == MEMBERS_AT_START,
          f"开始 {len(MEMBERS_AT_START)} 条 → 结束 {len(after)} 条；"
          f"少了 {sorted(MEMBERS_AT_START - after)}")
    print("\n（测试数据已清理）")

print("\n== 汇总 ==")
print("PASS %d / FAIL %d" % (ok, bad))
if fails:
    print("失败项：")
    for f in fails:
        print("  -", f)
sys.exit(1 if bad else 0)
