import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .excel import ALIASES, HEADERS, analyze, read_rows, workbook_bytes
from .integrity import name_key
from .models import (
    AuditEvent,
    Chain,
    Company,
    Contact,
    ImportJob,
    Region,
    Store,
    StoreFormat,
)
from .registers import filtered
from .security import allowed

MODELS = {
    "companies": Company,
    "contacts": Contact,
    "chains": Chain,
    "regions": Region,
    "formats": StoreFormat,
    "stores": Store,
}

COUNT_LABELS = {
    "rows": "Строки",
    "companies": "Новые клиенты",
    "chains": "Новые сети",
    "regions": "Новые регионы",
    "formats": "Новые форматы",
    "stores": "Новые магазины",
    "contacts": "Новые контакты",
    "links": "Новые связи",
    "updates": "Обновления",
    "skips": "Пропущено",
    "pending_or_skipped": "Ожидают решения или отложены",
    "errors": "Ошибки",
    "questions": "Вопросы",
}


def require_import(user):
    if not all(
        allowed(user, model, action)
        for model in MODELS.values()
        for action in ("view", "add")
    ):
        raise PermissionDenied


def import_excel(request, job_id=None):
    require_import(request.user)
    job = get_object_or_404(ImportJob, pk=job_id, user=request.user) if job_id else None
    error = None
    try:
        if job is None and request.method == "POST":
            upload = request.FILES.get("file")
            if not upload or not upload.name.lower().endswith(".xlsx"):
                raise ValueError("Выберите файл .xlsx.")
            rows, warnings = read_rows(upload)
            mode = request.POST.get("mode", "create")
            if mode not in ("create", "update", "check"):
                raise ValueError("Неизвестный режим.")
            if mode == "update" and not all(
                allowed(request.user, model, "change") for model in MODELS.values()
            ):
                raise PermissionDenied
            job = ImportJob(
                user=request.user, original_name=upload.name[:255], mode=mode
            )
            upload.seek(0)
            job.file.save(
                str(uuid.uuid4()) + ".xlsx", ContentFile(upload.read()), save=False
            )
            job.mapping = {
                str(i): ALIASES[name_key(h)]
                for i, h in enumerate(rows[0])
                if name_key(h) in ALIASES
            }
            job.save()
            return redirect("import_job", job_id=job.pk)
        if job:
            rows, warnings = read_rows(job.file)
            if request.method == "POST" and not job.committed_at:
                if job.mode == "update" and not all(
                    allowed(request.user, model, "change") for model in MODELS.values()
                ):
                    raise PermissionDenied
                if request.POST.get("action") == "commit":
                    result = analyze(job, commit=True)
                    job.preview = result
                    job.committed_at = timezone.now()
                    job.save()
                    AuditEvent.objects.create(
                        actor=request.user,
                        action="import_committed",
                        model="crm.ImportJob",
                        object_id=str(job.pk),
                        after=result["counts"],
                    )
                else:
                    new_mapping = {
                        str(i): request.POST.get("column_" + str(i), "")
                        for i in range(len(rows[0]))
                    }
                    mapping_changed = new_mapping != job.mapping
                    if mapping_changed:
                        job.row_overrides = {}
                        job.answers = {}
                    job.mapping = new_mapping
                    job.answers.update(
                        {
                            k[7:]: v
                            for k, v in request.POST.items()
                            if k.startswith("answer_") and v
                        }
                    )
                    for key, value in request.POST.items():
                        if key.startswith("row_") and not mapping_changed:
                            parts = key.split("_", 2)
                            if (
                                len(parts) == 3
                                and parts[1].isdigit()
                                and parts[2] in (*HEADERS, "skip")
                            ):
                                job.row_overrides.setdefault(parts[1], {})[parts[2]] = (
                                    value
                                )
                    job.preview = analyze(job)
                    job.save()
            columns = [
                {"index": i, "label": h, "selected": job.mapping.get(str(i), "")}
                for i, h in enumerate(rows[0])
            ]
            page = Paginator(list(enumerate(rows[1:], 2)), 50).get_page(
                request.GET.get("page")
            )
            editable_rows = []
            for number, row in page:
                data = {
                    target: row[int(i)] if int(i) < len(row) else ""
                    for i, target in job.mapping.items()
                    if target in HEADERS
                }
                data.update(job.row_overrides.get(str(number), {}))
                editable_rows.append(
                    {
                        "number": number,
                        "fields": [
                            (key, label, data.get(key, ""))
                            for key, label in HEADERS.items()
                        ],
                        "skip": data.get("skip", "0"),
                    }
                )
            preview = dict(job.preview)
            changes_page = Paginator(job.preview.get("changes", []), 100).get_page(
                request.GET.get("changes_page")
            )
            preview["changes"] = changes_page
            return render(
                request,
                "crm/excel.html",
                {
                    "job": job,
                    "columns": columns,
                    "headers": HEADERS.items(),
                    "preview": preview,
                    "warnings": warnings,
                    "error": error,
                    "editable_rows": editable_rows,
                    "page": page,
                    "changes_page": changes_page,
                    "visible_questions": preview.get("questions", [])[:50],
                    "count_labels": [
                        (COUNT_LABELS.get(key, key), value)
                        for key, value in preview.get("counts", {}).items()
                    ],
                },
            )
    except (ValueError, ValidationError, OSError) as exc:
        error = str(exc)
    columns = (
        [
            {"index": i, "label": h, "selected": job.mapping.get(str(i), "")}
            for i, h in enumerate(rows[0])
        ]
        if job and "rows" in locals()
        else []
    )
    return render(
        request,
        "crm/excel.html",
        {
            "job": job,
            "error": error,
            "headers": HEADERS.items(),
            "columns": columns,
            "preview": job.preview if job else {},
        },
    )


