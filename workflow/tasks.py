from zoneinfo import ZoneInfo

from celery import shared_task
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Event, Incident, Quote, SupplierOrder
from .services import business_due, elapsed, leaders, lock_writes, moment, notify


def alert(key, project, name, users, date, critical=False):
    incident, _ = Incident.objects.get_or_create(
        key=key, defaults={"project": project, "name": name}
    )
    incident.critical = critical
    incident.closed = False
    incident.save()
    notify(
        users,
        ("КРИТИЧЕСКОЕ: " if critical else "") + name,
        f"incident:{incident.pk}:{date}",
    )


@shared_task
@transaction.atomic
def monitor():
    lock_writes()
    now = timezone.now()
    errors = []
    for order in SupplierOrder.objects.exclude(authorized_at=None).select_related(
        "calendar", "specification__project__manager"
    ):
        calendar = order.calendar
        date = now.astimezone(ZoneInfo(calendar.timezone)).date()
        try:
            day = calendar.calendarday_set.filter(date=date).first()
            if day is None:
                raise ValidationError(f"Не задан день {date}")
            if not day.working or now < moment(calendar, date):
                continue
            users = [order.project.manager]
            paused = order.pause_until and order.pause_until > now
            if order.pause_until and not paused:
                order.pause_until = None
                order.pause_requested = False
                order.save()
                Event.objects.create(
                    model=order._meta.label, object_id=order.pk, action="pause_expired"
                )
            if not paused:
                if not order.sent_at and order.due_at <= now:
                    critical = now >= business_due(calendar, order.due_at, 1)
                    alert(
                        f"placement:{order.pk}",
                        order.project,
                        f"Заказ {order}: размещение просрочено.",
                        users
                        + list(leaders("РОП"))
                        + (list(leaders("Директор")) if critical else []),
                        date,
                        critical,
                    )
                elif order.sent_at and order.status != "confirmed":
                    days = elapsed(calendar, order.sent_at, now)
                    if days >= 1:
                        alert(
                            f"confirmation:{order.pk}",
                            order.project,
                            f"Заказ {order}: не все позиции подтверждены поставщиком.",
                            users
                            + (list(leaders("РОП")) if days >= 2 else [])
                            + (list(leaders("Директор")) if days >= 3 else []),
                            date,
                            days >= 3,
                        )
            # A production pause never hides the outstanding money check.
            if order.conditions.filter(payment=True, received=False).exists():
                days = elapsed(calendar, order.authorized_at, now)
                if days >= 1:
                    alert(
                        f"payment:{order.pk}",
                        order.project,
                        f"Заказ {order}: фактическое поступление денег ещё не подтверждено.",
                        users + (list(leaders("РОП")) if days >= 2 else []),
                        date,
                    )
            else:
                Incident.objects.filter(key=f"payment:{order.pk}").update(closed=True)
        except ValidationError as exc:
            errors.append(f"{order}: {exc}")
            notify(
                leaders(),
                f"Контроль SLA заказа {order} требует настройки календаря: {exc}",
                f"calendar:{calendar.pk}:{date}",
            )
    for quote in Quote.objects.exclude(submitted_at=None).select_related(
        "calendar", "specification__project__manager", "followup"
    ):
        calendar = quote.calendar
        date = now.astimezone(ZoneInfo(calendar.timezone)).date()
        try:
            day = calendar.calendarday_set.filter(date=date).first()
            if day is None:
                raise ValidationError(f"Не задан день {date}")
            if not day.working or now < moment(calendar, date):
                continue
            if quote.status in ("pending", "discussion"):
                days = elapsed(calendar, quote.submitted_at, now)
                if days >= calendar.quote_reminder_days:
                    alert(
                        f"quote:{quote.pk}",
                        quote.project,
                        f"КП {quote}: НЕ СОГЛАСОВАНО.",
                        [quote.project.manager, *leaders()],
                        date,
                        days > calendar.quote_reminder_days,
                    )
            if (
                quote.followup
                and quote.followup.status != "done"
                and quote.followup.due_at <= now
            ):
                alert(
                    f"followup:{quote.pk}",
                    quote.project,
                    f"Просрочен следующий контакт по КП {quote}.",
                    [quote.followup.assignee, *leaders("РОП")],
                    date,
                )
            elif quote.followup:
                Incident.objects.filter(key=f"followup:{quote.pk}").update(closed=True)
        except ValidationError as exc:
            errors.append(f"{quote}: {exc}")
            notify(
                leaders(),
                f"Контроль КП {quote} требует настройки календаря: {exc}",
                f"calendar:{calendar.pk}:{date}",
            )
    return {"calendar_errors": errors}
