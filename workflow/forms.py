from django import forms
from django.contrib.auth import get_user_model

from . import models as m

RESOURCES = {
    "inquiries": (
        m.Inquiry,
        [
            "name",
            "project",
            "channel",
            "contact",
            "received_at",
            "need",
            "next_step",
            "next_at",
        ],
    ),
    "specifications": (
        m.Specification,
        [
            "name",
            "project",
            "source",
            "reliable_dimensions",
            "nonstandard",
            "tender_measurement",
            "requested_measurement",
            "measurement",
        ],
    ),
    "equipment": (
        m.Equipment,
        [
            "specification",
            "store",
            "name",
            "manufacturer",
            "model",
            "equipment_type",
            "dimensions",
            "quantity",
            "glazing",
            "accessories",
            "evidence",
        ],
    ),
    "quotes": (
        m.Quote,
        [
            "name",
            "specification",
            "calendar",
            "installation",
            "vat_mode",
            "valid_until",
            "split_reason",
        ],
    ),
    "quote-lines": (
        m.QuoteLine,
        ["quote", "store", "name", "quantity", "unit", "price", "vat_rate"],
    ),
    "costs": (
        m.Cost,
        [
            "quote",
            "category",
            "name",
            "equipment",
            "quantity",
            "unit",
            "price",
            "source",
            "comment",
        ],
    ),
    "allocations": (m.Allocation, ["cost", "line", "amount"]),
    "orders": (m.SupplierOrder, ["name", "supplier", "specification", "calendar"]),
    "order-lines": (m.OrderLine, ["order", "equipment", "quantity"]),
    "conditions": (
        m.Condition,
        [
            "order",
            "name",
            "payment",
            "required_amount",
            "payer",
            "recipient",
            "order_reference",
        ],
    ),
    "calendars": (
        m.Calendar,
        [
            "name",
            "timezone",
            "check_at",
            "max_pause_days",
            "quote_reminder_days",
            "first_report_on",
        ],
    ),
    "calendar-days": (m.CalendarDay, ["calendar", "date", "working"]),
    "budgets": (m.Budget, []),
    "decisions": (m.Decision, []),
    "confirmations": (m.Confirmation, []),
    "budget-decisions": (m.BudgetDecision, []),
    "sla-reports": (m.SlaReport, []),
}
FINANCIAL = {
    "quotes",
    "quote-lines",
    "costs",
    "allocations",
    "budgets",
    "decisions",
    "budget-decisions",
}


