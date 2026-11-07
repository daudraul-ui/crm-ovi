"""Deterministic, previewed import. Ambiguities are explicit grouped decisions."""

import hashlib
import io
import json
from collections import Counter
from difflib import SequenceMatcher

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from openpyxl import Workbook, load_workbook

from .file_validation import validate_upload
from .integrity import name_key
from .models import (
    Chain,
    Company,
    Contact,
    Region,
    Store,
    StoreFormat,
    WriteLock,
    region_key,
)
from .services import audit

HEADERS = {
    "chain": "Торговая сеть",
    "company": "Клиент",
    "inn": "ИНН",
    "code": "Номер (КОД)",
    "name": "Название",
    "address": "Адрес",
    "region": "Регион",
    "format": "Формат",
    "manager": "Менеджер",
    "contact": "Контактное лицо",
    "phone": "Телефон",
    "email": "Email",
}
ALIASES = {name_key(v): k for k, v in HEADERS.items()}
for key, names in {
    "code": ["Код", "Код ТТ", "Номер магазина", "Номер ТТ"],
    "chain": ["ТС", "Сеть"],
    "company": ["ЮЛ", "Юридическое лицо"],
    "address": ["Адрес ТТ", "Адрес магазина"],
}.items():
    ALIASES.update({name_key(n): key for n in names})


def read_rows(file):
    validate_upload(file)
    file.seek(0)
    try:
        book = load_workbook(file, read_only=True, data_only=False)
    except (KeyError, IndexError, TypeError, ValueError, OSError) as exc:
        raise ValueError("Повреждённая или неподдерживаемая структура XLSX.") from exc
    try:
        if len(book.sheetnames) != 1:
            raise ValueError("Оставьте один лист с данными для импорта.")
        sheet = book.active
        if sheet.max_column > 30 or sheet.max_row > 10001:
            raise ValueError("Максимум 10 000 строк и 30 колонок.")
        rows = []
        warnings = set()
        for index, cells in enumerate(sheet.iter_rows(), 1):
            if index > 10001 or len(cells) > 30:
                raise ValueError("Превышены ограничения таблицы.")
            values = []
            for cell in cells:
                if cell.data_type == "f":
                    raise ValueError(
                        f"Строка {index}: формулы запрещены; замените их значениями."
                    )
                value = cell.value
                if isinstance(value, (int, float)):
                    warnings.add(
                        "В файле есть числовые ячейки. Для кодов и ИНН проверьте ведущие нули: исходное представление могло быть утрачено Excel."
                    )
                values.append("" if value is None else str(value).strip())
            rows.append(values)
        if not rows or not any(rows[0]):
            raise ValueError("Нет заголовков.")
        return rows, sorted(warnings)
    finally:
        book.close()


class Pending(Exception):
    pass


