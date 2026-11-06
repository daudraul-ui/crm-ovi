import getpass

from django.contrib.auth.models import Group, User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from crm.models import AuditEvent


class Command(BaseCommand):
    help = "Создать персональный аккаунт и назначить несколько ролей (пароль вводится скрыто)"

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--email", required=True)
        parser.add_argument("--role", action="append", required=True)

    def handle(self, *args, **options):
        roles = list(Group.objects.filter(name__in=options["role"]))
        if len(roles) != len(set(options["role"])):
            raise CommandError("Неизвестная роль; выполните bootstrap")
        password = getpass.getpass("Пароль: ")
        try:
            validate_password(password)
        except ValidationError as exc:
            raise CommandError(str(exc))
        if getpass.getpass("Повторите пароль: ") != password:
            raise CommandError("Пароли не совпадают")
        with transaction.atomic():
            if User.objects.filter(username=options["username"]).exists():
                raise CommandError("Аккаунт уже существует")
            user = User.objects.create_user(
                options["username"], email=options["email"], password=password
            )
            user.groups.set(roles)
            AuditEvent.objects.create(
                action="account_created_cli",
                model="auth.User",
                object_id=str(user.pk),
                after={"roles": options["role"]},
            )
        self.stdout.write("Аккаунт создан; TOTP настраивается при первом входе")
