from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import FileResponse, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django_otp import login as otp_login
from django_otp.plugins.otp_totp.models import TOTPDevice

from .forms import LABELS, MODELS, EmployeeForm, form_class
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
    from django.core.cache import cache

    from .recovery import recover

    key = f"2fa-recovery:{request.user.pk}"
    if request.method == "POST" and request.POST.get("recovery_code"):
        attempts = cache.get(key, 0)
        if attempts >= 5:
            return HttpResponse(
                "Слишком много попыток. Повторите через 15 минут.", status=429
            )
        cache.set(key, attempts + 1, 900)
        if recover(request, request.POST["recovery_code"].strip()):
            return redirect("two_factor")
        return HttpResponse("Неверный резервный код.", status=400)
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        device = (
            TOTPDevice.objects.select_for_update().filter(user=request.user).first()
        )
        if device is None:
            device = TOTPDevice.objects.create(
                user=request.user, name="Основной", confirmed=False
            )
        error = None
        if request.method == "POST":
            first_setup = not device.confirmed
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
                if first_setup:
                    from .recovery import generate_codes

                    codes = generate_codes(request.user)
                    response = render(
                        request, "registration/recovery_codes.html", {"codes": codes}
                    )
                    response["Cache-Control"] = "no-store"
                    return response
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
            counts.append((LABELS[key], model.objects.count(), key))
    overdue = (
        Task.objects.exclude(status="done").filter(due_at__lt=timezone.now())
        if allowed(request.user, Task)
        else Task.objects.none()
    )
    projects = (
        Project.objects.exclude(status__in=["done", "paused"]).select_related(
            "company", "manager"
        )
        if allowed(request.user, Project)
        else Project.objects.none()
    )
    employee = request.GET.get("employee", "")
    if employee.isascii() and employee.isdigit():
        overdue = overdue.filter(assignee_id=employee)
        projects = projects.filter(manager_id=employee)
    for key, lookup in [("from", "due_at__date__gte"), ("to", "due_at__date__lte")]:
        if request.GET.get(key):
            from django.utils.dateparse import parse_date

            try:
                date = parse_date(request.GET[key])
            except ValueError:
                date = None
            if date:
                overdue = overdue.filter(**{lookup: date})
    return render(
        request,
        "crm/dashboard.html",
        {
            "counts": counts,
            "overdue": overdue[:50],
            "projects": projects[:20],
            "employees": get_user_model().objects.filter(is_active=True),
        },
    )


def listing(request, kind):
    model = MODELS.get(kind)
    if not model or not allowed(request.user, model):
        raise PermissionDenied
    from .registers import context

    data = context(model, request)
    data.update(
        {
            "kind": kind,
            "title": LABELS[kind],
            "can_add": allowed(request.user, model, "add"),
            "can_change": allowed(request.user, model, "change"),
        }
    )
    return render(request, "crm/list.html", data)


@transaction.atomic
def edit(request, kind, pk=None):
    model = MODELS.get(kind)
    if not model or not allowed(request.user, model, "change" if pk else "add"):
        raise PermissionDenied
    queryset = (
        model.objects.filter(canonical__isnull=True)
        if model is Region
        else model.objects.all()
    )
    obj = get_object_or_404(queryset.select_for_update(), pk=pk) if pk else None
    before = snapshot(obj)
    previous_assignee = obj.assignee_id if isinstance(obj, Task) else None
    form = form_class(model, request.user)(
        request.POST or None,
        request.FILES or None,
        instance=obj,
        initial={
            key: request.GET[key]
            for key in ("company", "project")
            if key in request.GET
            and key in [f.name for f in model._meta.fields]
            and request.GET[key].isdigit()
        }
        if not pk
        else None,
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
    return render(
        request,
        "crm/form.html",
        {
            "form": form,
            "title": LABELS[kind],
            "kind": kind,
            "quick_format": kind == "stores"
            and allowed(request.user, StoreFormat, "add"),
        },
    )


@require_POST
def delete(request, kind, pk):
    model = MODELS.get(kind)
    if not model or not allowed(request.user, model, "delete"):
        raise PermissionDenied
    queryset = (
        model.objects.filter(canonical__isnull=True)
        if model is Region
        else model.objects.all()
    )
    obj = get_object_or_404(queryset, pk=pk)
    from django.db.models.deletion import ProtectedError

    try:
        with transaction.atomic():
            audit(request.user, "delete", obj, snapshot(obj))
            obj.delete()
    except ProtectedError as exc:
        return HttpResponseBadRequest(str(exc.args[0]))
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
        {
            "events": events,
            "groups": request.user.groups.all(),
            "employees": get_user_model()
            .objects.prefetch_related("groups")
            .order_by("last_name", "username")
            if request.user.has_perm("crm.manage_employees")
            else None,
        },
    )


