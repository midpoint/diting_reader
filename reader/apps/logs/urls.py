from django.urls import path

from . import views

urlpatterns = [
    path("logs", views.index, name="logs_index"),
    # ★ 这个端点是 htmx 定时轮询用的，已在 settings.LOG_IGNORE_PATHS 里排除，
    #   否则每几秒一条请求日志会把日志本身刷成流水。
    path("logs/rows", views.rows, name="logs_rows"),
]
