from django.urls import path

from . import views

urlpatterns = [
    path("groups/create", views.create, name="groups_create"),
    path("groups/rename", views.rename, name="groups_rename"),
    path("groups/delete", views.delete, name="groups_delete"),
    path("groups/move", views.move, name="groups_move"),
    path("groups/cleanup", views.cleanup, name="groups_cleanup"),
    path("groups/state", views.state, name="groups_state"),
]
