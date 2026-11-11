import os

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from crm.models import Chain, Company, Project, Store, Task


class Command(BaseCommand):
    help = "Демонстрационные данные; пароль берётся из DEMO_PASSWORD"

    @transaction.atomic
    def handle(self, *args, **kwargs):
        password = os.getenv("DEMO_PASSWORD")
        if not password or len(password) < 12:
            raise CommandError("Укажите DEMO_PASSWORD не короче 12 символов")
        call_command("bootstrap")
        user, created = User.objects.get_or_create(
            username="demo", defaults={"first_name": "Демо"}
        )
        if not created:
            self.stdout.write(
                "Пользователь demo уже существует. Существующие объекты не изменены."
            )
            return
        if created:
            user.set_password(password)
            user.save()
            user.groups.add(Group.objects.get(name="Директор"))
        company = Company.objects.create(name="Демо — Север Ритейл", manager=user)
        chain = Chain.objects.create(name="Демо — Север")
        chain.companies.add(company)
        store = Store.objects.create(
            chain=chain,
            code="MSK-001",
            name="Север • Москва",
            address="Москва, ул. Примерная, 1",
            company=company,
        )
        project = Project.objects.create(
            name="Остекление холодильной линии",
            company=company,
            manager=user,
            status="active",
        )
        project.stores.add(store)
        Task.objects.create(
            name="Уточнить размеры витрин",
            project=project,
            assignee=user,
            due_at=timezone.now() + timezone.timedelta(days=2),
        )
        self.stdout.write("Создан demo; при первом входе настройте TOTP")
