from django.urls import include, path

urlpatterns = [
    path("", include("reader.urls")),
    path("", include("triage.urls")),
    path("", include("costs.urls")),
    path("", include("summary.urls")),
    # ★ 虚拟分组（阅读页树的中间层）——只改本地表，不碰 Miniflux
    path("", include("groups.urls")),
    # ★ 标题翻译（只改本地表 + 调 LLM，不写 Miniflux）
    path("", include("translate.urls")),
    # ★ 日志查看页 —— 纯只读 data/logs/*.log
    path("", include("logs.urls")),
    # ★ 设置页 —— 渲染 settingkit 注册表；写入 AppSetting，读取点全部走 sk.effective
    path("", include("prefs.urls")),
    # ★ 知识库页 —— 草稿审批（Gate 1）+ 已入库页浏览；文件读写在 core/wiki.py
    path("", include("wiki.urls")),
]
