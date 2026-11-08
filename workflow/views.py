import json

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models.deletion import ProtectedError
from django.forms.models import model_to_dict
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from crm.api import Conflict

from . import presentation, services
from .forms import FINANCIAL, RESOURCES, action_form, command_data, record_form
from .models import Budget, Event, Incident, Quote, SlaReport, SupplierOrder


def access(request, kind):
    if kind not in RESOURCES:
        raise Http404
    if (
        not request.user.is_active
        or not request.user.is_authenticated
        or not request.user.is_verified()
    ):
        raise PermissionDenied
    services.require_role(request.user, "Менеджер", "Инженер", "РОП", "Директор")
    if not services.role(request.user, "Менеджер", "РОП", "Директор") and kind not in (
        "specifications",
        "equipment",
    ):
        raise PermissionDenied(
            "Инженеру доступны технические спецификации и оборудование."
        )
    if kind in FINANCIAL and not request.user.has_perm("workflow.view_economics"):
        raise PermissionDenied("Нет права на себестоимость и маржу.")
    return RESOURCES[kind][0]


def link(obj):
    for kind, (model, _) in RESOURCES.items():
        if type(obj) is model:
            return reverse("workflow-detail", args=[kind, obj.pk])
    return ""


def public_data(obj):
    return {f.name: getattr(obj, f.attname) for f in obj._meta.fields}


def menu(user):
    return [
        (k, m._meta.verbose_name)
        for k, (m, _) in RESOURCES.items()
        if (k not in FINANCIAL or user.has_perm("workflow.view_economics"))
        and (
            services.role(user, "Менеджер", "РОП", "Директор")
            or k in ("specifications", "equipment")
        )
    ]


def dashboard(request):
    access(request, "specifications")
    commercial = services.role(request.user, "Менеджер", "РОП", "Директор")
    return render(
        request,
        "workflow/dashboard.html",
        {
            "menu": menu(request.user),
            "incidents": Incident.objects.filter(closed=False).select_related(
                "project"
            )[:100]
            if commercial
            else [],
            "orders": SupplierOrder.objects.exclude(status="confirmed")[:30]
            if commercial
            else [],
            "quotes": Quote.objects.filter(status__in=["pending", "discussion"])[:30]
            if request.user.has_perm("workflow.view_economics")
            else [],
            "kpi": services.kpi()
            if request.user.has_perm("workflow.view_economics")
            else None,
        },
    )


def listing(request, kind):
    model = access(request, kind)
    query = model.objects.all()
    search = request.GET.get("q", "").strip()
    if search and any(f.name == "name" for f in model._meta.fields):
        query = query.filter(name__icontains=search)
    page = Paginator(query, 30).get_page(request.GET.get("page"))
    return render(
        request,
        "workflow/list.html",
        {
            "kind": kind,
            "title": model._meta.verbose_name,
            "page": page,
            "can_create": bool(RESOURCES[kind][1]),
            "menu": menu(request.user),
        },
    )


@require_http_methods(["GET", "POST"])
def edit(request, kind, pk=None):
    model = access(request, kind)
    if not RESOURCES[kind][1]:
        raise PermissionDenied
    obj = get_object_or_404(model, pk=pk) if pk else model()
    form = record_form(kind)(request.POST or None, instance=obj)
    status = 200
    if request.method == "POST" and form.is_valid():
        try:
            saved = services.save_record(
                request.user,
                form.save(commit=False),
                form.cleaned_data.get("expected_version"),
            )
            return redirect(link(saved))
        except Conflict:
            form.add_error(
                None, "Запись изменена другим сотрудником. Обновите страницу."
            )
            status = 409
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
            status = 400
    return render(
        request,
        "workflow/form.html",
        {"form": form, "title": model._meta.verbose_name, "kind": kind},
        status=status,
    )


def detail(request, kind, pk):
    model = access(request, kind)
    obj = get_object_or_404(model, pk=pk)
    related = []
    # Discover only declared workflow children; no unrelated user/session relations.
    for child_kind, (child_model, _) in RESOURCES.items():
        if child_kind in FINANCIAL and not request.user.has_perm(
            "workflow.view_economics"
        ):
            continue
        for field in child_model._meta.fields:
            if (
                field.is_relation
                and field.related_model is model
                and field.name not in ("previous",)
            ):
                children = child_model.objects.filter(**{field.name: obj})[:100]
                related.append(
                    {
                        "title": child_model._meta.verbose_name,
                        "kind": child_kind,
                        "items": children,
                        "can_create": bool(RESOURCES[child_kind][1])
                        and presentation.can_edit(request.user, obj),
                    }
                )
    history = Event.objects.filter(model=obj._meta.label, object_id=obj.pk)[:100]
    return render(
        request,
        "workflow/detail.html",
        {
            "obj": obj,
            "kind": kind,
            "fields": presentation.fields(obj),
            "editable": bool(RESOURCES[kind][1])
            and presentation.can_edit(request.user, obj),
            "commands": [
                (a, action_form(obj, a)[0])
                for a in presentation.actions(request.user, obj)
            ],
            "related": related,
            "history": [
                {
                    "created_at": e.created_at,
                    "actor": e.actor,
                    "action": presentation.EVENTS.get(e.action, e.action),
                    "details": [
                        (presentation.LABELS.get(k, k), v) for k, v in e.data.items()
                    ],
                }
                for e in history
            ],
            "economics": services.economics(obj) if isinstance(obj, Quote) else None,
            "budget_review": services.budget_review(obj)
            if isinstance(obj, Budget)
            else None,
            "sla_summary": obj.summary.items() if isinstance(obj, SlaReport) else None,
            "removable": presentation.can_edit(request.user, obj)
            and kind
            in (
                "quote-lines",
                "costs",
                "allocations",
                "equipment",
                "order-lines",
                "conditions",
            ),
        },
    )


