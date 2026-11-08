"""Transactional business commands shared by HTML, API and periodic checks."""

from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from crm.api import Conflict
from crm.models import Document, Notification, Task, WriteLock
from crm.services import assigned, audit

from .models import (
    Allocation,
    Budget,
    BudgetDecision,
    Calendar,
    CalendarDay,
    Condition,
    Confirmation,
    Cost,
    Decision,
    Dispatch,
    Equipment,
    Event,
    Incident,
    Inquiry,
    OrderLine,
    Quote,
    QuoteLine,
    Specification,
    SupplierOrder,
)

CENT = Decimal("0.01")


def rounded(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def role(user, *names):
    return user.is_active and user.groups.filter(name__in=names).exists()


def require_role(user, *names):
    if not role(user, *names):
        raise PermissionDenied("Операция недоступна для вашей роли.")


def project_for(obj):
    if isinstance(obj, (Inquiry, Specification)):
        return obj.project
    if isinstance(obj, (Quote, SupplierOrder)):
        return obj.project
    if isinstance(obj, Equipment):
        return obj.specification.project
    if isinstance(obj, (QuoteLine, Cost, Budget, Decision)):
        return obj.quote.project
    if isinstance(obj, Allocation):
        return obj.cost.quote.project
    if isinstance(obj, (Condition, OrderLine)):
        return obj.order.project
    if isinstance(obj, Confirmation):
        return obj.line.order.project
    if isinstance(obj, BudgetDecision):
        return obj.budget.quote.project
    return None


def manager(user, obj, substitute=True):
    project = project_for(obj)
    if (
        not project
        or not user.is_active
        or not (
            (
                user.pk == project.manager_id
                and role(user, "Менеджер", "Директор", "РОП")
            )
            or (substitute and role(user, "РОП"))
        )
    ):
        raise PermissionDenied(
            "Требуется менеджер проекта"
            + (" или замещающий РОП." if substitute else ".")
        )


def leaders(*names):
    return (
        get_user_model()
        .objects.filter(is_active=True, groups__name__in=names or ["РОП", "Директор"])
        .distinct()
    )


def notify(users, text, key):
    for user in {u.pk: u for u in users if u.is_active}.values():
        Notification.objects.get_or_create(
            key=f"wf:{key}:{user.pk}", defaults={"user": user, "text": text[:500]}
        )


def event(user, obj, action, **data):
    project = project_for(obj)
    if project:
        data.setdefault("responsible_manager", project.manager_id)
    Event.objects.create(
        actor=user,
        model=obj._meta.label,
        object_id=obj.pk,
        action=action,
        data=data,
        document=evidence(data["document"], obj) if data.get("document") else None,
    )
    if user:
        audit(user, action, obj)
        project = project_for(obj)
        if project and user.pk != project.manager_id and role(user, "РОП"):
            notify(
                [project.manager],
                f"РОП выполнил действие «{action}» по {obj}. Первоначальный ответственный сохранён.",
                f"substitute:{obj._meta.model_name}:{obj.pk}:{obj.version}:{action}",
            )


def evidence(document_id, obj):
    try:
        doc = Document.objects.get(pk=document_id, archived=False)
    except (Document.DoesNotExist, TypeError, ValueError):
        raise ValidationError("Выберите действующий документ проекта.")
    if doc.project_id != project_for(obj).pk:
        raise ValidationError("Документ должен относиться к этому проекту.")
    return doc


def moment(calendar, date):
    return datetime.combine(date, calendar.check_at, tzinfo=ZoneInfo(calendar.timezone))


def business_due(calendar, start, days):
    """Explicit dates only: a missing calendar date must never silently mean weekend."""
    date = start.astimezone(ZoneInfo(calendar.timezone)).date()
    for _ in range(3700):
        date += timedelta(days=1)
        entry = calendar.calendarday_set.filter(date=date).first()
        if entry is None:
            raise ValidationError(f"Календарь «{calendar}»: не задан день {date}.")
        if entry.working:
            days -= 1
            if days == 0:
                return moment(calendar, date)
    raise ValidationError("В календаре недостаточно рабочих дней.")


def elapsed(calendar, start, now):
    zone = ZoneInfo(calendar.timezone)
    begin, end = start.astimezone(zone).date(), now.astimezone(zone).date()
    if end <= begin:
        return 0
    entries = list(calendar.calendarday_set.filter(date__gt=begin, date__lte=end))
    if len(entries) != (end - begin).days:
        raise ValidationError("В рабочем календаре есть незаполненные даты.")
    return sum(d.working and moment(calendar, d.date) <= now for d in entries)


def economics(quote):
    rows = []
    for line in quote.lines.all().order_by("pk"):
        net = rounded(line.quantity * line.price)
        vat = rounded(net * line.vat_rate / 100)
        rows.append(
            {
                "id": line.pk,
                "store": line.store_id,
                "store_code": line.store.code,
                "name": line.name,
                "quantity": line.quantity,
                "unit": line.unit,
                "price": line.price,
                "rate": line.vat_rate,
                "net": net,
                "vat": vat,
                "gross": net + vat,
            }
        )
    net = sum((r["net"] for r in rows), Decimal(0))
    vat = sum((r["vat"] for r in rows), Decimal(0))
    cost = sum((rounded(c.quantity * c.price) for c in quote.costs.all()), Decimal(0))
    stores = {}
    for row in rows:
        group = stores.setdefault(
            row["store"],
            {"code": row["store_code"], "net": Decimal(0), "cost": Decimal(0)},
        )
        group["net"] += row["net"]
        group["cost"] += Allocation.objects.filter(line_id=row["id"]).aggregate(
            total=Sum("amount")
        )["total"] or Decimal(0)
    for group in stores.values():
        group["profit"] = group["net"] - group["cost"]
        group["margin"] = margin(group["net"], group["cost"])
    return {
        "rows": rows,
        "stores": list(stores.values()),
        "net": net,
        "vat": vat,
        "gross": net + vat,
        "cost": cost,
        "profit": net - cost,
        "margin": margin(net, cost),
    }


def margin(revenue, cost):
    return (revenue - cost) / revenue * 100 if revenue else None


def needs_both(quote):
    e = economics(quote)
    return (
        e["gross"] >= Decimal("3000000")
        or quote.artificial_split
        or (
            quote.minimum_margin is not None
            and e["margin"] is not None
            and e["margin"] < quote.minimum_margin
        )
    )


def check_spec(spec):
    if not spec.items.exists():
        raise ValidationError("Добавьте позиции оборудования.")
    if spec.measurements_required and not spec.measurement_id:
        raise ValidationError("По условиям спецификации требуются результаты замеров.")
    if spec.measurement_id:
        evidence(spec.measurement_id, spec)


def check_quote(quote):
    spec = quote.specification
    if not spec.manager_approved_id or not spec.engineer_approved_id:
        raise ValidationError(
            "Требуются подтверждения технической спецификации менеджером и инженером."
        )
    check_spec(spec)
    e = economics(quote)
    if not e["rows"] or e["net"] <= 0 or not quote.costs.exists():
        raise ValidationError(
            "Добавьте клиентские позиции с положительной выручкой и калькуляцию."
        )
    if quote.minimum_margin is None:
        raise ValidationError("РОП или директор должен задать коммерческий минимум.")
    mandatory = {name for name, _ in Cost.CATEGORIES} - {"Шеф-монтаж"}
    missing = mandatory - set(quote.costs.values_list("category", flat=True))
    if missing:
        raise ValidationError(
            "Заполните обязательные категории затрат (0, если не применимо): "
            + ", ".join(sorted(missing))
        )
    for cost in quote.costs.all():
        allocated = cost.allocations.aggregate(total=Sum("amount"))["total"] or 0
        if allocated != rounded(cost.quantity * cost.price):
            raise ValidationError(
                f"Распределите всю стоимость статьи «{cost.name}» между клиентскими строками."
            )
    return e


def editable(obj):
    if isinstance(obj, Specification) and (
        obj.manager_approved_id or obj.engineer_approved_id or obj.frozen
    ):
        raise ValidationError("Подтверждённое ТЗ меняется через новую редакцию.")
    if isinstance(obj, Equipment):
        editable(obj.specification)
    if isinstance(obj, Quote) and obj.status != "draft":
        raise ValidationError(
            "КП уже передано на согласование. Создайте новую редакцию."
        )
    if isinstance(obj, (QuoteLine, Cost)):
        editable(obj.quote)
    if isinstance(obj, Allocation):
        editable(obj.cost.quote)
    if isinstance(obj, SupplierOrder) and obj.status != "waiting":
        raise ValidationError(
            "Состав разрешённой партии неизменяем. Создайте отдельную партию."
        )
    if isinstance(obj, (Condition, OrderLine)):
        editable(obj.order)


def validate_links(obj):
    project = project_for(obj)
    if project and project.archived:
        raise ValidationError("Проект в архиве.")
    if isinstance(obj, Equipment):
        if not obj.specification.project.stores.filter(pk=obj.store_id).exists():
            raise ValidationError("Магазин не включён в проект.")
        evidence(obj.evidence_id, obj)
    if (
        isinstance(obj, QuoteLine)
        and not obj.quote.project.stores.filter(pk=obj.store_id).exists()
    ):
        raise ValidationError("Магазин не включён в проект.")
    if (
        isinstance(obj, Cost)
        and obj.equipment.specification_id != obj.quote.specification_id
    ):
        raise ValidationError("Позиция оборудования принадлежит другому ТЗ.")
    if isinstance(obj, Allocation):
        if obj.cost.quote_id != obj.line.quote_id:
            raise ValidationError(
                "Затраты и клиентская строка должны относиться к одному КП."
            )
        allocated = (
            obj.cost.allocations.exclude(pk=obj.pk).aggregate(total=Sum("amount"))[
                "total"
            ]
            or 0
        )
        if allocated + obj.amount > rounded(obj.cost.quantity * obj.cost.price):
            raise ValidationError(
                "Распределение превышает стоимость статьи (двойной учёт)."
            )
    if isinstance(obj, (Specification, Quote)) and obj.previous_id:
        if project_for(obj.previous).pk != project.pk:
            raise ValidationError("Предыдущая редакция относится к другому проекту.")
    if isinstance(obj, OrderLine):
        if obj.equipment.specification_id != obj.order.specification_id:
            raise ValidationError("Позиция относится к другому ТЗ.")
        ordered = (
            OrderLine.objects.filter(equipment=obj.equipment)
            .exclude(pk=obj.pk)
            .aggregate(total=Sum("quantity"))["total"]
            or 0
        )
        if ordered + obj.quantity > obj.equipment.quantity:
            raise ValidationError(
                "Сумма партий превышает согласованное количество оборудования."
            )
    if (
        isinstance(obj, Condition)
        and obj.payment
        and (
            not obj.required_amount
            or not obj.payer.strip()
            or not obj.recipient.strip()
            or not obj.order_reference.strip()
        )
    ):
        raise ValidationError(
            "Для платёжного условия нужны сумма, плательщик, получатель и реквизиты заказа."
        )


def lock_writes():
    WriteLock.objects.get_or_create(name="crm")
    WriteLock.objects.select_for_update().get(name="crm")


def check_version(obj, version):
    if version != obj.version:
        raise Conflict()


@transaction.atomic
def save_record(user, obj, expected=None):
    lock_writes()
    if obj.pk:
        current = type(obj).objects.select_for_update().get(pk=obj.pk)
        check_version(current, expected)
        # Parent changes could move a previously frozen child into a draft.
        editable(current)
        for field in obj._meta.fields:
            if field.is_relation and getattr(obj, field.attname) != getattr(
                current, field.attname
            ):
                raise ValidationError(
                    "Связи существующей записи не переносятся. Создайте новую запись."
                )
    if isinstance(obj, (Calendar, CalendarDay)):
        require_role(user, "РОП", "Директор")
        if obj.pk:
            raise ValidationError(
                "Используемый календарь не перезаписывается. Создайте новый календарь или добавьте новые даты."
            )
    elif isinstance(obj, (Specification, Equipment)) and role(user, "Инженер"):
        pass
    else:
        manager(user, obj)
    editable(obj)
    obj.full_clean()
    validate_links(obj)
    obj.save()
    event(user, obj, "save")
    if isinstance(obj, Inquiry) and expected is None:
        task = Task.objects.create(
            name=obj.next_step,
            project=obj.project,
            assignee=obj.project.manager,
            due_at=obj.next_at,
        )
        assigned(task)
    return obj


@transaction.atomic
def command(user, model, pk, version, action, data):
    lock_writes()
    obj = model.objects.select_for_update().get(pk=pk)
    check_version(obj, version)
    now = timezone.now()
    handlers = {
        Inquiry: spec_command,
        Calendar: calendar_command,
        Specification: spec_command,
        Quote: quote_command,
        SupplierOrder: order_command,
        Condition: condition_command,
        OrderLine: line_command,
        Budget: budget_command,
    }
    if model not in handlers:
        raise ValidationError("Команда не поддерживается.")
    result = handlers[model](user, obj, action, data, now)
    obj.save()
    event(user, obj, action, **{k: str(v) for k, v in data.items()})
    return result or obj


def spec_command(user, spec, action, data, now):
    if action == "handover":
        require_role(user, "РОП")
        project = spec.project
        new = get_user_model().objects.get(pk=data["new_manager"])
        require_role(new, "Менеджер")
        if new.pk == project.manager_id:
            raise ValidationError("Этот сотрудник уже отвечает за проект.")
        previous = project.manager_id
        tasks = list(
            Task.objects.select_for_update()
            .filter(project=project, assignee_id=previous)
            .exclude(status="done")
        )
        for task in tasks:
            task.assignee = user
            task.save()
            assigned(task)
        project.manager = new
        project._workflow_handover = True
        project.save()
        event(
            user,
            spec,
            "tasks_transferred",
            previous_manager=previous,
            new_manager=new.pk,
            rop=user.pk,
            tasks=[t.pk for t in tasks],
            reason=data["reason"],
        )
        notify(
            [new, user],
            f"Проект {project} передан новому менеджеру. РОП распределяет {len(tasks)} открытых задач.",
            f"handover:{project.pk}:{project.version}",
        )
        return
    if isinstance(spec, Inquiry):
        raise ValidationError("Для обращения доступна только передача менеджера.")
    if action == "revise":
        manager(user, spec)
        new = Specification.objects.create(
            name=spec.name + " — новая редакция",
            project=spec.project,
            previous=spec,
            source=spec.source,
            reliable_dimensions=spec.reliable_dimensions,
            nonstandard=spec.nonstandard,
            tender_measurement=spec.tender_measurement,
            requested_measurement=spec.requested_measurement,
            measurement=spec.measurement,
        )
        for item in spec.items.all():
            item.pk = None
            item._state.adding = True
            item.specification = new
            item.version = 1
            item.save()
        event(user, new, "revision_created", previous=spec.pk)
        return new
    if action not in ("approve_manager", "approve_engineer") or spec.frozen:
        raise ValidationError("Недопустимая операция с ТЗ.")
    check_spec(spec)
    if action == "approve_manager":
        manager(user, spec)
        spec.manager_approved = user
    else:
        require_role(user, "Инженер")
        spec.engineer_approved = user
    spec.frozen = bool(spec.manager_approved_id and spec.engineer_approved_id)


def quote_command(user, quote, action, data, now):
    if action == "policy":
        require_role(user, "РОП", "Директор")
        if quote.status != "draft":
            raise ValidationError(
                "Коммерческие правила задаются до отправки на согласование."
            )
        quote.minimum_margin = data["minimum_margin"]
        quote.artificial_split = data["artificial_split"]
    elif action == "submit":
        manager(user, quote)
        if quote.status not in ("draft", "discussion"):
            raise ValidationError("КП уже отправлено на согласование.")
        if quote.status == "discussion" and not data.get("discussion"):
            raise ValidationError(
                "Зафиксируйте результат совместного обсуждения после отказа."
            )
        check_quote(quote)
        # Require enough calendar coverage before a promise is recorded.
        business_due(quote.calendar, now, quote.calendar.quote_reminder_days)
        quote.round += 1
        quote.submitted_at = now
        quote.status = "pending"
        quote.exception_by = None
        quote.exception_reason = ""
        notify(
            leaders(),
            f"КП {quote}: требуется согласование редакции, раунд {quote.round}.",
            f"quote:{quote.pk}:round:{quote.round}",
        )
        related = (
            Quote.objects.filter(specification__project=quote.project)
            .exclude(pk=quote.pk)
            .exclude(status="draft")
        )
        # Count only the latest submitted revision of each proposal.
        total = economics(quote)["gross"] + sum(
            (
                economics(q)["gross"]
                for q in related
                if not Quote.objects.filter(previous=q).exclude(status="draft").exists()
            ),
            Decimal(0),
        )
        if total > Decimal("3000000"):
            notify(
                leaders("РОП"),
                f"Связанные КП проекта {quote.project} превышают 3 млн ₽. Проверьте причины разделения.",
                f"split:{quote.pk}:{quote.round}",
            )
    elif action == "decide":
        selected_role = data["role"]
        require_role(user, selected_role)
        if selected_role not in ("РОП", "Директор") or quote.status != "pending":
            raise ValidationError("Нет открытого согласования для этой роли.")
        if Decision.objects.filter(
            quote=quote, round=quote.round, role=selected_role
        ).exists():
            raise ValidationError("Решение этой роли уже зарегистрировано.")
        # A dual-role account cannot stand in for two different executives.
        if Decision.objects.filter(quote=quote, round=quote.round, actor=user).exists():
            raise ValidationError(
                "Для двух согласований нужны два разных руководителя."
            )
        Decision.objects.create(
            quote=quote,
            round=quote.round,
            role=selected_role,
            actor=user,
            approved=data["approved"],
            reason=data["reason"],
        )
        if not data["approved"]:
            quote.status = "discussion"
            quote.exception_by = None
            quote.exception_reason = ""
        else:
            count = quote.decisions.filter(round=quote.round, approved=True).count()
            if count >= (2 if needs_both(quote) else 1):
                quote.status = "approved"
                Incident.objects.filter(key=f"quote:{quote.pk}").update(closed=True)
        notify(
            [quote.project.manager, *leaders()],
            f"КП {quote}: решение {selected_role}; состояние {quote.status}.",
            f"decision:{quote.pk}:{quote.round}:{selected_role}",
        )
    elif action == "exception":
        require_role(user, "РОП", "Директор")
        if quote.status not in ("pending", "discussion"):
            raise ValidationError(
                "Исключение применяется к несогласованному КП на рассмотрении."
            )
        check_quote(quote)
        evidence(data["document"], quote)
        quote.exception_by = user
        quote.exception_reason = data["reason"]
    elif action == "send":
        manager(user, quote)
        if quote.status != "approved" and not quote.exception_by_id:
            raise ValidationError(
                "Нет согласования или отдельного разрешения руководителя."
            )
        if quote.sent_at:
            raise ValidationError("Отправка уже зарегистрирована.")
        check_quote(quote)
        if data["followup_at"] <= now:
            raise ValidationError("Укажите будущую дату следующего контакта.")
        evidence(data["document"], quote)
        quote.sent_at = now
        from .exports import render_quote

        pdf, _ = render_quote(quote, "pdf")
        xlsx, _ = render_quote(quote, "xlsx")
        Dispatch.objects.create(
            quote=quote, actor=user, status=quote.status, pdf=pdf, xlsx=xlsx
        )
        quote.followup = Task.objects.create(
            name=f"Связаться с клиентом по КП {quote.name}",
            project=quote.project,
            assignee=quote.project.manager,
            due_at=data["followup_at"],
        )
        assigned(quote.followup)
    elif action == "revise":
        manager(user, quote)
        target = quote.specification
        if data.get("specification"):
            target = Specification.objects.get(pk=data["specification"])
            if target.project_id != quote.project.pk:
                raise ValidationError(
                    "Новая спецификация должна относиться к тому же проекту."
                )
        new = Quote.objects.create(
            name=quote.name + " — новая редакция",
            specification=target,
            calendar=quote.calendar,
            previous=quote,
            installation=quote.installation,
            vat_mode=quote.vat_mode,
            valid_until=quote.valid_until,
            minimum_margin=quote.minimum_margin,
            artificial_split=quote.artificial_split,
            split_reason=quote.split_reason,
        )
        if target.pk != quote.specification_id:
            event(
                user,
                new,
                "revision_created",
                previous=quote.pk,
                new_specification=target.pk,
            )
            notify(
                [quote.project.manager, *leaders("РОП")],
                f"Новая редакция {new}: заполните калькуляцию по изменённому ТЗ.",
                f"revision:{new.pk}",
            )
            return new
        line_ids, cost_ids = {}, {}
        for row in quote.lines.all():
            old = row.pk
            row.pk = None
            row._state.adding = True
            row.quote = new
            row.version = 1
            row.save()
            line_ids[old] = row.pk
        for row in quote.costs.all():
            old = row.pk
            row.pk = None
            row._state.adding = True
            row.quote = new
            row.version = 1
            row.save()
            cost_ids[old] = row.pk
        for allocation in Allocation.objects.filter(cost__quote=quote):
            Allocation.objects.create(
                cost_id=cost_ids[allocation.cost_id],
                line_id=line_ids[allocation.line_id],
                amount=allocation.amount,
            )
        event(user, new, "revision_created", previous=quote.pk)
        notify(
            [quote.project.manager, *leaders("РОП")],
            f"Создана новая редакция КП {new}. Требуется повторное согласование.",
            f"revision:{new.pk}",
        )
        return new
    elif action == "baseline":
        manager(user, quote)
        if quote.status != "approved" or Budget.objects.filter(quote=quote).exists():
            raise ValidationError(
                "Нужна согласованная редакция без уже зафиксированной базы."
            )
        doc = evidence(data["document"], quote)
        previous = None
        ancestors = set()
        ancestor = quote.previous
        while ancestor is not None:
            ancestors.add(ancestor.pk)
            ancestor = ancestor.previous
        if data.get("previous_budget"):
            previous = Budget.objects.get(pk=data["previous_budget"])
            if (
                previous.quote.project.pk != quote.project.pk
                or previous.quote_id not in ancestors
                or Budget.objects.filter(previous=previous).exists()
            ):
                raise ValidationError("Неверная предыдущая база.")
            if data.get("change_type") not in ("price", "volume", "composition"):
                raise ValidationError(
                    "База меняется только при согласованных изменениях клиента: цена, объём или состав."
                )
        elif Budget.objects.filter(quote_id__in=ancestors).exists():
            raise ValidationError(
                "Для изменения заказа укажите предыдущую базу и согласованное изменение клиента."
            )
        e = check_quote(quote)
        stores = {}
        for row in e["rows"]:
            key = str(row["store"])
            entry = stores.setdefault(key, {"revenue": "0", "cost": "0"})
            entry["revenue"] = str(Decimal(entry["revenue"]) + row["net"])
            allocated = (
                Allocation.objects.filter(line_id=row["id"]).aggregate(
                    total=Sum("amount")
                )["total"]
                or 0
            )
            entry["cost"] = str(Decimal(entry["cost"]) + allocated)
        baseline = Budget.objects.create(
            quote=quote,
            previous=previous,
            evidence=doc,
            reason=data["reason"],
            revenue=e["net"],
            cost=e["cost"],
            forecast_cost=e["cost"],
            stores=stores,
        )
        event(
            user,
            baseline,
            "baseline_created",
            change_type=data.get("change_type", "initial"),
        )
        return baseline
    else:
        raise ValidationError("Неизвестная команда КП.")


def condition_command(user, condition, action, data, now):
    if action == "exception":
        require_role(user, "РОП", "Директор")
        if not condition.payment or condition.order.authorized_at:
            raise ValidationError(
                "Исключение возможно для предоплаты до разрешения запуска."
            )
        condition.exception_reason = data["reason"]
        condition.fulfilled = True
        condition.verified_by = user
        condition.evidence = evidence(data["document"], condition)
        return
    manager(user, condition)
    if action not in ("verify", "receipt"):
        raise ValidationError("Неизвестная операция проверки.")
    if action == "receipt" and not condition.payment:
        raise ValidationError("Условие не относится к оплате.")
    if condition.order.authorized_at and action != "receipt":
        raise ValidationError(
            "Основание уже разрешённого запуска сохраняется неизменным."
        )
    doc = evidence(data["document"], condition)
    if condition.payment:
        if (
            data["amount"] < condition.required_amount
            or data["payer"].strip() != condition.payer.strip()
            or data["recipient"].strip() != condition.recipient.strip()
            or data["order_reference"].strip() != condition.order_reference.strip()
            or not data["execution_checked"]
        ):
            raise ValidationError(
                "Сумма, плательщик, получатель, реквизиты или статус исполнения не прошли проверку."
            )
    condition.evidence = doc
    condition.verified_by = user
    condition.fulfilled = True
    if action == "receipt":
        condition.received = True


def order_command(user, order, action, data, now):
    if action in ("pause", "resume"):
        require_role(user, "РОП")
        if action == "pause":
            if not order.pause_requested:
                raise ValidationError(
                    "Сначала менеджер должен зарегистрировать причину задержки."
                )
            until = data["until"]
            if until <= now or until > now + timedelta(
                days=order.calendar.max_pause_days
            ):
                raise ValidationError(
                    "Срок паузы выходит за установленный предел календаря."
                )
            order.pause_until = until
            order.pause_by = user
        else:
            order.pause_until = None
            order.pause_requested = False
        return
    if action == "reschedule":
        require_role(user, "РОП", "Директор")
        if not order.authorized_at or order.sent_at or data["until"] <= now:
            raise ValidationError(
                "Перенос возможен для неотправленного разрешённого заказа на будущую дату."
            )
        event(
            user,
            order,
            "deadline_changed",
            old=str(order.due_at),
            new=str(data["until"]),
            reason=data["reason"],
        )
        order.due_at = data["until"]
        if order.placement_task:
            task = order.placement_task
            task.due_at = order.due_at
            task.save()
        return
    manager(user, order)
    if action == "authorize":
        if order.authorized_at or order.status != "waiting":
            raise ValidationError("Запуск уже разрешён.")
        check_spec(order.specification)
        if (
            not order.specification.frozen
            or not order.conditions.exists()
            or order.conditions.filter(fulfilled=False).exists()
            or not order.lines.exists()
        ):
            raise ValidationError(
                "Нужны подтверждённое ТЗ, позиции партии и выполнение всех индивидуальных условий."
            )
        order.due_at = business_due(order.calendar, now, 2)
        order.authorized_at = now
        order.status = "ready"
        order.placement_task = Task.objects.create(
            name=f"Разместить заказ {order.name}",
            project=order.project,
            assignee=order.project.manager,
            due_at=order.due_at,
        )
        assigned(order.placement_task)
    elif action == "dispatch":
        if order.status != "ready":
            raise ValidationError(
                "Сначала разрешите запуск при выполнении всех условий."
            )
        order.dispatch_evidence = evidence(data["document"], order)
        business_due(order.calendar, now, 3)
        order.sent_at = now
        order.status = "sent"
        task = order.placement_task
        task.status = "done"
        task.save()
        Incident.objects.filter(key=f"placement:{order.pk}").update(closed=True)
    elif action == "request_pause":
        if not order.authorized_at or order.status == "confirmed":
            raise ValidationError("Нет активного контроля для паузы.")
        order.pause_requested = True
        order.pause_reason = data["reason"]
        notify(
            leaders("РОП"),
            f"Заказ {order}: менеджер просит приостановить эскалацию.",
            f"pause:{order.pk}:{order.version}",
        )
    elif action == "refuse":
        if not order.sent_at:
            raise ValidationError("Заказ ещё не отправлен.")
        evidence(data["document"], order)
        order.status = "refused"
        notify(
            [order.project.manager, *leaders()],
            f"Поставщик отказал по заказу {order}. Требуется решение.",
            f"refusal:{order.pk}:{order.version}",
        )
    elif action == "payment_risk":
        notify(
            leaders("Директор"),
            f"Критический риск неоплаты по заказу {order}: {data['reason']}",
            f"payment-risk:{order.pk}:{order.version}",
        )
    else:
        raise ValidationError("Неизвестная команда заказа.")


def line_command(user, line, action, data, now):
    manager(user, line, substitute=False)
    if not line.order.sent_at:
        raise ValidationError("Заказ ещё не отправлен поставщику.")
    doc = evidence(data["document"], line)
    if action == "confirm":
        if line.stopped:
            raise ValidationError("Производство позиции письменно остановлено.")
        from django.core.validators import validate_email

        validate_email(data.get("sender", ""))
        if not data.get("received_at") or data["received_at"] > now:
            raise ValidationError("Укажите дату уже полученного письма поставщика.")
        quantity = data["quantity"]
        if (
            not data["composition_checked"]
            or quantity <= 0
            or quantity > line.quantity - line.confirmed_quantity
        ):
            raise ValidationError(
                "Подтвердите состав и укажите количество в пределах неподтверждённого остатка."
            )
        Confirmation.objects.create(
            line=line,
            sender=data["sender"],
            received_at=data["received_at"],
            document=doc,
            quantity=quantity,
            ready_on=data["ready_on"],
            composition=data["composition"],
            actor=user,
        )
        line.confirmed_quantity += quantity
        line.ready_on = max(filter(None, [line.ready_on, data["ready_on"]]))
        line.save()
        order = line.order
        order.status = (
            "confirmed"
            if not order.lines.exclude(confirmed_quantity=models_F("quantity")).exists()
            else "partial"
        )
        order.save()
        if order.status == "confirmed":
            Incident.objects.filter(key=f"confirmation:{order.pk}").update(closed=True)
    elif action == "request_stop":
        line.stop_requested = True
        notify(
            [line.order.project.manager, *leaders("РОП")],
            f"Запрошена остановка позиции {line.pk}. До письменного ответа поставщика позиция в производстве. Согласуйте действия с клиентом и РОПом.",
            f"stop:{line.pk}:{line.version}",
        )
    elif action == "confirm_stop":
        if not line.stop_requested:
            raise ValidationError("Запрос остановки отсутствует.")
        line.stopped = True
    else:
        raise ValidationError("Неизвестная команда позиции.")


def models_F(name):
    from django.db.models import F

    return F(name)


def budget_command(user, budget, action, data, now):
    if action == "decide":
        require_role(user, data["role"])
        if (
            data["role"] not in ("РОП", "Директор")
            or budget_review(budget) != "Требуется согласование обоих руководителей"
        ):
            raise ValidationError("Нет открытого согласования перерасхода.")
        decisions = budget.decisions.filter(revision=budget.forecast_revision)
        if (
            decisions.filter(role=data["role"]).exists()
            or decisions.filter(actor=user).exists()
        ):
            raise ValidationError(
                "Требуются два разных руководителя; решение этой роли уже учтено."
            )
        BudgetDecision.objects.create(
            budget=budget,
            revision=budget.forecast_revision,
            role=data["role"],
            actor=user,
            approved=data["approved"],
            reason=data["reason"],
        )
        notify(
            [budget.quote.project.manager, *leaders()],
            f"Перерасход по {budget.quote}: {budget_review(budget)}",
            f"budget-decision:{budget.pk}:{budget.version}",
        )
        return
    manager(user, budget)
    if action != "forecast":
        raise ValidationError(
            "База неизменяема. Для изменений клиента создайте согласованную редакцию КП."
        )
    budget.forecast_cost = data["forecast_cost"]
    budget.forecast_revision += 1
    budget.actual_cost = data.get("actual_cost")
    budget.actual_revenue = data.get("actual_revenue")
    budget.finalized = data["finalized"]
    if budget.finalized and (
        budget.actual_cost is None or budget.actual_revenue is None
    ):
        raise ValidationError(
            "Для финансового завершения требуются фактические затраты и выручка."
        )
    if budget.actual_cost is not None or budget.actual_revenue is not None:
        evidence(data.get("document"), budget)
    original = margin(budget.revenue, budget.cost)
    forecast = margin(budget.revenue, budget.forecast_cost)
    if budget_review(budget) == "Требуется согласование обоих руководителей":
        notify(
            leaders(),
            f"Прогноз по {budget.quote} ниже коммерческого минимума: требуется согласование перерасхода РОПом и директором. База сохранена.",
            f"budget-review:{budget.pk}:{budget.forecast_revision}",
        )
    if original is not None and forecast is not None and original - forecast > 5:
        notify(
            leaders("РОП"),
            f"Снижение прогнозной маржи более 5 п.п. по {budget.quote}. Утверждённая база сохранена.",
            f"margin:{budget.pk}:{budget.version}",
        )


def budget_review(budget):
    forecast = (
        margin(budget.revenue, budget.forecast_cost)
        if budget.forecast_cost is not None
        else None
    )
    if (
        forecast is None
        or budget.quote.minimum_margin is None
        or forecast >= budget.quote.minimum_margin
    ):
        return "Дополнительное согласование не требуется"
    decisions = budget.decisions.filter(revision=budget.forecast_revision)
    if decisions.filter(approved=False).exists():
        return "Отказ: требуется новый прогноз и новое согласование"
    if decisions.filter(approved=True).count() == 2:
        return "Перерасход согласован обоими руководителями"
    return "Требуется согласование обоих руководителей"


@transaction.atomic
def remove_line(user, model, pk, version):
    lock_writes()
    if model not in (QuoteLine, Cost, Allocation, Equipment, OrderLine, Condition):
        raise ValidationError("Удаление этого объекта не поддерживается.")
    obj = model.objects.select_for_update().get(pk=pk)
    check_version(obj, version)
    manager(user, obj)
    editable(obj)
    event(user, obj, "remove_draft_line")
    obj.delete()


def kpi():
    projects = {}
    for budget in Budget.objects.filter(replacement__isnull=True).select_related(
        "quote__specification__project"
    ):
        group = projects.setdefault(budget.quote.project.pk, [])
        group.append(budget)
    eligible, passing, incomplete = 0, 0, 0
    for group in projects.values():
        if group[0].quote.project.status != "done" or not all(
            b.finalized for b in group
        ):
            incomplete += 1
            continue
        baseline = margin(sum(b.revenue for b in group), sum(b.cost for b in group))
        actual = margin(
            sum(b.actual_revenue for b in group), sum(b.actual_cost for b in group)
        )
        if baseline is None or actual is None:
            incomplete += 1
            continue
        eligible += 1
        passing += baseline - actual <= 5
    return {
        "eligible": eligible,
        "passing": passing,
        "incomplete": incomplete,
        "percent": rounded(Decimal(passing) * 100 / eligible) if eligible else None,
        "target": 90,
    }


def calendar_command(user, calendar, action, data, now):
    require_role(user, "РОП", "Директор")
    if action == "fill_ru":
        from .ru_calendar import russian_calendar

        return calendar_command(
            user,
            calendar,
            "fill",
            {**russian_calendar(data["year"]), "reason": data["reason"]},
            now,
        )
    if action != "fill":
        raise ValidationError("Неизвестная операция календаря.")
    start, end = data["start"], data["end"]
    if end < start or (end - start).days > 1096:
        raise ValidationError("Выберите диапазон не более трёх лет.")
    if calendar.calendarday_set.filter(date__range=(start, end)).exists():
        raise ValidationError(
            "Диапазон содержит уже установленные даты. Существующий календарь не перезаписывается."
        )

    def dates(value):
        from datetime import date

        try:
            result = {
                date.fromisoformat(d.strip())
                for d in value.replace(",", "\n").splitlines()
                if d.strip()
            }
        except ValueError:
            raise ValidationError(
                "Даты исключений задаются как ГГГГ-ММ-ДД, по одной на строку."
            )
        if any(d < start or d > end for d in result):
            raise ValidationError("Исключения должны быть внутри выбранного диапазона.")
        return result

    off, on = dates(data.get("holidays", "")), dates(data.get("extra_workdays", ""))
    if off & on:
        raise ValidationError(
            "Одна дата не может быть одновременно рабочей и выходной."
        )
    weekdays = {int(v) for v in data["weekdays"]}
    rows = []
    for i in range((end - start).days + 1):
        day = start + timedelta(days=i)
        rows.append(
            CalendarDay(
                calendar=calendar,
                date=day,
                working=day in on or (day.weekday() in weekdays and day not in off),
            )
        )
    CalendarDay.objects.bulk_create(rows)
