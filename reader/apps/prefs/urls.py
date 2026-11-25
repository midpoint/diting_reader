from django.urls import path

from . import views

urlpatterns = [
    path("prefs", views.index, name="prefs_index"),
    path("prefs/save", views.save, name="prefs_save"),
    path("prefs/reset", views.reset, name="prefs_reset"),
    # .env 配置（数据源 / 模型 / 运行参数）：直接读写仓库根 .env
    path("prefs/env", views.save_env, name="prefs_env_save"),
    path("prefs/env/test", views.test_datasource, name="prefs_env_test"),
    path("prefs/env/raw", views.env_raw, name="prefs_env_raw"),
    path("prefs/env/raw/save", views.save_env_raw, name="prefs_env_raw_save"),
    # 全局层文件（画像 / 本体）：整篇 markdown，不是键值，单独端点
    path("prefs/file", views.save_file, name="prefs_file"),
]
