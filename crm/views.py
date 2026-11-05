import io

from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django_otp import login as otp_login
from django_otp.plugins.otp_totp.models import TOTPDevice
from openpyxl import load_workbook

from .forms import LABELS, MODELS, form_class
from .models import (
    AuditEvent,
    Chain,
    Comment,
    Company,
    Contact,
    Document,
    Notification,
    Project,
    Region,
    Store,
    StoreFormat,
    Task,
)
from .security import allowed
from .services import assigned, audit, snapshot


def health(request):
    return HttpResponse("ok")


def sign_in(request):
    import hashlib

    from django.core.cache import cache

    key = (
        "login:"
        + hashlib.sha256(
            (
                request.META.get("REMOTE_ADDR", "")
                + ":"
                + request.POST.get("username", "")
            ).encode()
        ).hexdigest()
    )
    failures = cache.get(key, 0)
    if request.method == "POST" and failures >= 10:
        return HttpResponse(
            "Слишком много попыток. Повторите через 15 минут.", status=429
        )
    form = AuthenticationForm(request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        cache.delete(key)
        login(request, form.get_user())
        AuditEvent.objects.create(
            actor=request.user,
            action="login_password",
            model="auth.User",
            object_id=str(request.user.pk),
        )
        return redirect("two_factor")
    if request.method == "POST":
        cache.set(key, failures + 1, 900)
    return render(request, "registration/login.html", {"form": form})


@require_POST
def sign_out(request):
    logout(request)
    return redirect("login")


@login_required
def two_factor(request):
    with transaction.atomic():
        device = (
            TOTPDevice.objects.select_for_update().filter(user=request.user).first()
        )
        if device is None:
            device = TOTPDevice.objects.create(
                user=request.user, name="Основной", confirmed=False
            )
        error = None
        if request.method == "POST":
            if device.verify_token(request.POST.get("token", "")):
                device.confirmed = True
                device.save()
                otp_login(request, device)
                AuditEvent.objects.create(
                    actor=request.user,
                    action="2fa_verified",
                    model="auth.User",
                    object_id=str(request.user.pk),
                )
                return redirect("dashboard")
            error = "Неверный код или слишком много попыток. Подождите и повторите."
    return render(
        request,
        "registration/2fa.html",
        {
            "provisioning": device.config_url if not device.confirmed else None,
            "error": error,
        },
    )


def dashboard(request):
    counts = []
    for key in ("companies", "stores", "projects", "tasks"):
        model = MODELS[key]
        if allowed(request.user, model):
            counts.append((LABELS[key], model.objects.count()))
    overdue = (
        Task.objects.exclude(status="done").filter(due_at__lt=timezone.now())
        if allowed(request.user, Task)
        else Task.objects.none()
    )
    projects = (
        Project.objects.exclude(status__in=["done", "paused"]).select_related(
            "company", "manager"
        )[:6]
        if allowed(request.user, Project)
        else []
    )
    return render(
        request,
        "crm/dashboard.html",
        {"counts": counts, "overdue": overdue, "projects": projects},
    )


def listing(request, kind):
    model = MODELS.get(kind)
    if not model or not allowed(request.user, model):
        raise PermissionDenied
    objects = model.objects.all()
    query = request.GET.get("q", "")
    if query:
        objects = (
            objects.filter(name__icontains=query)
            if hasattr(model, "name")
            else objects.none()
        )
    if kind == "companies":
        if query:
            objects = model.objects.filter(
                Q(name__icontains=query) | Q(inn__icontains=query)
            )
        if request.GET.get("manager"):
            objects = (
                objects.filter(manager_id=request.GET["manager"])
                if request.GET["manager"].isascii() and request.GET["manager"].isdigit()
                else objects.none()
            )
    if kind == "stores" and request.GET.get("chain"):
        objects = (
            objects.filter(chain_id=request.GET["chain"])
            if request.GET["chain"].isascii() and request.GET["chain"].isdigit()
            else objects.none()
        )
    if kind in ("projects", "tasks") and request.GET.get("status"):
        objects = objects.filter(status=request.GET["status"])
    return render(
        request,
        "crm/list.html",
        {
            "objects": objects[:200],
            "kind": kind,
            "title": LABELS[kind],
            "can_add": allowed(request.user, model, "add"),
            "can_change": allowed(request.user, model, "change"),
        },
    )


def edit(request, kind, pk=None):
    model = MODELS.get(kind)
    if not model or not allowed(request.user, model, "change" if pk else "add"):
        raise PermissionDenied
    obj = get_object_or_404(model, pk=pk) if pk else None
    before = snapshot(obj)
    previous_assignee = obj.assignee_id if isinstance(obj, Task) else None
    form = form_class(model, request.user)(
        request.POST or None, request.FILES or None, instance=obj
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            saved = form.save(commit=False)
            if isinstance(saved, Document):
                saved.uploaded_by = request.user
            if isinstance(saved, Comment):
                saved.author = request.user
            saved.save()
            form.save_m2m()
            audit(request.user, "update" if obj else "create", saved, before)
            if isinstance(saved, Task) and previous_assignee != saved.assignee_id:
                assigned(saved)
        return redirect("listing", kind=kind)
    return render(request, "crm/form.html", {"form": form, "title": LABELS[kind]})


@require_POST
def delete(request, kind, pk):
    model = MODELS.get(kind)
    if not model or not allowed(request.user, model, "delete"):
        raise PermissionDenied
    obj = get_object_or_404(model, pk=pk)
    from django.db.models.deletion import ProtectedError

    try:
        with transaction.atomic():
            audit(request.user, "delete", obj, snapshot(obj))
            obj.delete()
    except ProtectedError:
        return HttpResponseBadRequest("Объект используется другими записями")
    return redirect("listing", kind=kind)


def download(request, pk):
    if not allowed(request.user, Document):
        raise PermissionDenied
    obj = get_object_or_404(Document, pk=pk)
    if obj.project_id and not allowed(request.user, Project):
        raise PermissionDenied
    if obj.company_id and not allowed(request.user, Company):
        raise PermissionDenied
    audit(request.user, "download", obj)
    response = FileResponse(
        obj.file.open("rb"),
        as_attachment=True,
        filename=obj.name + __import__("pathlib").Path(obj.file.name).suffix,
    )
    response["Content-Type"] = "application/octet-stream"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def notifications(request):
    if (
        request.method == "POST"
        and request.POST.get("id", "").isascii()
        and request.POST.get("id", "").isdigit()
    ):
        Notification.objects.filter(
            user=request.user, pk=request.POST.get("id")
        ).update(read=True)
    return render(
        request,
        "crm/notifications.html",
        {"objects": Notification.objects.filter(user=request.user)[:100]},
    )


def settings_view(request):
    events = (
        AuditEvent.objects.select_related("actor")[:100]
        if request.user.has_perm("crm.view_auditevent")
        else []
    )
    return render(
        request,
        "crm/settings.html",
        {"events": events, "groups": request.user.groups.all()},
    )


def import_stores(request):
    if not allowed(request.user, Store, "add"):
        raise PermissionDenied
    error = None
    if request.method == "POST":
        upload = request.FILES.get("file")
        try:
            if not upload or upload.size > 2 * 1024 * 1024:
                raise ValueError("Ожидается XLSX до 2 МБ")
            from zipfile import ZipFile

            data = upload.read()
            with ZipFile(io.BytesIO(data)) as archive:
                if sum(i.file_size for i in archive.infolist()) > 20 * 1024 * 1024:
                    raise ValueError("Слишком большой распакованный файл")
            book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            with transaction.atomic():
                rows = book.active.iter_rows(values_only=True)
                if tuple(next(rows)) != (
                    "Сеть",
                    "Код",
                    "Название",
                    "Адрес",
                    "Регион",
                    "Формат",
                ):
                    raise ValueError("Неверные заголовки")
                for index, row in enumerate(rows, 2):
                    if index > 1001:
                        raise ValueError("Максимум 1000 строк")
                    chain_name, code, name, address, region_name, format_name = row
                    chain = Chain.objects.get(name=chain_name)
                    region = (
                        Region.objects.get(chain=chain, name=region_name)
                        if region_name
                        else None
                    )
                    fmt = (
                        StoreFormat.objects.get(chain=chain, name=format_name)
                        if format_name
                        else None
                    )
                    obj = Store(
                        chain=chain,
                        code=str(code or ""),
                        name=name or "",
                        address=address or "",
                        region=region,
                        format=fmt,
                    )
                    obj.full_clean()
                    obj.save()
                    audit(request.user, "import", obj)
            book.close()
            return redirect("listing", kind="stores")
        except (
            ValueError,
            ValidationError,
            Chain.DoesNotExist,
            Region.DoesNotExist,
            StoreFormat.DoesNotExist,
            __import__("zipfile").BadZipFile,
            __import__("xml.etree.ElementTree", fromlist=["ParseError"]).ParseError,
        ) as exc:
            error = f"Импорт отменён: {exc}"
    return render(request, "crm/import.html", {"error": error})


def detail(request, kind, pk):
    model = MODELS.get(kind)
    if not model or not allowed(request.user, model):
        raise PermissionDenied
    obj = get_object_or_404(model, pk=pk)
    fields = []
    for field in model._meta.fields:
        if field.name in ("id", "file"):
            continue
        if field.name == "financial_notes" and not request.user.has_perm(
            "crm.view_finance"
        ):
            continue
        display = getattr(obj, "get_" + field.name + "_display", None)
        fields.append(
            (field.verbose_name, display() if display else getattr(obj, field.name))
        )
    documents = Document.objects.none()
    if allowed(request.user, Document):
        if isinstance(obj, Project):
            documents = Document.objects.filter(project=obj)
        elif isinstance(obj, Company):
            documents = Document.objects.filter(company=obj)
    comments = (
        obj.comments.select_related("author")
        if isinstance(obj, Project) and allowed(request.user, Comment)
        else []
    )
    contacts = (
        obj.contacts.all()
        if isinstance(obj, Company) and allowed(request.user, Contact)
        else []
    )
    events = (
        AuditEvent.objects.filter(model=obj._meta.label, object_id=str(obj.pk))[:50]
        if request.user.has_perm("crm.view_auditevent")
        else []
    )
    return render(
        request,
        "crm/detail.html",
        {
            "obj": obj,
            "kind": kind,
            "fields": fields,
            "documents": documents,
            "comments": comments,
            "contacts": contacts,
            "events": events,
            "stores": obj.stores.all() if isinstance(obj, Project) else [],
            "can_change": allowed(request.user, model, "change"),
            "can_delete": allowed(request.user, model, "delete"),
        },
    )
