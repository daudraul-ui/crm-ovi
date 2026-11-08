"""Human labels and action visibility; services remain the authority for access."""

from datetime import date, datetime

from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from . import models as m
from . import services as s
from .forms import ACTIONS

LABELS = {
    "id": "Номер записи",
    "version": "Версия записи",
    "created_at": "Создано",
    "project": "Проект",
    "specification": "Техническая спецификация",
    "previous": "Предыдущая редакция",
    "calendar": "Рабочий календарь",
    "round": "Раунд согласования",
    "submitted_at": "Направлено на согласование",
    "artificial_split": "Искусственное разделение заказа",
    "exception_reason": "Основание исключения",
    "exception_by": "Исключение разрешил",
    "sent_at": "Дата отправки",
    "followup": "Следующий контакт",
    "manager_approved": "ТЗ подтвердил менеджер",
    "engineer_approved": "ТЗ подтвердил инженер",
    "frozen": "Редакция зафиксирована",
    "order": "Производственный заказ",
    "equipment": "Оборудование",
    "quote": "Коммерческое предложение",
    "store": "Магазин",
    "cost": "Себестоимость",
    "line": "Клиентская строка",
    "authorized_at": "Запуск разрешён",
    "due_at": "Срок размещения",
    "dispatch_evidence": "Подтверждение отправки",
    "placement_task": "Задача на размещение",
    "pause_until": "Пауза до",
    "pause_reason": "Причина паузы",
    "pause_requested": "Пауза запрошена",
    "pause_by": "Паузу согласовал",
    "confirmed_quantity": "Подтверждённое количество",
    "ready_on": "Срок готовности",
    "stop_requested": "Остановка запрошена",
    "stopped": "Остановка письменно подтверждена",
    "fulfilled": "Условие выполнено",
    "received": "Деньги зачислены",
    "verified_by": "Проверил",
    "evidence": "Подтверждающий документ",
    "reason": "Основание",
    "revenue": "Утверждённая выручка",
    "forecast_cost": "Прогноз себестоимости",
    "actual_cost": "Факт себестоимости",
    "actual_revenue": "Факт выручки",
    "finalized": "Финансовые данные завершены",
    "date": "Дата",
    "working": "Рабочий день",
    "actor": "Сотрудник",
    "role": "Роль",
    "approved": "Согласовано",
    "document": "Документ-основание",
    "composition": "Подтверждённый состав",
    "responsible_manager": "Ответственный менеджер (номер)",
    "new_manager": "Новый менеджер (номер)",
    "previous_manager": "Прежний менеджер (номер)",
    "tasks": "Переданные задачи (номера)",
    "rop": "Ответственный РОП (номер)",
    "minimum_margin": "Коммерческий минимум, %",
    "discussion": "Результат совместного обсуждения",
    "previous_budget": "Предыдущая утверждённая база (номер)",
    "change_type": "Изменение клиента",
    "followup_at": "Дата следующего контакта",
    "forecast_revision": "Редакция прогноза",
    "revision": "Редакция прогноза",
    "budget": "Утверждённая экономика",
}
EVENTS = {
    "save": "Сохранение",
    "submit": "Направлено на согласование",
    "decide": "Решение руководителя",
    "policy": "Коммерческие правила",
    "send": "Отправка клиенту",
    "baseline": "Фиксация утверждённого заказа",
    "baseline_created": "Создана финансовая база",
    "authorize": "Разрешён запуск",
    "dispatch": "Отправка поставщику",
    "verify": "Проверка основания",
    "receipt": "Подтверждено зачисление",
    "confirm": "Проверено письмо поставщика",
    "revise": "Новая редакция",
    "revision_created": "Создана новая редакция",
    "exception": "Разрешено исключение",
    "approve_manager": "ТЗ проверено менеджером",
    "approve_engineer": "ТЗ проверено инженером",
    "export_pdf": "Скачан PDF",
    "export_xlsx": "Скачан Excel",
    "forecast": "Обновлён прогноз/факт",
    "request_pause": "Запрошена пауза",
    "pause": "Пауза согласована",
    "resume": "Контроль возобновлён",
    "request_stop": "Остановка запрошена",
    "confirm_stop": "Остановка подтверждена",
    "fill": "Календарь утверждён",
    "handover": "Менеджер проекта изменён РОПом",
    "tasks_transferred": "Открытые задачи переданы РОПу для распределения",
}


def fields(obj):
    result = []
    for field in obj._meta.fields:
        if field.name == "stores":
            continue
        value = getattr(obj, field.name)
        if field.choices:
            value = getattr(obj, f"get_{field.name}_display")()
        elif value is None or value == "":
            value = "—"
        elif isinstance(value, bool):
            value = "Да" if value else "Нет"
        elif isinstance(value, datetime):
            value = timezone.localtime(value).strftime("%d.%m.%Y %H:%M")
        elif isinstance(value, date):
            value = value.strftime("%d.%m.%Y")
        result.append((LABELS.get(field.name, field.verbose_name), str(value)))
    return result


def can_edit(user, obj):
    try:
        s.editable(obj)
        if isinstance(obj, (m.Calendar, m.CalendarDay, m.Budget)):
            return False
        if isinstance(obj, (m.Specification, m.Equipment)) and s.role(user, "Инженер"):
            return True
        s.manager(user, obj)
        return True
    except (PermissionDenied, ValidationError):
        return False


def actions(user, obj):
    possible = list(ACTIONS.get(type(obj), []))
    managing = False
    try:
        s.manager(user, obj)
        managing = True
    except PermissionDenied:
        pass
    result = []
    for action in possible:
        allowed = managing
        if action in ("policy", "exception", "reschedule", "fill"):
            allowed = s.role(user, "РОП", "Директор")
        if action in ("pause", "resume", "handover"):
            allowed = s.role(user, "РОП")
        if action == "approve_engineer":
            allowed = s.role(user, "Инженер")
        if action == "decide":
            allowed = s.role(user, "РОП", "Директор")
        if isinstance(obj, m.OrderLine):
            allowed = user.pk == obj.order.project.manager_id and s.role(
                user, "Менеджер", "РОП", "Директор"
            )
        if not allowed:
            continue
        if isinstance(obj, m.Quote):
            if action == "policy" and obj.status != "draft":
                continue
            if action == "submit" and obj.status not in ("draft", "discussion"):
                continue
            if action == "decide" and obj.status != "pending":
                continue
            if action == "exception" and obj.status not in ("pending", "discussion"):
                continue
            if action == "send" and (
                obj.sent_at or (obj.status != "approved" and not obj.exception_by_id)
            ):
                continue
            if action == "baseline" and (
                obj.status != "approved" or m.Budget.objects.filter(quote=obj).exists()
            ):
                continue
        if isinstance(obj, m.Specification):
            if obj.frozen and action not in ("revise", "handover"):
                continue
        if isinstance(obj, m.SupplierOrder):
            if action == "authorize" and obj.authorized_at:
                continue
            if action == "dispatch" and obj.status != "ready":
                continue
            if action == "pause" and not obj.pause_requested:
                continue
            if action == "resume" and not obj.pause_until:
                continue
            if action == "reschedule" and (not obj.authorized_at or obj.sent_at):
                continue
        result.append(action)
    return result
