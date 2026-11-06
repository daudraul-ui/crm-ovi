import uuid
from pathlib import Path

from django.conf import settings
from django.core.validators import FileExtensionValidator, RegexValidator
from django.db import models


class Named(models.Model):
    name = models.CharField("Название", max_length=255)

    class Meta:
        abstract = True
        ordering = ["name"]

    def __str__(self):
        return self.name


class Company(Named):
    inn = models.CharField(
        "ИНН",
        max_length=12,
        blank=True,
        validators=[RegexValidator(r"^(\d{10}|\d{12})$", "ИНН: 10 или 12 цифр")],
    )
    details = models.TextField("Реквизиты", blank=True)
    email = models.EmailField("Email", blank=True)
    phone = models.CharField("Телефон", max_length=40, blank=True)
    manager = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, verbose_name="Менеджер"
    )
    financial_notes = models.TextField("Финансовая информация", blank=True)

    class Meta(Named.Meta):
        permissions = [
            ("view_finance", "Просмотр и изменение финансовой информации"),
            ("manage_employees", "Создание сотрудников и назначение ролей"),
        ]


class Contact(Named):
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="contacts",
        verbose_name="Компания",
    )
    position = models.CharField("Должность", max_length=120, blank=True)
    phone = models.CharField("Телефон", max_length=40, blank=True)
    email = models.EmailField(blank=True)


class Chain(Named):
    companies = models.ManyToManyField(
        Company,
        blank=True,
        related_name="chains",
        verbose_name="Клиенты (юридические лица)",
    )

    def validate_companies(self, ids):
        from django.core.exceptions import ValidationError

        if (
            self.pk
            and Store.objects.filter(chain=self, company__isnull=False)
            .exclude(company_id__in=ids)
            .exists()
        ):
            raise ValidationError(
                {
                    "companies": "Юридическое лицо используется магазином этой сети. Сначала измените клиента в магазине."
                }
            )


def region_key(name):
    return "".join(name.split()).casefold()


class Region(Named):
    normalized_name = models.CharField(
        max_length=255, unique=True, null=True, blank=True, editable=False
    )
    canonical = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="legacy_aliases",
        editable=False,
    )
    legacy_data = models.JSONField(default=dict, blank=True, editable=False)

    class Meta(Named.Meta):
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(canonical__isnull=True, normalized_name__isnull=False)
                    | models.Q(canonical__isnull=False, normalized_name__isnull=True)
                ),
                name="region_canonical_key",
            )
        ]

    def clean(self):
        from django.core.exceptions import ValidationError

        self.name = " ".join(self.name.split())
        if not self.name:
            raise ValidationError({"name": "Введите название региона"})
        self.normalized_name = None if self.canonical_id else region_key(self.name)
        duplicate = Region.objects.filter(normalized_name=self.normalized_name).exclude(
            pk=self.pk
        )
        if self.normalized_name and duplicate.exists():
            raise ValidationError(
                {
                    "name": "Регион с таким названием уже существует (регистр и пробелы не учитываются)"
                }
            )

    def save(self, *args, **kwargs):
        self.normalized_name = None if self.canonical_id else region_key(self.name)
        return super().save(*args, **kwargs)


class StoreFormat(Named):
    chain = models.ForeignKey(Chain, on_delete=models.CASCADE, verbose_name="Сеть")


class Store(Named):
    company = models.ForeignKey(
        Company,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="stores",
        verbose_name="Клиент (юридическое лицо)",
    )
    chain = models.ForeignKey(Chain, on_delete=models.PROTECT, verbose_name="Сеть")
    region = models.ForeignKey(
        Region, null=True, blank=True, on_delete=models.PROTECT, verbose_name="Регион"
    )
    format = models.ForeignKey(
        StoreFormat,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        verbose_name="Формат",
    )
    code = models.CharField("Код", max_length=80)
    address = models.TextField("Адрес")

    class Meta(Named.Meta):
        constraints = [
            models.UniqueConstraint(fields=["chain", "code"], name="store_chain_code")
        ]

    def clean(self):
        from django.core.exceptions import ValidationError

        if self.format_id and self.format.chain_id != self.chain_id:
            raise ValidationError({"format": "Выберите формат из той же сети"})
        if self.company_id and (
            not self.chain_id
            or not self.chain.companies.filter(pk=self.company_id).exists()
        ):
            raise ValidationError(
                {"company": "Юридическое лицо не связано с выбранной торговой сетью"}
            )
        if self.region_id and self.region.canonical_id:
            raise ValidationError({"region": "Выберите регион из общего справочника"})


class Project(Named):
    class Status(models.TextChoices):
        NEW = "new", "Новый"
        ACTIVE = "active", "В работе"
        PAUSED = "paused", "Приостановлен"
        DONE = "done", "Завершён"

    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, verbose_name="Клиент"
    )
    manager = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, verbose_name="Менеджер"
    )
    stores = models.ManyToManyField(Store, blank=True, verbose_name="Магазины")
    status = models.CharField(
        "Статус", max_length=20, choices=Status.choices, default=Status.NEW
    )
    description = models.TextField("Описание", blank=True)


class Task(Named):
    project = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.PROTECT, verbose_name="Проект"
    )
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, verbose_name="Ответственный"
    )
    due_at = models.DateTimeField("Срок")
    priority = models.CharField(
        "Приоритет",
        max_length=10,
        choices=[("low", "Низкий"), ("normal", "Обычный"), ("high", "Высокий")],
        default="normal",
    )
    status = models.CharField(
        "Статус",
        max_length=10,
        choices=[("new", "Новая"), ("active", "В работе"), ("done", "Выполнена")],
        default="new",
    )

    @property
    def overdue(self):
        from django.utils import timezone

        return self.status != "done" and self.due_at < timezone.now()


class Comment(models.Model):
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="comments",
        verbose_name="Проект",
    )
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    text = models.TextField("Комментарий")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.text[:80]


def private_path(instance, filename):
    return f"documents/{uuid.uuid4().hex}{Path(filename).suffix.lower()}"


class Document(models.Model):
    name = models.CharField("Название", max_length=255)
    company = models.ForeignKey(
        Company, null=True, blank=True, on_delete=models.PROTECT, verbose_name="Клиент"
    )
    project = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.PROTECT, verbose_name="Проект"
    )
    file = models.FileField(
        "Файл",
        upload_to=private_path,
        validators=[
            FileExtensionValidator(["pdf", "xlsx", "docx", "txt", "png", "jpg", "jpeg"])
        ],
    )
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    def clean(self):
        from django.core.exceptions import ValidationError

        if not self.company_id and not self.project_id:
            raise ValidationError("Укажите клиента или проект")
        if (
            self.company_id
            and self.project_id
            and self.project.company_id != self.company_id
        ):
            raise ValidationError("Проект принадлежит другому клиенту")
        if self.file and self.file.size > 10 * 1024 * 1024:
            raise ValidationError("Максимум 10 МБ")


class Notification(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    text = models.CharField(max_length=500)
    read = models.BooleanField(default=False)
    key = models.CharField(max_length=150, unique=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.text


class AuditEvent(models.Model):
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    action = models.CharField(max_length=50)
    model = models.CharField(max_length=80)
    object_id = models.CharField(max_length=80)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
