from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Создать роли; безопасно повторять"

    def handle(self, *args, **kwargs):
        for name in ["Директор", "РОП", "Менеджер", "Инженер", "Администратор"]:
            group, _ = Group.objects.get_or_create(name=name)
            perms = Permission.objects.filter(content_type__app_label="crm")
            if name in ("Менеджер", "РОП"):
                perms = perms.exclude(codename="view_finance").exclude(
                    content_type__model="auditevent"
                )
                if name == "Менеджер":
                    perms = perms.exclude(codename__startswith="delete_")
            elif name == "Инженер":
                perms = perms.filter(
                    content_type__model__in=[
                        "project",
                        "store",
                        "chain",
                        "region",
                        "storeformat",
                        "task",
                        "document",
                        "comment",
                    ]
                ).exclude(codename__startswith="delete_")
                perms = (
                    perms.filter(codename__startswith="view_")
                    | perms.filter(
                        content_type__model__in=["task", "comment"],
                        codename__startswith="change_",
                    )
                    | perms.filter(
                        content_type__model="comment", codename="add_comment"
                    )
                )
            elif name == "Администратор":
                perms = perms.exclude(codename="view_finance")
            group.permissions.set(perms)
        self.stdout.write(
            "Роли обновлены. Роли назначаются через Django shell; обычные пользователи не управляют правами."
        )