class Analyzer:
    def __init__(self, job):
        self.job = job
        self.questions = {}
        self.errors = []
        self.changes = []
        self.counts = Counter()
        self.cache = {}

    def question(self, key, title, options):
        key = hashlib.sha256(key.encode()).hexdigest()[:24]
        answer = self.job.answers.get(key)
        allowed = {str(v) for v, label in options}
        if answer in allowed:
            return answer
        self.questions[key] = {"key": key, "title": title, "options": options}
        raise Pending

    def match(self, model, value, extra=None, fuzzy=False):
        extra = extra or {}
        normalize = region_key if model is Region else name_key
        key = (model.__name__, normalize(value), str(extra))
        if key in self.cache:
            return self.cache[key]
        candidates = list(model.objects.filter(**extra))
        exact = [o for o in candidates if normalize(o.name) == normalize(value)]
        similar = (
            [
                o
                for o in candidates
                if SequenceMatcher(None, name_key(o.name), name_key(value)).ratio()
                >= 0.8
            ]
            if fuzzy
            else []
        )
        if len(exact) == 1:
            result = exact[0]
        elif exact or similar:
            options = [(str(o.pk), o.name) for o in exact or similar] + [
                ("skip", "Пропустить строки")
            ]
            if not exact:
                options.append(("new", "Создать новую запись"))
            choice = self.question(
                str(key), f"Сопоставить «{value}» ({model._meta.verbose_name})", options
            )
            if choice == "skip":
                raise Pending
            result = next((o for o in exact or similar if str(o.pk) == choice), None)
        else:
            result = None
        self.cache[key] = result
        return result

    def created(self, kind, obj):
        obj.full_clean()
        obj.save()
        self.counts[kind] += 1
        self.changes.append({"action": "create", "kind": kind, "name": str(obj)})
        audit(self.job.user, "import", obj)
        self.cache.clear()
        return obj

    def row(self, data, number):
        chain = None
        company = None
        if data.get("chain"):
            chain = self.match(Chain, data["chain"], fuzzy=True)
            if chain is None:
                chain = self.created("chains", Chain(name=data["chain"]))
        if data.get("company") or data.get("inn"):
            matches = (
                list(Company.objects.filter(inn=data["inn"])) if data.get("inn") else []
            )
            if len(matches) > 1:
                choice = self.question(
                    "inn:" + data["inn"],
                    "Несколько клиентов с ИНН " + data["inn"],
                    [(str(o.pk), o.name) for o in matches]
                    + [("skip", "Отложить строки")],
                )
                company = next((o for o in matches if str(o.pk) == choice), None)
                if company is None:
                    raise Pending
            elif matches:
                company = matches[0]
                if data.get("company") and name_key(company.name) != name_key(
                    data["company"]
                ):
                    choice = self.question(
                        "inn-name:" + data["inn"] + data["company"],
                        f"ИНН совпал: в CRM «{company.name}», в Excel «{data['company']}»",
                        [
                            ("use", "Использовать существующего"),
                            ("skip", "Отложить строки"),
                        ],
                    )
                    if choice == "skip":
                        raise Pending
            else:
                company = self.match(Company, data.get("company", ""), fuzzy=True)
                if company and data.get("inn") and company.inn != data["inn"]:
                    choice = self.question(
                        "different-inn:" + data["company"] + data["inn"],
                        f"У клиента «{company.name}» в CRM ИНН {company.inn or 'не указан'}, в файле {data['inn']}.",
                        [
                            ("use", "Использовать существующего без изменения ИНН"),
                            ("new", "Создать отдельного клиента"),
                            ("skip", "Отложить строки"),
                        ],
                    )
                    if choice == "skip":
                        raise Pending
                    if choice == "new":
                        company = None
            if company is None:
                users = list(
                    get_user_model()
                    .objects.filter(
                        is_active=True, groups__name__in=["Директор", "РОП", "Менеджер"]
                    )
                    .distinct()
                )
                matches = [
                    u
                    for u in users
                    if data.get("manager")
                    and name_key(data["manager"])
                    in (name_key(u.username), name_key(u.get_full_name()))
                ]
                manager = matches[0] if len(matches) == 1 else None
                if manager is None:
                    choice = self.question(
                        "manager:" + data.get("manager", ""),
                        "Выберите ответственного менеджера: "
                        + data.get("manager", "не указан"),
                        [(str(u.pk), u.get_full_name() or u.username) for u in users]
                        + [("skip", "Пропустить строки")],
                    )
                    manager = next((u for u in users if str(u.pk) == choice), None)
                    if manager is None:
                        raise Pending
                company = self.created(
                    "companies",
                    Company(
                        name=data.get("company", ""),
                        inn=data.get("inn", ""),
                        manager=manager,
                    ),
                )
            if chain and not chain.companies.filter(pk=company.pk).exists():
                chain.companies.add(company)
                self.counts["links"] += 1
                self.changes.append(
                    {"action": "link", "chain": chain.name, "company": company.name}
                )
        if data.get("contact"):
            if company is None:
                raise ValueError("Для контактного лица требуется клиент.")
            contact, created = Contact.objects.get_or_create(
                company=company,
                name=data["contact"],
                phone=data.get("phone", ""),
                email=data.get("email", ""),
            )
            contact.full_clean()
            if created:
                self.counts["contacts"] += 1
                self.changes.append(
                    {"action": "create", "kind": "contacts", "name": contact.name}
                )
                audit(self.job.user, "import", contact)
        if not data.get("code"):
            if data.get("name") or data.get("address"):
                raise ValueError("У магазина не указан код.")
            return
        if chain is None:
            raise ValueError("Для магазина обязательна торговая сеть.")
        if company is None:
            options = [(str(c.pk), c.name) for c in chain.companies.all()] + [
                ("blank", "Оставить клиента пустым"),
                ("skip", "Пропустить строки"),
            ]
            choice = self.question(
                "store-company:" + chain.name,
                f"Клиент магазинов сети «{chain.name}» не указан",
                options,
            )
            if choice == "skip":
                raise Pending
            company = chain.companies.get(pk=choice) if choice != "blank" else None
        references = {}
        for field, model in [("region", Region), ("format", StoreFormat)]:
            extra = (
                {"chain": chain} if field == "format" else {"canonical__isnull": True}
            )
            value = data.get(field)
            obj = self.match(model, value, extra) if value else None
            if value and obj is None:
                obj = self.created(
                    field + "s",
                    model(
                        name=value, **({"chain": chain} if field == "format" else {})
                    ),
                )
            elif not value:
                choice = self.question(
                    field + ":blank:" + (chain.name if field == "format" else ""),
                    HEADERS[field] + " не указан",
                    [("blank", "Оставить пустым")]
                    + [(str(o.pk), o.name) for o in model.objects.filter(**extra)],
                )
                obj = None if choice == "blank" else model.objects.get(pk=choice)
            references[field] = obj
        existing = Store.objects.filter(chain=chain, code=data["code"]).first()
        values = {
            "name": data.get("name", ""),
            "address": data.get("address", ""),
            "company": company,
            **references,
        }
        if existing:
            differences = {
                k: {"before": str(getattr(existing, k) or ""), "after": str(v or "")}
                for k, v in values.items()
                if getattr(existing, k) != v
            }
            if not differences:
                self.counts["skips"] += 1
                return
            self.changes.append(
                {
                    "action": "compare",
                    "code": existing.code,
                    "version": existing.version,
                    "differences": differences,
                }
            )
            options = [("keep", "Сохранить существующие записи")]
            if self.job.mode == "update":
                options += [
                    ("update", "Обновить только непустыми значениями"),
                    ("replace", "Обновить, включая очистку пустыми значениями"),
                ]
            choice = self.question(
                "existing-store:" + chain.name,
                "Существующие магазины сети «" + chain.name + "»: проверьте различия",
                options,
            )
            if choice == "keep":
                self.counts["skips"] += 1
            else:
                for key, value in values.items():
                    if value or choice == "replace":
                        setattr(existing, key, value)
                existing.full_clean()
                existing.save()
                self.counts["updates"] += 1
                audit(self.job.user, "import_update", existing)
        else:
            self.created("stores", Store(chain=chain, code=data["code"], **values))


