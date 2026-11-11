from datetime import time, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from crm.models import ConcurrentRecord


def money(label, **kwargs):
    return models.DecimalField(
        label,
        max_digits=16,
        decimal_places=2,
        validators=[MinValueValidator(0)],
        **kwargs,
    )


def first_report_date():
    return timezone.localdate() + timedelta(days=14)


class Record(ConcurrentRecord):
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True
        ordering = ["-pk"]

    def __str__(self):
        return getattr(self, "name", "") or f"{self._meta.verbose_name} #{self.pk}"


class Calendar(Record):
    first_report_on = models.DateField(
        "Первый сводный SLA-отчёт (далее каждые 14 дней)", default=first_report_date
    )
    name = models.CharField("Название календаря", max_length=120)
    timezone = models.CharField(
        "Часовой пояс IANA", max_length=60, default="Europe/Moscow"
    )
    check_at = models.TimeField("Время ежедневной проверки", default=time(10))
    max_pause_days = models.PositiveIntegerField(
        "Максимальная пауза (календарных дней)",
        validators=[MinValueValidator(1)],
        default=4,
    )
    quote_reminder_days = models.PositiveIntegerField(
        "Первое напоминание КП (рабочих дней)",
        validators=[MinValueValidator(1)],
        default=2,
    )

    def clean(self):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValidationError({"timezone": "Неизвестный часовой пояс."})

    class Meta(Record.Meta):
        verbose_name = "Рабочий календарь"
        permissions = [("view_economics", "Просмотр коммерческой экономики")]


class CalendarDay(Record):
    calendar = models.ForeignKey(Calendar, on_delete=models.PROTECT)
    date = models.DateField("Дата")
    working = models.BooleanField("Рабочий день")

    class Meta(Record.Meta):
        verbose_name = "День календаря"
        constraints = [
            models.UniqueConstraint(
                fields=["calendar", "date"], name="workflow_calendar_date"
            )
        ]


class SlaReport(Record):
    calendar = models.ForeignKey(Calendar, on_delete=models.PROTECT)
    period_start = models.DateField("Начало периода")
    period_end = models.DateField("Конец периода")
    summary = models.JSONField(default=dict)

    class Meta(Record.Meta):
        verbose_name = "Сводный SLA-отчёт"
        constraints = [
            models.UniqueConstraint(
                fields=["calendar", "period_end"], name="workflow_sla_period_unique"
            )
        ]


class Inquiry(Record):
    name = models.CharField("Обращение", max_length=255)
    project = models.ForeignKey("crm.Project", on_delete=models.PROTECT)
    channel = models.CharField("Канал / источник", max_length=120)
    contact = models.ForeignKey(
        "crm.Contact", on_delete=models.PROTECT, null=True, blank=True
    )
    received_at = models.DateTimeField("Дата обращения")
    need = models.TextField("Потребность")
    next_step = models.CharField("Следующий шаг", max_length=255)
    next_at = models.DateTimeField("Срок следующего шага")

    def clean(self):
        if (
            self.contact_id
            and self.project_id
            and self.contact.company_id
            not in [
                self.project.company_id,
                *self.project.additional_companies.values_list("pk", flat=True),
            ]
        ):
            raise ValidationError("Контакт не относится к клиентам проекта.")

    class Meta(Record.Meta):
        verbose_name = "Обращение"


class Specification(Record):
    name = models.CharField("Название редакции ТЗ", max_length=255)
    project = models.ForeignKey("crm.Project", on_delete=models.PROTECT)
    previous = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True
    )
    source = models.TextField("Источник исходных данных")
    reliable_dimensions = models.BooleanField("Достоверные размеры", default=False)
    nonstandard = models.BooleanField("Нестандартное оборудование", default=False)
    tender_measurement = models.BooleanField(
        "Замеры по условиям тендера/клиента", default=False
    )
    requested_measurement = models.BooleanField(
        "Замеры по решению менеджера/инженера", default=False
    )
    measurement = models.ForeignKey(
        "crm.Document",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Результаты замеров",
    )
    manager_approved = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        editable=False,
        related_name="+",
    )
    engineer_approved = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        editable=False,
        related_name="+",
    )
    frozen = models.BooleanField(default=False, editable=False)

    @property
    def measurements_required(self):
        return (
            not self.reliable_dimensions
            or self.nonstandard
            or self.tender_measurement
            or self.requested_measurement
        )

    class Meta(Record.Meta):
        verbose_name = "Техническая спецификация"


