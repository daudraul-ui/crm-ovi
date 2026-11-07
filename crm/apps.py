from django.apps import AppConfig


class CrmConfig(AppConfig):
    name = "crm"

    def ready(self):
        from . import integrity  # noqa: F401
