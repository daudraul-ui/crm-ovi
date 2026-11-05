import os

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from crm.models import Chain, Company, Project, Store, Task


class Command(BaseCommand):
    help = "Демонстрационные данные; пароль берётся из DEMO_PASSWORD"

    def handle(self, *args, **kwargs):
        password = os.getenv("DEMO_PASSWORD")
        if not password or len(password) < 12:
            raise CommandError("Укажите DEMO_PASSWORD не короче 12 символов")
        call_command("bootstrap")
        user, created = User.objects.get_or_create(
            username="demo", defaults={"first_name": "Демо"}
        )
        if created:
            user.set_password(password)
            user.save()
            user.groups.add(Group.objects.get(name="Директор"))
        company, _ = Company.objects.get_or_create(
            name="Север Ритейл", defaults={"inn": "7701234567", "manager": user}
        )
        chain, _ = Chain.objects.get_or_create(name="Север")
        store, _ = Store.objects.get_or_create(
            chain=chain,
            code="MSK-001",
            defaults={"name": "Север • Москва", "address": "Москва, ул. Примерная, 1"},
        )
        project, _ = Project.objects.get_or_create(
            name="Остекление холодильной линии",
            defaults={"company": company, "manager": user, "status": "active"},
        )
        project.stores.add(store)
        Task.objects.get_or_create(
            name="Уточнить размеры витрин",
            defaults={
                "project": project,
                "assignee": user,
                "due_at": timezone.now() + timezone.timedelta(days=2),
            },
        )
        self.stdout.write("Создан demo; при первом входе настройте TOTP")