class Equipment(Record):
    specification = models.ForeignKey(
        Specification, on_delete=models.PROTECT, related_name="items"
    )
    store = models.ForeignKey("crm.Store", on_delete=models.PROTECT)
    name = models.CharField("Позиция", max_length=255)
    manufacturer = models.CharField("Производитель", max_length=120)
    model = models.CharField("Модель", max_length=120)
    equipment_type = models.CharField("Тип оборудования", max_length=120)
    dimensions = models.CharField("Размеры и единицы измерения", max_length=255)
    quantity = models.PositiveIntegerField(
        "Количество", validators=[MinValueValidator(1)]
    )
    glazing = models.CharField("Тип остекления", max_length=120)
    accessories = models.TextField("Дополнительные комплекты", blank=True)
    evidence = models.ForeignKey(
        "crm.Document",
        on_delete=models.PROTECT,
        verbose_name="Фото / схема / исходные данные",
    )

    class Meta(Record.Meta):
        verbose_name = "Позиция оборудования"


class Quote(Record):
    name = models.CharField("КП / редакция", max_length=255)
    specification = models.ForeignKey(Specification, on_delete=models.PROTECT)
    calendar = models.ForeignKey(Calendar, on_delete=models.PROTECT)
    previous = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True
    )
    installation = models.BooleanField("Продажа с монтажом", default=True)
    vat_mode = models.CharField(
        "Отображение НДС",
        max_length=12,
        choices=[
            ("included", "Включён"),
            ("excluded", "Цена без НДС"),
            ("separate", "Отдельной строкой"),
        ],
    )
    valid_until = models.DateField("КП действительно до")
    minimum_margin = models.DecimalField(
        "Коммерческий минимум, %",
        max_digits=6,
        decimal_places=2,
        null=True,
        editable=False,
    )
    status = models.CharField(
        "Состояние согласования",
        max_length=20,
        default="draft",
        editable=False,
        choices=[
            ("draft", "Черновик"),
            ("pending", "На согласовании"),
            ("discussion", "Совместное обсуждение после отказа"),
            ("approved", "Согласовано"),
        ],
    )
    round = models.PositiveIntegerField(default=0, editable=False)
    submitted_at = models.DateTimeField(null=True, editable=False)
    artificial_split = models.BooleanField(default=False, editable=False)
    split_reason = models.TextField("Объяснение разделения заказа", blank=True)
    exception_reason = models.TextField(editable=False, blank=True)
    exception_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        editable=False,
        related_name="+",
    )
    sent_at = models.DateTimeField(null=True, editable=False)
    followup = models.ForeignKey(
        "crm.Task", on_delete=models.PROTECT, null=True, editable=False
    )

    @property
    def project(self):
        return self.specification.project

    class Meta(Record.Meta):
        verbose_name = "Коммерческое предложение"