def xlsx_response(data, name):
    response = HttpResponse(
        data,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{name}.xlsx"'
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def import_source(request, job_id):
    from django.http import FileResponse

    require_import(request.user)
    job = get_object_or_404(ImportJob, pk=job_id, user=request.user)
    AuditEvent.objects.create(
        actor=request.user,
        action="import_source_download",
        model="crm.ImportJob",
        object_id=str(job.pk),
    )
    response = FileResponse(
        job.file.open("rb"),
        as_attachment=True,
        filename="import-source.xlsx",
        content_type="application/octet-stream",
    )
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def import_history(request):
    require_import(request.user)
    page = Paginator(
        ImportJob.objects.filter(user=request.user).order_by("-created_at"), 50
    ).get_page(request.GET.get("page"))
    return render(request, "crm/import_history.html", {"page": page})


def template(request):
    require_import(request.user)
    return xlsx_response(
        workbook_bytes(list(HEADERS.values()), []), "crm-import-template"
    )


def export(request, kind):
    model = MODELS.get(kind)
    if not model or not allowed(request.user, model):
        raise PermissionDenied
    if model in (Store, Contact, Chain) and not allowed(request.user, Company):
        raise PermissionDenied
    specs = {
        "companies": [("name", "Название"), ("inn", "ИНН"), ("manager", "Менеджер")],
        "contacts": [
            ("name", "Контактное лицо"),
            ("company", "Клиент"),
            ("phone", "Телефон"),
            ("email", "Email"),
        ],
        "chains": [("name", "Торговая сеть")],
        "regions": [("name", "Регион")],
        "formats": [("name", "Формат"), ("chain", "Торговая сеть")],
        "stores": [
            ("code", "Номер (КОД)"),
            ("name", "Название"),
            ("address", "Адрес"),
            ("region", "Регион"),
            ("format", "Формат"),
            ("chain", "Торговая сеть"),
            ("company", "Клиент"),
        ],
    }
    columns = specs[kind]
    qs = filtered(model, request.GET, request.user)

    def rows():
        for obj in qs.iterator(chunk_size=1000):
            yield [getattr(obj, field) for field, label in columns]

    return xlsx_response(
        workbook_bytes([label for field, label in columns], rows()), kind
    )
