from django.urls import path

from . import views

urlpatterns = [
    path("summary", views.index, name="summary_index"),
    path("summary/run", views.run_day, name="summary_run"),
    path("summary/catchup", views.catch_up, name="summary_catchup"),
    path("summary/weekly", views.weekly_run, name="weekly_run"),
    path("summary/weekly/<str:week_end>", views.weekly_detail,
         name="weekly_detail"),
    path("summary/<str:day>", views.detail, name="summary_detail"),
]