@require_http_methods(["GET", "POST"])
def execute(request, kind, pk, action):
    model = access(request, kind)
    obj = get_object_or_404(model, pk=pk)
    try:
        title, form = action_form(
            obj, action, request.POST if request.method == "POST" else None
        )
    except ValueError:
        raise Http404
    status = 200
    if request.method == "POST" and form.is_valid():
        try:
            result = services.command(
                request.user,
                model,
                pk,
                form.cleaned_data["version"],
                action,
                command_data(form),
            )
            messages.success(request, "Действие зарегистрировано.")
            return redirect(link(result))
        except Conflict:
            form.add_error(
                None, "Версия изменилась. Обновите страницу и проверьте решения."
            )
            status = 409
        except (ValidationError, Budget.DoesNotExist) as exc:
            form.add_error(
                None,
                "; ".join(exc.messages)
                if isinstance(exc, ValidationError)
                else "База не найдена.",
            )
            status = 400
    return render(
        request,
        "workflow/form.html",
        {"form": form, "title": title, "obj": obj, "kind": kind},
        status=status,
    )


@require_http_methods(["GET", "POST"])
def remove(request, kind, pk):
    model = access(request, kind)
    obj = get_object_or_404(model, pk=pk)
    error, status = "", 200
    if request.method == "POST":
        try:
            services.remove_line(
                request.user, model, pk, int(request.POST.get("version", "0"))
            )
            return redirect("workflow-list", kind=kind)
        except Conflict:
            error, status = "Запись изменилась. Обновите страницу.", 409
        except (ValidationError, ProtectedError, ValueError) as exc:
            error, status = str(exc), 400
    return render(
        request,
        "workflow/delete.html",
        {"obj": obj, "kind": kind, "error": error},
        status=status,
    )


@require_http_methods(["GET", "POST", "PATCH"])
def api(request, kind, pk=None, action=None):
    try:
        model = access(request, kind)
        obj = get_object_or_404(model, pk=pk) if pk else None
        if request.method == "GET":
            if action == "history" and obj:
                page = Paginator(
                    Event.objects.filter(model=obj._meta.label, object_id=obj.pk), 30
                ).get_page(request.GET.get("page"))
                return JsonResponse(
                    {
                        "count": page.paginator.count,
                        "results": [public_data(e) for e in page],
                    }
                )
            if action:
                return JsonResponse({"detail": "Команда требует POST."}, status=405)
            if obj:
                return JsonResponse(public_data(obj))
            page = Paginator(model.objects.all(), 30).get_page(request.GET.get("page"))
            return JsonResponse(
                {
                    "count": page.paginator.count,
                    "results": [public_data(o) for o in page],
                }
            )
        try:
            data = (
                json.loads(request.body)
                if request.content_type == "application/json"
                else request.POST.dict()
            )
        except (ValueError, UnicodeDecodeError):
            return JsonResponse({"detail": "Некорректный JSON."}, status=400)
        if not isinstance(data, dict):
            return JsonResponse({"detail": "Ожидается объект."}, status=400)
        if action:
            if request.method != "POST" or not obj:
                return JsonResponse(
                    {"detail": "Команда требует POST с идентификатором."}, status=405
                )
            try:
                _, form = action_form(obj, action, data)
            except ValueError:
                raise Http404
        else:
            if (
                not RESOURCES[kind][1]
                or (obj and request.method != "PATCH")
                or (not obj and request.method != "POST")
            ):
                return JsonResponse({"detail": "Недопустимая операция."}, status=405)
            supplied = set(data)
            if obj:
                combined = model_to_dict(obj, fields=RESOURCES[kind][1])
                combined.update(data)
                data = combined
            form = record_form(kind)(data, instance=obj)
            if supplied - set(form.fields):
                return JsonResponse(
                    {"detail": "Переданы защищённые или неизвестные поля."}, status=400
                )
        if set(data) - set(form.fields):
            return JsonResponse({"detail": "Неизвестные поля команды."}, status=400)
        if not form.is_valid():
            return JsonResponse({"errors": form.errors.get_json_data()}, status=400)
        if action:
            result = services.command(
                request.user,
                model,
                pk,
                form.cleaned_data["version"],
                action,
                command_data(form),
            )
        else:
            result = services.save_record(
                request.user,
                form.save(commit=False),
                form.cleaned_data.get("expected_version"),
            )
        return JsonResponse(public_data(result), status=200 if obj else 201)
    except Conflict:
        return JsonResponse({"detail": "Конфликт версии."}, status=409)
    except PermissionDenied as exc:
        return JsonResponse({"detail": str(exc) or "Нет доступа."}, status=403)
    except ValidationError as exc:
        return JsonResponse({"detail": exc.messages}, status=400)


def export_quote(request, pk, format):
    access(request, "quotes")
    quote = get_object_or_404(Quote, pk=pk)
    from .exports import render_quote

    if format not in ("pdf", "xlsx"):
        raise Http404
    if hasattr(quote, "dispatch"):
        payload = bytes(getattr(quote.dispatch, format))
        content_type = (
            "application/pdf"
            if format == "pdf"
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    else:
        payload, content_type = render_quote(quote, format)
    response = HttpResponse(payload, content_type=content_type)
    response["Content-Disposition"] = (
        f'attachment; filename="OVI-quote-{quote.pk}.{format}"'
    )
    services.event(request.user, quote, "export_" + format)
    return response
