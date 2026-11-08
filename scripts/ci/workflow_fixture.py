"""Isolated browser fixture only; never run against a user database."""

import json
import os
import sys
from datetime import time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()

from django.contrib.auth.models import Group, User
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import Client
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice

from crm.models import Chain, Company, Document, Project, Store
from workflow.models import (
    Allocation,
    Calendar,
    CalendarDay,
    Condition,
    Cost,
    Equipment,
    OrderLine,
    Quote,
    QuoteLine,
    Specification,
    SupplierOrder,
)


def seed(suffix):
    if os.getenv("CRM_COMPOSE_SMOKE") != "1" and os.getenv("CRM_WORKFLOW_TEST") != "1":
        raise RuntimeError("Only explicitly isolated test environments are allowed.")
    prefix = "wf-browser-" + suffix
    if User.objects.filter(username__startswith=prefix).exists():
        raise RuntimeError("Fixture exists; refusing to overwrite data.")
    call_command("bootstrap", verbosity=0, stdout=sys.stderr)
    users = {}
    sessions = {}
    for key, role in [
        ("manager", "Менеджер"),
        ("engineer", "Инженер"),
        ("rop", "РОП"),
        ("director", "Директор"),
    ]:
        u = User.objects.create_user(prefix + "-" + key)
        u.groups.add(Group.objects.get(name=role))
        device = TOTPDevice.objects.create(user=u, confirmed=True)
        client = Client()
        client.force_login(u)
        session = client.session
        session["otp_device_id"] = device.persistent_id
        session.save()
        users[key] = u
        sessions[key] = session.session_key
    company = Company.objects.create(
        name="Тестовый клиент " + suffix, manager=users["manager"]
    )
    chain = Chain.objects.create(name="Тестовая сеть " + suffix)
    store = Store.objects.create(
        name="Магазин " + suffix, chain=chain, code="001", address="Тестовый адрес"
    )
    project = Project.objects.create(
        name="Сквозной БП " + suffix, company=company, manager=users["manager"]
    )
    project.stores.add(store)
    doc = Document.objects.create(
        name="Тестовое письменное подтверждение " + suffix,
        project=project,
        uploaded_by=users["manager"],
        file=ContentFile(b"Test evidence", name="evidence.txt"),
    )
    calendar = Calendar.objects.create(
        name="Демонстрационный календарь " + suffix,
        timezone="Europe/Moscow",
        check_at=time(10),
        max_pause_days=10,
        quote_reminder_days=1,
    )
    start = timezone.now().date() - timedelta(days=10)
    CalendarDay.objects.bulk_create(
        [
            CalendarDay(
                calendar=calendar,
                date=start + timedelta(days=i),
                working=(start + timedelta(days=i)).weekday() < 5,
            )
            for i in range(90)
        ]
    )
    spec = Specification.objects.create(
        name="ТЗ " + suffix,
        project=project,
        source="Тестовые замеры",
        reliable_dimensions=True,
    )
    item = Equipment.objects.create(
        specification=spec,
        store=store,
        name="Горка " + suffix,
        manufacturer="Завод",
        model="Модель",
        equipment_type="Горка",
        dimensions="1000 x 2000 мм",
        quantity=10,
        glazing="Двери",
        evidence=doc,
    )
    quote = Quote.objects.create(
        name="КП " + suffix,
        specification=spec,
        calendar=calendar,
        installation=True,
        vat_mode="separate",
        valid_until=timezone.now().date() + timedelta(days=20),
    )
    line = QuoteLine.objects.create(
        quote=quote,
        store=store,
        name="Система остекления " + suffix,
        quantity=10,
        price=100000,
        vat_rate=20,
    )
    for category, _ in Cost.CATEGORIES:
        cost = Cost.objects.create(
            quote=quote,
            category=category,
            name=category,
            equipment=item,
            quantity=1,
            price=700000 if category == "Остекление" else 0,
            source=suffix + "-" + category,
        )
        if cost.price:
            Allocation.objects.create(cost=cost, line=line, amount=cost.price)
    order = SupplierOrder.objects.create(
        name="Партия " + suffix,
        supplier="Поставщик",
        specification=spec,
        calendar=calendar,
    )
    order_line = OrderLine.objects.create(order=order, equipment=item, quantity=10)
    condition = Condition.objects.create(
        order=order,
        name="Предоплата",
        payment=True,
        required_amount=1200000,
        payer="Клиент",
        recipient="OVI",
        order_reference="Заказ 1",
    )
    return {
        "sessions": sessions,
        "project": project.pk,
        "spec": spec.pk,
        "quote": quote.pk,
        "order": order.pk,
        "line": order_line.pk,
        "condition": condition.pk,
        "document": doc.pk,
    }


if __name__ == "__main__":
    print(json.dumps({engine: seed(engine) for engine in ("chromium", "firefox")}))
