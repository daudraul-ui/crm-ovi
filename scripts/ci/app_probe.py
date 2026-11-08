"""Executed inside the CI web container. Never run against production data."""

import base64
import hashlib
import io
import json
import os
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()
from django.contrib.auth.models import Group, User
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice

from crm.models import (
    Chain,
    Company,
    Document,
    Notification,
    Project,
    Region,
    StoreFormat,
    Task,
)
from crm.tasks import notify_overdue

if os.getenv("CRM_COMPOSE_SMOKE") != "1":
    raise RuntimeError("CI probe requires CRM_COMPOSE_SMOKE=1")

CONTENT = b"CRM OVI isolated Compose backup/media round-trip\n"
MARKER = "compose-ci-roundtrip"


def check_data():
    user = User.objects.get(username=MARKER)
    company = Company.objects.get(name=MARKER, manager=user)
    project = Project.objects.get(name=MARKER, company=company, manager=user)
    doc = Document.objects.get(name=MARKER, company=company, project=project)
    with doc.file.open("rb") as file:
        assert file.read() == CONTENT, "Restored media differs"
    assert user.groups.filter(name="Директор").exists()
    assert TOTPDevice.objects.filter(user=user, confirmed=True).exists()
    assert Notification.objects.filter(user=user).count() >= 2
    assert Task.objects.filter(assignee=user, project=project).count() == 2
    assert not MigrationExecutor(connection).migration_plan(
        MigrationExecutor(connection).loader.graph.leaf_nodes()
    ), "Unapplied migrations"
    return user, doc


def seed():
    assert not User.objects.filter(username=MARKER).exists(), (
        "CI database must be fresh"
    )
    user = User.objects.create_user(MARKER, password=secrets.token_urlsafe(32))
    user.groups.add(Group.objects.get(name="Директор"))
    TOTPDevice.objects.create(user=user, confirmed=True)
    company = Company.objects.create(name=MARKER, manager=user, inn="7701234567")
    project = Project.objects.create(name=MARKER, company=company, manager=user)
    doc = Document(name=MARKER, company=company, project=project, uploaded_by=user)
    doc.file.save("roundtrip.txt", ContentFile(CONTENT), save=True)
    # Include nested and binary files not attached to model records in media backup.
    extra = Path(doc.file.storage.location) / "ci-extra" / "nested.bin"
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_bytes(bytes(range(256)))
    Task.objects.create(
        name=MARKER + "-worker",
        project=project,
        assignee=user,
        due_at=timezone.now() - timezone.timedelta(minutes=1),
    )
    cache.set(MARKER, "redis-ok", 60)
    assert cache.get(MARKER) == "redis-ok"
    result = notify_overdue.delay()
    result.get(timeout=60)
    assert Notification.objects.filter(user=user).exists(), (
        "Worker did not execute task"
    )
    Task.objects.create(
        name=MARKER + "-beat",
        project=project,
        assignee=user,
        due_at=timezone.now() - timezone.timedelta(minutes=1),
    )
    # Do NOT enqueue this task: only Beat can cause the second notification.
    deadline = time.monotonic() + 90
    while Notification.objects.filter(user=user).count() < 2:
        if time.monotonic() >= deadline:
            raise AssertionError(
                "Beat did not schedule overdue notification within 90s"
            )
        time.sleep(1)
    check_data()
    print(
        "PASS migrations, PostgreSQL, Redis cache, Worker/result backend, Beat scheduling, media seed"
    )


def auth():
    user, doc = check_data()
    client = Client()
    client.force_login(user)
    session = client.session
    session["otp_device_id"] = user.totpdevice_set.get(confirmed=True).persistent_id
    session.save()
    # Output captured in runner memory only; never write credentials to artifacts.
    print(
        json.dumps(
            {
                "session": session.session_key,
                "document": doc.pk,
                "sha256": hashlib.sha256(CONTENT).hexdigest(),
                "file": doc.file.name,
            }
        )
    )


