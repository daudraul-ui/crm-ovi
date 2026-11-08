from datetime import date, time, timedelta
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models.deletion import ProtectedError
from django.utils import timezone
from openpyxl import load_workbook
from pypdf import PdfReader

from crm.api import Conflict
from crm.models import Chain, Document, Notification, Project, Store
from workflow import services as s
from workflow.exports import render_quote
from workflow.models import (
    Allocation,
    Calendar,
    CalendarDay,
    Condition,
    Confirmation,
    Cost,
    Decision,
    Equipment,
    Incident,
    OrderLine,
    Quote,
    QuoteLine,
    Specification,
    SupplierOrder,
)
from workflow.tasks import monitor

pytestmark = pytest.mark.django_db


@pytest.fixture
def flow(user, company, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    manager = User.objects.create_user("manager-flow")
    manager.groups.add(Group.objects.get(name="Менеджер"))
    rop = User.objects.create_user("rop-flow")
    rop.groups.add(Group.objects.get(name="РОП"))
    engineer = User.objects.create_user("engineer-flow")
    engineer.groups.add(Group.objects.get(name="Инженер"))
    project = Project.objects.create(name="Проект БП", company=company, manager=manager)
    chain = Chain.objects.create(name="Сеть БП")
    store = Store.objects.create(
        name="Магазин", chain=chain, code="0001", address="Адрес"
    )
    project.stores.add(store)
    doc = Document.objects.create(
        name="Письмо / протокол",
        project=project,
        uploaded_by=manager,
        file=SimpleUploadedFile("proof.txt", b"written evidence"),
    )
    calendar = Calendar.objects.create(
        name="Тестовый календарь, не для production",
        timezone="Europe/Moscow",
        check_at=time(10),
        max_pause_days=10,
        quote_reminder_days=1,
    )
    start = timezone.now().date() - timedelta(days=60)
    CalendarDay.objects.bulk_create(
        [
            CalendarDay(
                calendar=calendar,
                date=start + timedelta(days=i),
                working=(start + timedelta(days=i)).weekday() < 5,
            )
            for i in range(180)
        ]
    )
    spec = Specification.objects.create(
        name="ТЗ 1", project=project, source="Замеры", reliable_dimensions=True
    )
    item = Equipment.objects.create(
        specification=spec,
        store=store,
        name="Горка",
        manufacturer="Завод",
        model="Модель",
        equipment_type="Горка",
        dimensions="1000 x 2000 мм",
        quantity=10,
        glazing="Двери",
        evidence=doc,
    )
    q = Quote.objects.create(
        name="КП 1",
        specification=spec,
        calendar=calendar,
        installation=True,
        vat_mode="separate",
        valid_until=timezone.now().date() + timedelta(days=30),
    )
    line = QuoteLine.objects.create(
        quote=q,
        store=store,
        name="Система остекления",
        quantity=10,
        price=100000,
        vat_rate=20,
    )
    cost = Cost.objects.create(
        quote=q,
        category="Остекление",
        name="Секретная себестоимость",
        equipment=item,
        quantity=10,
        price=70000,
        source="расчёт-1",
    )
    Allocation.objects.create(cost=cost, line=line, amount=700000)
    order = SupplierOrder.objects.create(
        name="Партия 1", supplier="Поставщик", specification=spec, calendar=calendar
    )
    ol = OrderLine.objects.create(order=order, equipment=item, quantity=10)
    condition = Condition.objects.create(
        order=order,
        name="Предоплата",
        payment=True,
        required_amount=1200000,
        payer="Клиент",
        recipient="OVI",
        order_reference="Заказ 1",
    )
    return locals()


def cmd(f, key, action, data=None, actor="manager"):
    obj = f[key]
    obj.refresh_from_db()
    return s.command(f[actor], type(obj), obj.pk, obj.version, action, data or {})


def technical(f):
    cmd(f, "spec", "approve_manager")
    cmd(f, "spec", "approve_engineer", actor="engineer")


def submit(f):
    for category, _ in Cost.CATEGORIES:
        if not Cost.objects.filter(quote=f["q"], category=category).exists():
            Cost.objects.create(
                quote=f["q"],
                category=category,
                name=category + " не требуется",
                equipment=f["item"],
                quantity=1,
                price=0,
                source=category + "-zero",
            )
    technical(f)
    cmd(
        f,
        "q",
        "policy",
        {"minimum_margin": Decimal("10"), "artificial_split": False},
        "rop",
    )
    cmd(f, "q", "submit")


def approve(f, actor="rop"):
    return cmd(
        f,
        "q",
        "decide",
        {
            "role": "РОП" if actor == "rop" else "Директор",
            "approved": True,
            "reason": "Проверено",
        },
        actor,
    )


def payment(f):
    return {
        "document": f["doc"].pk,
        "amount": Decimal("1200000"),
        "payer": "Клиент",
        "recipient": "OVI",
        "order_reference": "Заказ 1",
        "execution_checked": True,
    }


def launch(f):
    technical(f)
    cmd(f, "condition", "verify", payment(f))
    cmd(f, "order", "authorize")


@pytest.mark.parametrize(
    "total,expected",
    [("2999999.99", "approved"), ("3000000", "pending"), ("3000000.01", "pending")],
)
def test_threshold_includes_vat(flow, total, expected):
    line = flow["line"]
    line.quantity = 1
    line.price = Decimal(total)
    line.vat_rate = 0
    line.save()
    submit(flow)
    q = approve(flow)
    assert q.status == expected
    if expected == "pending":
        q = approve(flow, "user")
        assert q.status == "approved"
        assert q.decisions.count() == 2


def test_vat_crosses_threshold(flow):
    line = flow["line"]
    line.price = Decimal("250000")
    line.save()
    submit(flow)
    assert approve(flow).status == "pending"
    assert approve(flow, "user").status == "approved"


def test_refusal_requires_discussion_and_new_round(flow):
    submit(flow)
    cmd(
        flow,
        "q",
        "decide",
        {"role": "РОП", "approved": False, "reason": "Нужен разбор"},
        "rop",
    )
    with pytest.raises(ValidationError):
        approve(flow, "user")
    with pytest.raises(ValidationError):
        cmd(flow, "q", "submit")
    cmd(flow, "q", "submit", {"discussion": "РОП и директор обсудили замечания"})
    assert approve(flow).status == "approved"
    assert Decision.objects.filter(quote=flow["q"], approved=False).count() == 1


def test_one_person_cannot_supply_two_executive_approvals(flow):
    flow["rop"].groups.add(Group.objects.get(name="Директор"))
    submit(flow)
    Quote.objects.filter(pk=flow["q"].pk).update(artificial_split=True)
    approve(flow)
    with pytest.raises(ValidationError):
        cmd(
            flow,
            "q",
            "decide",
            {"role": "Директор", "approved": True, "reason": "same person"},
            "rop",
        )


def test_snapshot_cannot_be_mutated_or_reparented(flow):
    submit(flow)
    approve(flow)
    line = flow["line"]
    old = line.version
    line.price = 1
    with pytest.raises(ValidationError):
        s.save_record(flow["manager"], line, old)
    new = cmd(flow, "q", "revise")
    assert new.status == "draft" and new.decisions.count() == 0
    assert s.economics(new) == {
        **s.economics(flow["q"]),
        "rows": s.economics(new)["rows"],
    }
    line.quote = new
    with pytest.raises(ValidationError):
        s.save_record(flow["manager"], line, old)


def test_technical_requires_measurements_and_two_roles(flow):
    spec = flow["spec"]
    spec.nonstandard = True
    spec.save()
    with pytest.raises(ValidationError):
        cmd(flow, "spec", "approve_manager")
    spec.measurement = flow["doc"]
    spec.save()
    cmd(flow, "spec", "approve_manager")
    with pytest.raises(PermissionDenied):
        cmd(flow, "spec", "approve_engineer")
    cmd(flow, "spec", "approve_engineer", actor="engineer")
    new = cmd(flow, "spec", "revise")
    assert not new.frozen and new.items.count() == 1 and new.previous == spec


@pytest.mark.parametrize(
    "field,value",
    [
        ("amount", Decimal("1")),
        ("payer", "Чужой"),
        ("recipient", "Другой"),
        ("order_reference", "Другой заказ"),
        ("execution_checked", False),
    ],
)
def test_payment_document_checks_all_five_attributes(flow, field, value):
    data = payment(flow)
    data[field] = value
    with pytest.raises(ValidationError):
        cmd(flow, "condition", "verify", data)
    flow["condition"].refresh_from_db()
    assert not flow["condition"].fulfilled


def test_payment_order_is_not_receipt_and_conditions_gate_launch(flow):
    with pytest.raises(ValidationError):
        cmd(flow, "order", "authorize")
    launch(flow)
    condition = Condition.objects.get(pk=flow["condition"].pk)
    order = SupplierOrder.objects.get(pk=flow["order"].pk)
    assert condition.fulfilled and not condition.received
    assert order.due_at == s.business_due(order.calendar, order.authorized_at, 2)
    assert order.placement_task.assignee == flow["manager"]
    cmd(flow, "condition", "receipt", payment(flow))
    condition.refresh_from_db()
    assert condition.received


def test_partial_supplier_confirmation_and_stop(flow):
    launch(flow)
    cmd(flow, "order", "dispatch", {"document": flow["doc"].pk})
    data = {
        "document": flow["doc"].pk,
        "quantity": 6,
        "ready_on": date(2027, 1, 1),
        "composition": "Горка, двери",
        "composition_checked": True,
        "sender": "supplier@example.com",
        "received_at": timezone.now(),
    }
    with pytest.raises(PermissionDenied):
        cmd(flow, "ol", "confirm", data, "rop")
    cmd(flow, "ol", "confirm", data)
    flow["order"].refresh_from_db()
    assert flow["order"].status == "partial"
    with pytest.raises(ValidationError):
        cmd(flow, "ol", "confirm", data)
    data["quantity"] = 4
    cmd(flow, "ol", "confirm", data)
    flow["order"].refresh_from_db()
    assert flow["order"].status == "confirmed"
    cmd(
        flow,
        "ol",
        "request_stop",
        {"document": flow["doc"].pk, "reason": "Клиент меняет размеры"},
    )
    flow["ol"].refresh_from_db()
    assert flow["ol"].stop_requested and not flow["ol"].stopped
    cmd(
        flow,
        "ol",
        "confirm_stop",
        {"document": flow["doc"].pk, "reason": "Письменный ответ"},
    )
    flow["ol"].refresh_from_db()
    assert flow["ol"].stopped and Confirmation.objects.count() == 2


def test_parts_cannot_overbook_equipment(flow):
    second = SupplierOrder.objects.create(
        name="Партия 2",
        supplier="Поставщик",
        specification=flow["spec"],
        calendar=flow["calendar"],
    )
    with pytest.raises(ValidationError):
        s.save_record(
            flow["manager"], OrderLine(order=second, equipment=flow["item"], quantity=1)
        )


def test_pause_requires_rop_and_does_not_reset_deadline(flow):
    launch(flow)
    until = timezone.now() + timedelta(days=1)
    with pytest.raises(ValidationError):
        cmd(flow, "order", "pause", {"until": until}, "rop")
    cmd(flow, "order", "request_pause", {"reason": "Уточнение поставщика"})
    with pytest.raises(PermissionDenied):
        cmd(flow, "order", "pause", {"until": until})
    before = SupplierOrder.objects.get(pk=flow["order"].pk).due_at
    cmd(flow, "order", "pause", {"until": until}, "rop")
    assert SupplierOrder.objects.get(pk=flow["order"].pk).due_at == before


def test_business_calendar_missing_day_fails_closed(flow):
    tomorrow = timezone.now().date() + timedelta(days=1)
    CalendarDay.objects.filter(calendar=flow["calendar"], date=tomorrow).delete()
    with pytest.raises(ValidationError):
        s.business_due(flow["calendar"], timezone.now(), 2)


def test_escalation_workdays_and_idempotence(flow):
    launch(flow)
    cmd(flow, "order", "dispatch", {"document": flow["doc"].pk})
    order = SupplierOrder.objects.get(pk=flow["order"].pk)
    for day in (1, 2, 3):
        now = s.business_due(order.calendar, order.sent_at, day) + timedelta(minutes=1)
        with patch("workflow.tasks.timezone.now", return_value=now):
            monitor()
            count = Notification.objects.count()
            monitor()
            assert Notification.objects.count() == count
        incident = Incident.objects.get(key=f"confirmation:{order.pk}")
        assert incident.critical == (day >= 3)
        notes = Notification.objects.filter(key__contains=f"incident:{incident.pk}:")
        assert notes.filter(user=flow["manager"]).exists()
        assert notes.filter(user=flow["rop"]).exists() == (day >= 2)
        assert notes.filter(user=flow["user"]).exists() == (day >= 3)


def test_exception_keeps_unapproved_label_and_followup(flow):
    submit(flow)
    data = {
        "document": flow["doc"].pk,
        "followup_at": timezone.now() + timedelta(days=2),
    }
    with pytest.raises(ValidationError):
        cmd(flow, "q", "send", data)
    cmd(
        flow,
        "q",
        "exception",
        {"reason": "Предварительное предложение", "document": flow["doc"].pk},
        "rop",
    )
    q = cmd(flow, "q", "send", data)
    assert q.status == "pending" and q.followup.assignee == flow["manager"]
    pdf, _ = render_quote(q, "pdf")
    assert "НЕ СОГЛАСОВАНО" in "".join(
        p.extract_text() for p in PdfReader(BytesIO(pdf)).pages
    )
    with pytest.raises(ValidationError):
        cmd(flow, "q", "send", data)


def test_forecast_does_not_change_baseline_and_kpi_boundary(flow):
    submit(flow)
    approve(flow)
    b = cmd(
        flow,
        "q",
        "baseline",
        {"document": flow["doc"].pk, "reason": "Утверждённый заказ"},
    )
    flow["budget"] = b
    cmd(
        flow,
        "budget",
        "forecast",
        {
            "forecast_cost": Decimal("760000"),
            "actual_cost": Decimal("750000"),
            "actual_revenue": Decimal("1000000"),
            "finalized": True,
            "document": flow["doc"].pk,
            "reason": "Подтверждённый факт",
        },
    )
    b.refresh_from_db()
    assert b.cost == 700000 and b.forecast_cost == 760000
    flow["project"].status = "done"
    flow["project"].save()
    assert s.kpi()["percent"] == 100
    cmd(
        flow,
        "budget",
        "forecast",
        {
            "forecast_cost": Decimal("760000"),
            "actual_cost": Decimal("750001"),
            "actual_revenue": Decimal("1000000"),
            "finalized": True,
            "document": flow["doc"].pk,
            "reason": "Уточнение факта",
        },
    )
    assert s.kpi()["percent"] == 0


def test_pdf_and_excel_customer_totals_and_no_secrets(flow):
    pdf, _ = render_quote(flow["q"], "pdf")
    xlsx, _ = render_quote(flow["q"], "xlsx")
    text = "".join(p.extract_text() for p in PdfReader(BytesIO(pdf)).pages)
    wb = load_workbook(BytesIO(xlsx), data_only=True)
    assert wb.active["H10"].value == 1200000
    assert "1,200,000.00" in text
    assert "Секретная себестоимость" not in text
    assert "700,000" not in text
    flow["line"].name = '=HYPERLINK("https://example.com")'
    flow["line"].save()
    xlsx, _ = render_quote(flow["q"], "xlsx")
    assert load_workbook(BytesIO(xlsx)).active["A7"].data_type == "s"


def test_stale_commands_rollback(flow):
    q = flow["q"]
    old = q.version
    cmd(
        flow,
        "q",
        "policy",
        {"minimum_margin": Decimal("10"), "artificial_split": False},
        "rop",
    )
    with pytest.raises(Conflict):
        s.command(
            flow["rop"],
            Quote,
            q.pk,
            old,
            "policy",
            {"minimum_margin": Decimal("20"), "artificial_split": True},
        )
    q.refresh_from_db()
    assert q.minimum_margin == 10


def test_allocation_cannot_double_count(flow):
    second = QuoteLine.objects.create(
        quote=flow["q"],
        store=flow["store"],
        name="Другая строка",
        quantity=1,
        price=1,
        vat_rate=0,
    )
    with pytest.raises(ValidationError):
        s.save_record(
            flow["manager"], Allocation(cost=flow["cost"], line=second, amount=1)
        )


def test_documents_must_belong_to_project(flow):
    other = Project.objects.create(
        name="Чужой проект", company=flow["company"], manager=flow["manager"]
    )
    doc = Document.objects.create(
        name="Чужое письмо",
        project=other,
        uploaded_by=flow["manager"],
        file=SimpleUploadedFile("other.txt", b"proof"),
    )
    data = payment(flow)
    data["document"] = doc.pk
    with pytest.raises(ValidationError):
        cmd(flow, "condition", "verify", data)


def test_existing_crm_cannot_delete_used_project(flow):
    with pytest.raises(ProtectedError):
        flow["project"].delete()


def test_pages_and_api_permissions(verified, flow):
    assert verified.get("/workflow/").status_code == 200
    for kind in (
        "quotes",
        "orders",
        "specifications",
        "conditions",
        "budgets",
        "calendars",
        "inquiries",
    ):
        assert verified.get(f"/workflow/{kind}/").status_code == 200
        assert verified.get(f"/api/workflow/{kind}/").status_code == 200
    for kind, key in [("quotes", "q"), ("orders", "order"), ("specifications", "spec")]:
        assert verified.get(f"/workflow/{kind}/{flow[key].pk}/").status_code == 200
    response = verified.patch(
        f"/api/workflow/quotes/{flow['q'].pk}/",
        data=json_data({"status": "approved", "expected_version": flow["q"].version}),
        content_type="application/json",
    )
    assert response.status_code == 400
    verified.logout()
    assert verified.get("/api/workflow/quotes/").status_code == 403


def json_data(value):
    import json

    return json.dumps(value)


def test_engineer_cannot_read_economics(client, flow):
    from django_otp.plugins.otp_totp.models import TOTPDevice

    client.force_login(flow["engineer"])
    device = TOTPDevice.objects.create(user=flow["engineer"], confirmed=True)
    session = client.session
    session["otp_device_id"] = device.persistent_id
    session.save()
    assert client.get("/api/workflow/quotes/").status_code == 403
    assert client.get("/workflow/quotes/").status_code == 403
    assert client.get("/workflow/specifications/").status_code == 200


def test_handover_routes_open_tasks_to_rop_and_keeps_completed(flow):
    from crm.models import Task

    new = User.objects.create_user("next-manager")
    new.groups.add(Group.objects.get(name="Менеджер"))
    active = Task.objects.create(
        name="Открытая",
        project=flow["project"],
        assignee=flow["manager"],
        due_at=timezone.now(),
    )
    done = Task.objects.create(
        name="Закрытая",
        project=flow["project"],
        assignee=flow["manager"],
        due_at=timezone.now(),
        status="done",
    )
    project = flow["project"]
    project.manager = new
    with pytest.raises(ValidationError):
        project.full_clean()
    project.refresh_from_db()
    with pytest.raises(PermissionDenied):
        cmd(flow, "spec", "handover", {"new_manager": new.pk, "reason": "Передача"})
    cmd(flow, "spec", "handover", {"new_manager": new.pk, "reason": "Передача"}, "rop")
    project.refresh_from_db()
    active.refresh_from_db()
    done.refresh_from_db()
    assert project.manager == new
    assert active.assignee == flow["rop"] and done.assignee == flow["manager"]


def test_used_store_cannot_be_detached(flow):
    from django.db import transaction

    with pytest.raises(ValidationError), transaction.atomic():
        flow["project"].stores.remove(flow["store"])
    with pytest.raises(ValidationError), transaction.atomic():
        flow["project"].stores.clear()
    assert flow["project"].stores.filter(pk=flow["store"].pk).exists()


def test_sent_documents_are_immutable_and_history_is_available(verified, flow):
    from workflow.models import Dispatch

    submit(flow)
    cmd(
        flow,
        "q",
        "exception",
        {"reason": "Исключение", "document": flow["doc"].pk},
        "rop",
    )
    cmd(
        flow,
        "q",
        "send",
        {"document": flow["doc"].pk, "followup_at": timezone.now() + timedelta(days=1)},
    )
    original = bytes(Dispatch.objects.get(quote=flow["q"]).pdf)
    approve(flow)
    assert (
        verified.get(f"/workflow/quotes/{flow['q'].pk}/export/pdf/").content == original
    )
    assert "НЕ СОГЛАСОВАНО" in "".join(
        p.extract_text() for p in PdfReader(BytesIO(original)).pages
    )
    history = verified.get(f"/api/workflow/quotes/{flow['q'].pk}/history/")
    assert history.status_code == 200
    assert any(
        e["action"] == "send" and e["data"]["responsible_manager"] == flow["manager"].pk
        for e in history.json()["results"]
    )


def test_calendar_fill_requires_explicit_exceptions(flow):
    calendar = Calendar.objects.create(
        name="Новый",
        timezone="Europe/Moscow",
        check_at=time(10),
        max_pause_days=3,
        quote_reminder_days=2,
    )
    flow["new_calendar"] = calendar
    data = {
        "start": date(2027, 1, 4),
        "end": date(2027, 1, 10),
        "weekdays": ["0", "1", "2", "3", "4"],
        "holidays": "2027-01-05",
        "extra_workdays": "2027-01-09",
        "reason": "Утверждённый график",
    }
    cmd(flow, "new_calendar", "fill", data, "rop")
    assert calendar.calendarday_set.get(date=date(2027, 1, 5)).working is False
    assert calendar.calendarday_set.get(date=date(2027, 1, 9)).working is True
    with pytest.raises(ValidationError):
        cmd(flow, "new_calendar", "fill", data, "rop")


def test_forecast_below_minimum_requires_two_distinct_leaders(flow):
    from workflow.models import Budget

    submit(flow)
    approve(flow)
    cmd(
        flow,
        "q",
        "baseline",
        {
            "document": flow["doc"].pk,
            "reason": "Заказ утверждён",
            "previous_budget": None,
            "change_type": "",
        },
    )
    flow["budget"] = Budget.objects.get(quote=flow["q"])
    cmd(
        flow,
        "budget",
        "forecast",
        {
            "forecast_cost": Decimal("950000"),
            "actual_cost": None,
            "actual_revenue": None,
            "finalized": False,
            "document": flow["doc"].pk,
            "reason": "Внутренний перерасход",
        },
    )
    b = flow["budget"]
    b.refresh_from_db()
    assert s.budget_review(b) == "Требуется согласование обоих руководителей"
    cmd(
        flow,
        "budget",
        "decide",
        {"role": "РОП", "approved": True, "reason": "Проверено"},
        "rop",
    )
    b.refresh_from_db()
    assert s.budget_review(b) == "Требуется согласование обоих руководителей"
    with pytest.raises(ValidationError):
        cmd(
            flow,
            "budget",
            "decide",
            {"role": "РОП", "approved": True, "reason": "Повтор"},
            "rop",
        )
    cmd(
        flow,
        "budget",
        "decide",
        {"role": "Директор", "approved": True, "reason": "Проверено"},
        "user",
    )
    b.refresh_from_db()
    assert s.budget_review(b) == "Перерасход согласован обоими руководителями"
    assert b.cost == Decimal("700000")
    cmd(
        flow,
        "budget",
        "forecast",
        {
            "forecast_cost": Decimal("960000"),
            "actual_cost": None,
            "actual_revenue": None,
            "finalized": False,
            "document": flow["doc"].pk,
            "reason": "Новый прогноз",
        },
    )
    b.refresh_from_db()
    assert s.budget_review(b) == "Требуется согласование обоих руководителей"