class RecordForm(forms.ModelForm):
    expected_version = forms.IntegerField(widget=forms.HiddenInput, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .presentation import LABELS

        for name, field in self.fields.items():
            if name in LABELS:
                field.label = LABELS[name]
        if self.instance.pk:
            self.fields["expected_version"].initial = self.instance.version
        for field in self.fields.values():
            if isinstance(field, forms.DateTimeField):
                field.widget = forms.DateTimeInput(
                    attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
                )
            elif isinstance(field, forms.DateField):
                field.widget = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


def record_form(kind):
    model, fields = RESOURCES[kind]
    return forms.modelform_factory(model, form=RecordForm, fields=fields)


def text(label):
    return forms.CharField(label=label, widget=forms.Textarea(attrs={"rows": 2}))


def number(label):
    return forms.DecimalField(label=label, min_value=0, max_digits=16, decimal_places=2)


def doc():
    from crm.models import Document

    return forms.ModelChoiceField(
        label="Подтверждающий документ проекта",
        queryset=Document.objects.filter(archived=False),
    )


def date_time(label):
    return forms.DateTimeField(
        label=label, widget=forms.DateTimeInput(attrs={"type": "datetime-local"})
    )


def action_fields():
    payment = {
        "document": doc(),
        "amount": number("Проверенная сумма"),
        "payer": forms.CharField(label="Плательщик"),
        "recipient": forms.CharField(label="Получатель"),
        "order_reference": forms.CharField(label="Реквизиты заказа"),
        "execution_checked": forms.BooleanField(label="Статус исполнения проверен"),
    }
    return {
        "fill": (
            "Заполнить рабочий календарь",
            {
                "start": forms.DateField(
                    label="Начало периода",
                    widget=forms.DateInput(attrs={"type": "date"}),
                ),
                "end": forms.DateField(
                    label="Конец периода",
                    widget=forms.DateInput(attrs={"type": "date"}),
                ),
                "weekdays": forms.MultipleChoiceField(
                    label="Рабочие дни недели",
                    choices=[
                        (str(i), v)
                        for i, v in enumerate(
                            [
                                "Понедельник",
                                "Вторник",
                                "Среда",
                                "Четверг",
                                "Пятница",
                                "Суббота",
                                "Воскресенье",
                            ]
                        )
                    ],
                    widget=forms.CheckboxSelectMultiple,
                ),
                "holidays": forms.CharField(
                    label="Нерабочие исключения (ГГГГ-ММ-ДД, по одной дате)",
                    required=False,
                    widget=forms.Textarea,
                ),
                "extra_workdays": forms.CharField(
                    label="Рабочие исключения / переносы",
                    required=False,
                    widget=forms.Textarea,
                ),
                "reason": text("Основание утверждения календаря компании"),
            },
        ),
        "approve_manager": ("Подтвердить ТЗ как менеджер", {}),
        "fill_ru": (
            "Заполнить производственный календарь РФ",
            {
                "year": forms.TypedChoiceField(
                    label="Год (федеральная пятидневка)",
                    choices=[(2026, "2026"), (2027, "2027")],
                    coerce=int,
                ),
                "reason": text("Основание применения календаря РФ"),
            },
        ),
        "handover": (
            "РОП: передать проект новому менеджеру",
            {
                "new_manager": forms.ModelChoiceField(
                    label="Новый менеджер",
                    queryset=get_user_model()
                    .objects.filter(is_active=True, groups__name="Менеджер")
                    .distinct(),
                ),
                "reason": text(
                    "Основание передачи; открытые задачи поступят вам для распределения"
                ),
            },
        ),
        "approve_engineer": ("Подтвердить ТЗ как инженер", {}),
        "revise": ("Создать новую редакцию", {}),
        "policy": (
            "Установить коммерческие правила",
            {
                "minimum_margin": forms.DecimalField(
                    label="Минимальная маржа, %",
                    max_digits=6,
                    decimal_places=2,
                    min_value=-999,
                    max_value=100,
                ),
                "artificial_split": forms.BooleanField(
                    label="Заказ признан искусственно разделённым", required=False
                ),
            },
        ),
        "submit": (
            "Направить обоим руководителям",
            {
                "discussion": forms.CharField(
                    label="Результат совместного обсуждения (после отказа)",
                    required=False,
                    widget=forms.Textarea,
                )
            },
        ),
        "decide": (
            "Зарегистрировать решение",
            {
                "role": forms.ChoiceField(
                    label="Роль согласующего",
                    choices=[("РОП", "РОП"), ("Директор", "Директор")],
                ),
                "approved": forms.TypedChoiceField(
                    label="Решение",
                    choices=[("yes", "Согласовано"), ("no", "Отказ")],
                    coerce=lambda v: v == "yes",
                ),
                "reason": text("Обоснование"),
            },
        ),
        "exception": (
            "Разрешить исключение",
            {"reason": text("Причина исключения"), "document": doc()},
        ),
        "send": (
            "Зафиксировать отправку клиенту",
            {"document": doc(), "followup_at": date_time("Дата следующего контакта")},
        ),
        "baseline": (
            "Зафиксировать утверждённый заказ / изменение клиента",
            {
                "document": doc(),
                "reason": text("Основание утверждения"),
                "previous_budget": forms.ModelChoiceField(
                    label="Предыдущая база (только изменение клиента)",
                    queryset=m.Budget.objects.filter(replacement__isnull=True),
                    required=False,
                ),
                "change_type": forms.ChoiceField(
                    label="Тип изменения клиента",
                    required=False,
                    choices=[
                        ("", "Первоначальная база"),
                        ("price", "Цена"),
                        ("volume", "Объём"),
                        ("composition", "Состав"),
                    ],
                ),
            },
        ),
        "authorize": ("Разрешить запуск производства", {}),
        "dispatch": ("Зафиксировать отправку поставщику", {"document": doc()}),
        "request_pause": (
            "Запросить паузу у РОПа",
            {"reason": text("Причина задержки")},
        ),
        "pause": ("РОП: согласовать паузу", {"until": date_time("Приостановить до")}),
        "resume": ("РОП: возобновить контроль", {}),
        "reschedule": (
            "Изменить срок размещения",
            {"until": date_time("Новый срок"), "reason": text("Причина переноса")},
        ),
        "refuse": (
            "Зарегистрировать отказ поставщика",
            {"document": doc(), "reason": text("Причина отказа")},
        ),
        "payment_risk": (
            "Сообщить директору о риске неоплаты",
            {"reason": text("Основание критического риска")},
        ),
        "verify": ("Проверить условие / платёжное поручение", payment),
        "receipt": ("Подтвердить фактическое зачисление", payment),
        "confirm": (
            "Проверить письмо поставщика",
            {
                "document": doc(),
                "sender": forms.EmailField(label="Email отправителя — поставщика"),
                "received_at": date_time("Дата получения письма поставщика"),
                "quantity": forms.IntegerField(
                    label="Дополнительно подтверждённое количество", min_value=1
                ),
                "ready_on": forms.DateField(
                    label="Срок готовности",
                    widget=forms.DateInput(attrs={"type": "date"}),
                ),
                "composition": text("Подтверждённый состав"),
                "composition_checked": forms.BooleanField(
                    label="Состав письма сверён с заказом"
                ),
            },
        ),
        "request_stop": (
            "Запросить остановку позиции",
            {
                "document": doc(),
                "reason": text("Причина; дальнейшие действия с клиентом и РОПом"),
            },
        ),
        "confirm_stop": (
            "Письменно подтверждена остановка",
            {"document": doc(), "reason": text("Письменное подтверждение поставщика")},
        ),
        "forecast": (
            "Обновить прогноз / подтверждённый факт",
            {
                "forecast_cost": number("Прогноз себестоимости без НДС"),
                "actual_cost": forms.DecimalField(
                    label="Фактическая себестоимость",
                    required=False,
                    min_value=0,
                    max_digits=16,
                    decimal_places=2,
                ),
                "actual_revenue": forms.DecimalField(
                    label="Фактическая выручка без НДС",
                    required=False,
                    min_value=0,
                    max_digits=16,
                    decimal_places=2,
                ),
                "finalized": forms.BooleanField(
                    label="Финансовые данные завершены", required=False
                ),
                "document": doc(),
                "reason": text("Основание изменения прогноза/факта"),
            },
        ),
    }


ACTIONS = {
    m.Inquiry: ["handover"],
    m.Calendar: ["fill", "fill_ru"],
    m.Specification: ["approve_manager", "approve_engineer", "revise", "handover"],
    m.Quote: ["policy", "submit", "decide", "exception", "send", "revise", "baseline"],
    m.SupplierOrder: [
        "authorize",
        "dispatch",
        "request_pause",
        "pause",
        "resume",
        "reschedule",
        "refuse",
        "payment_risk",
    ],
    m.Condition: ["verify", "receipt", "exception"],
    m.OrderLine: ["confirm", "request_stop", "confirm_stop"],
    m.Budget: ["forecast", "decide"],
}


def action_form(obj, action, data=None):
    if action not in ACTIONS.get(type(obj), []):
        raise ValueError("Команда не предусмотрена для этой записи.")
    title, fields = action_fields()[action]
    if isinstance(obj, m.Quote) and action == "revise":
        fields = {
            "specification": forms.ModelChoiceField(
                label="Новое ТЗ (если меняется состав; калькуляция будет пустой)",
                queryset=m.Specification.objects.filter(project=obj.project),
                required=False,
            )
        }
    if isinstance(obj, m.Condition) and not obj.payment and action == "verify":
        fields = {"document": doc()}
    cls = type(
        "CommandForm",
        (forms.Form,),
        {"version": forms.IntegerField(widget=forms.HiddenInput), **fields},
    )
    form = cls(data=data, initial={"version": obj.version})
    return title, form


def command_data(form):
    return {
        k: (v.pk if hasattr(v, "pk") else v)
        for k, v in form.cleaned_data.items()
        if k != "version"
    }
