from django.urls import include, path

from crm import employees, excel_views, views
from crm.api import router
from workflow import views as workflow_views

urlpatterns = [
    path("api/workflow/<str:kind>/", workflow_views.api),
    path("api/workflow/<str:kind>/<int:pk>/", workflow_views.api),
    path("api/workflow/<str:kind>/<int:pk>/<str:action>/", workflow_views.api),
    path("workflow/", include("workflow.urls")),
    path("health/", views.health),
    path("login/", views.sign_in, name="login"),
    path("logout/", views.sign_out, name="logout"),
    path("2fa/", views.two_factor, name="two_factor"),
    path("", views.dashboard, name="dashboard"),
    path("notifications/", views.notifications, name="notifications"),
    path("settings/", views.settings_view, name="settings"),
    path("settings/employees/new/", views.employee_create, name="employee_create"),
    path("settings/employees/<int:pk>/", employees.employee_edit, name="employee_edit"),
    path("settings/employees/<int:pk>/totp/", employees.reset_totp, name="reset_totp"),
    path("settings/recovery/", employees.recovery_codes, name="recovery_codes"),
    path("stores/options/", views.store_options, name="store_options"),
    path("references/<str:kind>/", views.reference_options, name="reference_options"),
    path("stores/format/new/", views.store_format_create, name="store_format_create"),
    path("stores/import/", excel_views.import_excel, name="import_stores"),
    path("excel/import/<uuid:job_id>/", excel_views.import_excel, name="import_job"),
    path("excel/template/", excel_views.template, name="import_template"),
    path("excel/history/", excel_views.import_history, name="import_history"),
    path(
        "excel/import/<uuid:job_id>/source/",
        excel_views.import_source,
        name="import_source",
    ),
    path("excel/export/<str:kind>/", excel_views.export, name="export_excel"),
    path("documents/<int:pk>/download/", views.download, name="download"),
    path("api/", include(router.urls)),
    path("<str:kind>/", views.listing, name="listing"),
    path("<str:kind>/new/", views.edit, name="create"),
    path("<str:kind>/<int:pk>/", views.detail, name="detail"),
    path("<str:kind>/<int:pk>/edit/", views.edit, name="edit"),
    path("<str:kind>/<int:pk>/delete/", views.delete, name="delete"),
    path("<str:kind>/<int:pk>/archive/", views.archive, name="archive"),
]