def manifest():
    from django.conf import settings

    check_data()
    root = Path(settings.MEDIA_ROOT)
    data = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }
    assert len(data) >= 2, (
        f"Expected fixture files and protected import sources, got {len(data)}"
    )
    from django.apps import apps

    workflow_rows = {}
    for model in apps.get_app_config("workflow").get_models():
        rows = []
        for row in model.objects.order_by("pk").values():
            rows.append(
                {
                    k: hashlib.sha256(bytes(v)).hexdigest()
                    if isinstance(v, (bytes, memoryview))
                    else str(v)
                    for k, v in row.items()
                }
            )
        workflow_rows[model._meta.label] = rows
    data["__workflow_database_sha256__"] = hashlib.sha256(
        json.dumps(workflow_rows, sort_keys=True).encode()
    ).hexdigest()
    print(json.dumps(data, sort_keys=True))


def browser_fixtures():
    user, _ = check_data()
    suffix = secrets.token_hex(4)
    a = Company.objects.create(name="ЮЛ A " + suffix, manager=user)
    b = Company.objects.create(name="ЮЛ B " + suffix, manager=user)
    chain = Chain.objects.create(name="Другая сеть " + suffix)
    chain.companies.add(a)
    fmt = StoreFormat.objects.create(name="Формат другой сети " + suffix, chain=chain)
    region = Region.objects.create(name="Регион UI " + suffix)
    client = Client()
    client.force_login(user)
    session = client.session
    session["otp_device_id"] = user.totpdevice_set.get(confirmed=True).persistent_id
    session.save()
    from openpyxl import Workbook

    book = Workbook()
    book.active.append(
        [
            "Торговая сеть",
            "Клиент",
            "Менеджер",
            "Номер (КОД)",
            "Название",
            "Адрес",
            "Регион",
            "Формат",
        ]
    )
    book.active.append(
        [
            "Импорт сеть " + suffix,
            "Импорт ЮЛ " + suffix,
            user.username,
            "IMPORT-00001",
            "Импорт магазин",
            "Москва",
            "Москва импорт",
            "Импорт формат",
        ]
    )
    stream = io.BytesIO()
    book.save(stream)
    print(
        json.dumps(
            {
                "session": session.session_key,
                "suffix": suffix,
                "company_a": a.pk,
                "company_b": b.pk,
                "company_b_name": b.name,
                "other_chain": chain.pk,
                "other_format_name": fmt.name,
                "region": region.pk,
                "import_xlsx": base64.b64encode(stream.getvalue()).decode(),
            }
        )
    )


def workflow_integration():
    from datetime import time as clock_time

    from workflow.models import CalendarDay, Incident, SupplierOrder
    from workflow.tasks import monitor

    orders = list(
        SupplierOrder.objects.filter(
            name__in=["Партия chromium", "Партия firefox"]
        ).order_by("pk")
    )
    assert len(orders) == 2
    for order in orders:
        # Explicit synthetic calendar for a real Worker/Beat test on any CI date.
        order.calendar.check_at = clock_time(0)
        order.calendar.save()
        CalendarDay.objects.filter(calendar=order.calendar).update(working=True)
    orders[0].authorized_at = timezone.now() - timezone.timedelta(days=3)
    orders[0].save()
    monitor.delay().get(timeout=60)
    assert Incident.objects.filter(key=f"payment:{orders[0].pk}", closed=False).exists()
    orders[1].authorized_at = timezone.now() - timezone.timedelta(days=3)
    orders[1].save()
    deadline = time.monotonic() + 60
    while not Incident.objects.filter(
        key=f"payment:{orders[1].pk}", closed=False
    ).exists():
        if time.monotonic() > deadline:
            raise AssertionError("Beat did not run workflow monitor")
        time.sleep(1)
    print(
        "PASS workflow monitor: actual Worker, Beat, PostgreSQL incidents and payment reminders"
    )


{
    "seed": seed,
    "auth": auth,
    "manifest": manifest,
    "browser-fixtures": browser_fixtures,
    "workflow-integration": workflow_integration,
}[sys.argv[1]]()
