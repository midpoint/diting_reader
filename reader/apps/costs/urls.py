from django.urls import path

from . import views

urlpatterns = [
    path("costs", views.index, name="costs_index"),
    path("costs/feed/<int:feed_id>", views.feed_detail, name="costs_feed"),
    path("costs/budget", views.set_budget, name="costs_budget"),
]
