from django.urls import path

from . import treeviews, views

urlpatterns = [
    path("", views.index, name="index"),
    path("hx/entries", views.hx_entries, name="hx_entries"),
    path("hx/tree", views.hx_tree, name="hx_tree"),
    path("hx/entry/<int:entry_id>", views.hx_entry_detail, name="hx_entry_detail"),
    # 抓取原文全文（fetch original content）：同步打源站，覆盖前二次确认
    path("hx/fetch-full/<int:entry_id>", views.hx_fetch_full, name="hx_fetch_full"),
    # 深链窗口外补行（GET，无副作用）：列表只有最近 ENTRY_WINDOW 条，
    # 通知点名旧文时前端靠它把那一行取出来插进列表再展开。
    path("hx/row/<int:entry_id>", views.hx_row, name="hx_row"),
    path("hx/mark-read", views.hx_mark_read, name="hx_mark_read"),
    # 单条退回未读（文章右键；与批量标已读不同，带 entry_id，写 Miniflux）
    path("hx/mark-unread/<int:entry_id>", views.hx_mark_unread, name="hx_mark_unread"),
    # ★ 立即刷新：POST 启动批次（后台线程），GET 查状态（前端轮询）
    path("hx/refresh", views.hx_refresh, name="hx_refresh"),
    path("hx/refresh/status", views.hx_refresh_status, name="hx_refresh_status"),
    path("hx/star/<int:entry_id>", views.hx_star, name="hx_star"),
    path("icon/<int:feed_id>", views.icon, name="icon"),
    # ★ 树的写操作（分类新增/改名、feed 新增、拖动移动）
    path("tree/category/create", treeviews.category_create, name="tree_category_create"),
    path("tree/category/rename", treeviews.category_rename, name="tree_category_rename"),
    path("tree/category/delete", treeviews.category_delete, name="tree_category_delete"),
    path("tree/feed/create", treeviews.feed_create, name="tree_feed_create"),
    path("tree/feed/rename", treeviews.feed_rename, name="tree_feed_rename"),
    path("tree/feed/delete", treeviews.feed_delete, name="tree_feed_delete"),
    path("tree/feed/move", treeviews.feed_move, name="tree_feed_move"),
    # ★ 拖动重排（分类 / 目录 / feed 三层同一套：整层顺序落库）
    path("tree/reorder", treeviews.tree_reorder, name="tree_reorder"),
    path("tree/order/reset", treeviews.tree_order_reset, name="tree_order_reset"),
]
