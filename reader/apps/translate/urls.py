from django.urls import path

from . import views

urlpatterns = [
    path("translate/feed", views.set_feed, name="translate_feed"),
    path("translate/entry", views.retranslate, name="translate_entry"),
    path("translate/status", views.status, name="translate_status"),
]
