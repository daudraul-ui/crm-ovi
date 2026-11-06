from django.urls import include, path

from crm import views
from crm.api import router

urlpatterns = [
    path("health/", views.health),
    path("login/", views.sign_in, name="login"),
    path("logout/", views.sign_out, name="logout"),
    path("2fa/", views.two_factor, name="two_factor"),
    path("", views.dashboard, name="dashboard"),
    path("notifications/", views.notifications, name="notifications"),
    path("settings/", views.settings_view, name="settings"),
    path("settings/employees/new/", views.employee_create, name="employee_create"),
    path("stores/options/", views.store_options, name="store_options"),
    path("stores/format/new/", views.store_format_create, name="store_format_create"),
    path("stores/import/", views.import_stores, name="import_stores"),
    path("documents/<int:pk>/download/", views.download, name="download"),
    path("api/", include(router.urls)),
    path("<str:kind>/", views.listing, name="listing"),
    path("<str:kind>/new/", views.edit, name="create"),
    path("<str:kind>/<int:pk>/", views.detail, name="detail"),
    path("<str:kind>/<int:pk>/edit/", views.edit, name="edit"),
    path("<str:kind>/<int:pk>/delete/", views.delete, name="delete"),
]