def detail(request, kind, pk):
    model = MODELS.get(kind)
    if not model or not allowed(request.user, model):
        raise PermissionDenied
    queryset = (
        model.objects.filter(canonical__isnull=True)
        if model is Region
        else model.objects.all()
    )
    obj = get_object_or_404(queryset, pk=pk)
    fields = []
    for field in model._meta.fields:
        if field.name in ("id", "file", "normalized_name", "canonical", "legacy_data"):
            continue
        if (
            field.name == "company"
            and isinstance(obj, Store)
            and not allowed(request.user, Company)
        ):
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
            "stores": obj.stores.all()
            if isinstance(obj, Project)
            else Store.objects.filter(chain=obj)[:50]
            if isinstance(obj, Chain) and allowed(request.user, Store)
            else [],
            "formats": StoreFormat.objects.filter(chain=obj)
            if isinstance(obj, Chain) and allowed(request.user, StoreFormat)
            else [],
            "store_count": Store.objects.filter(chain=obj).count()
            if isinstance(obj, Chain) and allowed(request.user, Store)
            else None,
            "project_tasks": Task.objects.filter(project=obj)[:50]
            if isinstance(obj, Project) and allowed(request.user, Task)
            else [],
            "additional_companies": obj.additional_companies.all()
            if isinstance(obj, Project) and allowed(request.user, Company)
            else [],
            "companies": obj.companies.all()
            if isinstance(obj, Chain) and allowed(request.user, Company)
            else [],
            "can_change": allowed(request.user, model, "change"),
            "can_delete": allowed(request.user, model, "delete"),
        },
    )


def employee_create(request):
    if not request.user.has_perm("crm.manage_employees"):
        raise PermissionDenied
    form = EmployeeForm(request.POST or None, actor=request.user)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            employee = form.save()
            employee.groups.set(form.cleaned_data["roles"])
            AuditEvent.objects.create(
                actor=request.user,
                action="employee_created",
                model="auth.User",
                object_id=str(employee.pk),
                after={
                    "username": employee.username,
                    "roles": list(employee.groups.values_list("name", flat=True)),
                },
            )
        return redirect("settings")
    return render(request, "crm/form.html", {"form": form, "title": "Новый сотрудник"})


def can_edit_store(user):
    return allowed(user, Store, "add") or allowed(user, Store, "change")


@require_GET
def store_options(request):
    if not can_edit_store(request.user):
        raise PermissionDenied
    raw = request.GET.get("chain", "")
    chain = (
        Chain.objects.filter(pk=raw).first()
        if raw.isascii() and raw.isdigit()
        else None
    )
    return JsonResponse(
        {
            "companies": list(chain.companies.values("id", "name"))
            if chain and allowed(request.user, Company)
            else [],
            "formats": list(
                StoreFormat.objects.filter(chain=chain).values("id", "name")
            )
            if chain
            else [],
        }
    )


@require_POST
def store_format_create(request):
    if not can_edit_store(request.user) or not allowed(
        request.user, StoreFormat, "add"
    ):
        raise PermissionDenied
    form = form_class(StoreFormat, request.user)(request.POST)
    if not form.is_valid():
        return JsonResponse({"errors": form.errors}, status=400)
    with transaction.atomic():
        fmt = form.save()
        audit(request.user, "create", fmt)
    return JsonResponse(
        {"id": fmt.pk, "name": fmt.name, "chain": fmt.chain_id}, status=201
    )


@require_POST
@transaction.atomic
def archive(request, kind, pk):
    from .models import Named

    model = MODELS.get(kind)
    if (
        not model
        or not issubclass(model, Named)
        or not allowed(request.user, model, "change")
    ):
        raise PermissionDenied
    obj = get_object_or_404(model.objects.select_for_update(), pk=pk)
    if str(obj.version) != request.POST.get("version"):
        return HttpResponse("Запись изменилась. Обновите карточку.", status=409)
    before = snapshot(obj)
    obj.archived = request.POST.get("archived") == "1"
    obj.save()
    audit(request.user, "archive" if obj.archived else "restore", obj, before)
    return redirect("detail", kind=kind, pk=pk)


@require_GET
def reference_options(request, kind):
    from django.db.models import Q

    if kind == "employees":
        if not any(
            allowed(request.user, m, action)
            for m in (Company, Project, Task)
            for action in ("add", "change")
        ):
            raise PermissionDenied
        qs = get_user_model().objects.filter(is_active=True)
        if request.GET.get("field") == "manager":
            qs = qs.filter(groups__name__in=["Директор", "РОП", "Менеджер"]).distinct()
        q = request.GET.get("q", "")
        qs = qs.filter(
            Q(username__icontains=q)
            | Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
        )[:50]
        return JsonResponse(
            {
                "items": [
                    {
                        "id": u.pk,
                        "name": f"{u.get_full_name()} ({u.username})"
                        if u.get_full_name()
                        else u.username,
                    }
                    for u in qs
                ]
            }
        )
    model = MODELS.get(kind)
    if model is None or not allowed(request.user, model):
        raise PermissionDenied
    qs = model.objects.all()
    if hasattr(model, "archived"):
        qs = qs.filter(archived=False)
    if model is Region:
        qs = qs.filter(canonical__isnull=True)
    chain = request.GET.get("chain", "")
    if request.GET.get("context") == "stores" and model in (Company, StoreFormat):
        if not chain.isascii() or not chain.isdigit():
            return JsonResponse({"items": []})
        qs = (
            qs.filter(chains__pk=chain)
            if model is Company
            else qs.filter(chain_id=chain)
        )
    if hasattr(model, "name"):
        qs = qs.filter(name__icontains=request.GET.get("q", ""))
    return JsonResponse({"items": [{"id": o.pk, "name": str(o)} for o in qs[:50]]})