class QuoteLine(Record):
    quote = models.ForeignKey(Quote, on_delete=models.PROTECT, related_name="lines")
    store = models.ForeignKey("crm.Store", on_delete=models.PROTECT)
    name = models.CharField("Клиентская позиция", max_length=255)
    quantity = models.DecimalField(
        "Количество",
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    unit = models.CharField("Единица", max_length=20, default="шт.")
    price = money("Цена единицы без НДС")
    vat_rate = models.DecimalField(
        "Ставка НДС, %",
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )

    class Meta(Record.Meta):
        verbose_name = "Строка клиентского КП"


class Cost(Record):
    CATEGORIES = [
        (v, v)
        for v in [
            "Остекление",
            "Адаптационные комплекты",
            "LED",
            "Доставка от поставщика",
            "Доставка на объекты",
            "Замеры и выезды",
            "Монтаж",
            "Шеф-монтаж",
            "Командировки",
            "Упаковка и материалы",
            "Резерв на риски и гарантию",
        ]
    ]
    quote = models.ForeignKey(Quote, on_delete=models.PROTECT, related_name="costs")
    category = models.CharField("Категория", max_length=50, choices=CATEGORIES)
    name = models.CharField("Статья затрат", max_length=255)
    equipment = models.ForeignKey(Equipment, on_delete=models.PROTECT)
    quantity = models.DecimalField(
        "Количество",
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    unit = models.CharField("Единица", max_length=20, default="шт.")
    price = money("Себестоимость единицы без НДС")
    source = models.CharField(
        "Источник расчёта / уникальный номер статьи", max_length=255
    )
    comment = models.TextField("Комментарий (крепёж, ЗИП, метизы)", blank=True)

    class Meta(Record.Meta):
        verbose_name = "Внутренняя калькуляция"
        constraints = [
            models.UniqueConstraint(
                fields=["quote", "source"], name="workflow_cost_source"
            )
        ]


class Allocation(Record):
    cost = models.ForeignKey(Cost, on_delete=models.PROTECT, related_name="allocations")
    line = models.ForeignKey(
        QuoteLine, on_delete=models.PROTECT, related_name="allocations"
    )
    amount = money("Распределённая себестоимость без НДС")

    class Meta(Record.Meta):
        verbose_name = "Распределение затрат"
        constraints = [
            models.UniqueConstraint(
                fields=["cost", "line"], name="workflow_allocation_unique"
            )
        ]


class Decision(Record):
    quote = models.ForeignKey(Quote, on_delete=models.PROTECT, related_name="decisions")
    round = models.PositiveIntegerField()
    role = models.CharField(max_length=20)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    approved = models.BooleanField()
    reason = models.TextField()

    class Meta(Record.Meta):
        verbose_name = "Решение согласующего"
        constraints = [
            models.UniqueConstraint(
                fields=["quote", "round", "role"], name="workflow_decision_role"
            )
        ]


class Budget(Record):
    forecast_revision = models.PositiveIntegerField(default=0, editable=False)
    quote = models.OneToOneField(Quote, on_delete=models.PROTECT)
    previous = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="replacement",
    )
    evidence = models.ForeignKey("crm.Document", on_delete=models.PROTECT)
    reason = models.TextField()
    revenue = money("Утверждённая выручка без НДС")
    cost = money("Утверждённая себестоимость без НДС")
    stores = models.JSONField(default=dict)
    forecast_cost = money("Прогноз затрат", null=True)
    actual_cost = money("Факт затрат", null=True)
    actual_revenue = money("Факт выручки", null=True)
    finalized = models.BooleanField(default=False)

    class Meta(Record.Meta):
        verbose_name = "Утверждённая экономика"


class BudgetDecision(Record):
    budget = models.ForeignKey(
        Budget, on_delete=models.PROTECT, related_name="decisions"
    )
    revision = models.PositiveIntegerField()
    role = models.CharField(max_length=20)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    approved = models.BooleanField()
    reason = models.TextField()

    class Meta(Record.Meta):
        verbose_name = "Решение по перерасходу ниже коммерческого минимума"
        constraints = [
            models.UniqueConstraint(
                fields=["budget", "revision", "role"],
                name="workflow_budget_decision_role",
            )
        ]


class SupplierOrder(Record):
    name = models.CharField("Номер заказа / партия", max_length=120)
    supplier = models.CharField("Поставщик", max_length=255)
    specification = models.ForeignKey(Specification, on_delete=models.PROTECT)
    calendar = models.ForeignKey(Calendar, on_delete=models.PROTECT)
    status = models.CharField(
        "Состояние заказа",
        max_length=20,
        default="waiting",
        editable=False,
        choices=[
            ("waiting", "Ожидает условий"),
            ("ready", "Готов к размещению"),
            ("sent", "Отправлен поставщику"),
            ("partial", "Частично подтверждён"),
            ("confirmed", "Подтверждён поставщиком"),
            ("refused", "Отказ поставщика — требуется решение"),
        ],
    )
    authorized_at = models.DateTimeField(null=True, editable=False)
    due_at = models.DateTimeField(null=True, editable=False)
    sent_at = models.DateTimeField(null=True, editable=False)
    dispatch_evidence = models.ForeignKey(
        "crm.Document",
        on_delete=models.PROTECT,
        null=True,
        editable=False,
        related_name="+",
    )
    placement_task = models.ForeignKey(
        "crm.Task", on_delete=models.PROTECT, null=True, editable=False
    )
    pause_until = models.DateTimeField(null=True, editable=False)
    pause_reason = models.TextField(blank=True, editable=False)
    pause_requested = models.BooleanField(default=False, editable=False)
    pause_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        editable=False,
        related_name="+",
    )

    @property
    def project(self):
        return self.specification.project

    class Meta(Record.Meta):
        verbose_name = "Заказ поставщику / партия"