def analyze(job, commit=False):
    rows, warnings = read_rows(job.file)
    mapping = job.mapping
    targets = [v for v in mapping.values() if v != "ignore"]
    if len(targets) != len(set(targets)) or any(v not in HEADERS for v in targets):
        raise ValueError("Одно поле CRM можно сопоставить только одной колонке.")
    if len(mapping) != len(rows[0]):
        raise ValueError("Сопоставьте каждую колонку или выберите «Пропустить».")
    analyzer = Analyzer(job)
    with transaction.atomic():
        if commit:
            WriteLock.objects.get_or_create(name="crm")
            WriteLock.objects.select_for_update().get(name="crm")
        for number, row in enumerate(rows[1:], 2):
            if not any(row):
                continue
            analyzer.counts["rows"] += 1
            data = {
                target: row[int(index)] if int(index) < len(row) else ""
                for index, target in mapping.items()
                if target != "ignore"
            }
            override = job.row_overrides.get(str(number), {})
            if override.get("skip") == "1":
                analyzer.counts["skips"] += 1
                continue
            data.update(
                {
                    key: str(value).strip()
                    for key, value in override.items()
                    if key in HEADERS
                }
            )
            prior_counts = analyzer.counts.copy()
            prior_changes = len(analyzer.changes)
            try:
                with transaction.atomic():
                    analyzer.row(data, number)
            except Pending:
                analyzer.counts = prior_counts
                comparisons = [
                    c
                    for c in analyzer.changes[prior_changes:]
                    if c.get("action") == "compare"
                ]
                analyzer.changes[prior_changes:] = comparisons
                analyzer.cache.clear()
                analyzer.counts["pending_or_skipped"] += 1
            except (ValidationError, ValueError) as exc:
                analyzer.counts = prior_counts
                del analyzer.changes[prior_changes:]
                analyzer.cache.clear()
                analyzer.errors.append(f"Строка {number}: {exc}")
        result = {
            "counts": dict(analyzer.counts),
            "questions": list(analyzer.questions.values()),
            "errors": analyzer.errors,
            "changes": analyzer.changes,
            "warnings": warnings,
        }
        for key in (
            "rows",
            "companies",
            "chains",
            "regions",
            "formats",
            "stores",
            "contacts",
            "links",
            "updates",
            "skips",
        ):
            result["counts"].setdefault(key, 0)
        result["counts"]["errors"] = len(analyzer.errors)
        result["counts"]["questions"] = len(analyzer.questions)
        result["fingerprint"] = hashlib.sha256(
            json.dumps(result, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        if commit:
            if analyzer.questions or analyzer.errors or job.mode == "check":
                raise ValueError(
                    "Импорт не подтверждён: устраните ошибки и ответьте на вопросы."
                )
            if result["fingerprint"] != job.preview.get("fingerprint"):
                raise ValueError(
                    "Данные CRM изменились после предпросмотра. Повторите анализ."
                )
        else:
            transaction.set_rollback(True)
    return result


def workbook_bytes(headers, rows):
    book = Workbook(write_only=True)
    sheet = book.create_sheet("Данные")
    from openpyxl.cell import WriteOnlyCell

    for values in [headers]:
        sheet.append(values)
    for row in rows:
        cells = []
        for value in row:
            cell = WriteOnlyCell(sheet, value="" if value is None else str(value))
            cell.data_type = "s"
            cell.number_format = "@"
            cells.append(cell)
        sheet.append(cells)
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()
