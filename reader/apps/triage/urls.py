from django.urls import path

from . import views

urlpatterns = [
    path("triage/config", views.config_form, name="triage_config"),
    path("triage/config/save", views.config_save, name="triage_config_save"),
    path("triage/prompt", views.prompt_view, name="triage_prompt"),
    path("triage/prompt/rebuild", views.prompt_rebuild, name="triage_prompt_rebuild"),
    path("triage/level", views.set_level, name="triage_level"),
    path("triage/rejudge", views.rejudge, name="triage_rejudge"),
    # ★ 分级不再有自己的页面：状态以 JSON 暴露（脚本/排查用），列表页靠徽标表达
    path("triage/status", views.status, name="triage_status"),
    path("triage/rate-now", views.rate_now, name="triage_rate_now"),
    path("triage/run", views.run_now, name="triage_run"),
    path("triage/rejudge-failed", views.rejudge_failed, name="triage_rejudge_failed"),
    path("triage/fastrefresh", views.fast_refresh, name="triage_fastrefresh"),
]
