from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="workflow-dashboard"),
    path("api/<str:kind>/", views.api),
    path("api/<str:kind>/<int:pk>/", views.api),
    path("api/<str:kind>/<int:pk>/<str:action>/", views.api),
    path(
        "quotes/<int:pk>/export/<str:format>/",
        views.export_quote,
        name="workflow-export",
    ),
    path("<str:kind>/", views.listing, name="workflow-list"),
    path("<str:kind>/new/", views.edit, name="workflow-create"),
    path("<str:kind>/<int:pk>/", views.detail, name="workflow-detail"),
    path("<str:kind>/<int:pk>/edit/", views.edit, name="workflow-edit"),
    path("<str:kind>/<int:pk>/remove/", views.remove, name="workflow-remove"),
    path(
        "<str:kind>/<int:pk>/do/<str:action>/", views.execute, name="workflow-command"
    ),
]