class OrderLine(Record):
    order = models.ForeignKey(
        SupplierOrder, on_delete=models.PROTECT, related_name="lines"
    )
    equipment = models.ForeignKey(Equipment, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(
        "Количество партии", validators=[MinValueValidator(1)]
    )
    confirmed_quantity = models.PositiveIntegerField(default=0, editable=False)
    ready_on = models.DateField(null=True, editable=False)
    stop_requested = models.BooleanField(default=False, editable=False)
    stopped = models.BooleanField(default=False, editable=False)

    class Meta(Record.Meta):
        verbose_name = "Позиция производственной партии"
        constraints = [
            models.UniqueConstraint(
                fields=["order", "equipment"], name="workflow_order_equipment"
            ),
            models.CheckConstraint(
                condition=models.Q(confirmed_quantity__lte=models.F("quantity")),
                name="workflow_confirmed_limit",
            ),
        ]


class Condition(Record):
    order = models.ForeignKey(
        SupplierOrder, on_delete=models.PROTECT, related_name="conditions"
    )
    name = models.CharField("Условие запуска", max_length=255)
    payment = models.BooleanField("Платёжное условие", default=False)
    required_amount = money("Необходимая сумма с НДС", default=0)
    payer = models.CharField("Ожидаемый плательщик", max_length=255, blank=True)
    recipient = models.CharField("Ожидаемый получатель", max_length=255, blank=True)
    order_reference = models.CharField(
        "Реквизиты заказа в платеже", max_length=255, blank=True
    )
    fulfilled = models.BooleanField(default=False, editable=False)
    received = models.BooleanField(default=False, editable=False)
    evidence = models.ForeignKey(
        "crm.Document",
        on_delete=models.PROTECT,
        null=True,
        editable=False,
        related_name="+",
    )
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, editable=False
    )
    exception_reason = models.TextField(blank=True, editable=False)

    class Meta(Record.Meta):
        verbose_name = "Условие запуска"


class Confirmation(Record):
    sender = models.EmailField("Email поставщика", blank=True, default="")
    received_at = models.DateTimeField("Дата письма", null=True, blank=True)
    line = models.ForeignKey(
        OrderLine, on_delete=models.PROTECT, related_name="confirmations"
    )
    document = models.ForeignKey("crm.Document", on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    ready_on = models.DateField()
    composition = models.TextField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)

    class Meta(Record.Meta):
        verbose_name = "Подтверждение поставщика"


class Event(models.Model):
    document = models.ForeignKey(
        "crm.Document", on_delete=models.PROTECT, null=True, blank=True
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True
    )
    model = models.CharField(max_length=60)
    object_id = models.PositiveBigIntegerField()
    action = models.CharField(max_length=60)
    data = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-pk"]


class Incident(models.Model):
    key = models.CharField(max_length=150, unique=True)
    project = models.ForeignKey("crm.Project", on_delete=models.PROTECT)
    name = models.CharField(max_length=255)
    critical = models.BooleanField(default=False)
    closed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)


class Dispatch(models.Model):
    """The exact customer files and approval label at the time of dispatch."""

    quote = models.OneToOneField(
        Quote, on_delete=models.PROTECT, related_name="dispatch"
    )
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    status = models.CharField(max_length=20)
    pdf = models.BinaryField(editable=False)
    xlsx = models.BinaryField(editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
