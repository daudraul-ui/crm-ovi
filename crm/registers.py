from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db.models import Q
from django.utils import timezone

from .models import Chain, Company, Project, Region, Store, StoreFormat, Task

STORE_COLUMNS = [
    ("code", "Номер (КОД)"),
    ("name", "Название"),
    ("address", "Адрес"),
    ("region__name", "Регион"),
    ("format__name", "Формат"),
    ("chain__name", "Торговая сеть"),
    ("company__name", "Клиент"),
]


def filtered(model, params, user):
    qs = model.objects.all()
    if model is Region:
        qs = qs.filter(canonical__isnull=True)
    if hasattr(model, "archived"):
        qs = qs.filter(archived=params.get("archived") == "1")
    fields = [x[0] for x in STORE_COLUMNS] if model is Store else ["name"]
    if model is Store and not user.has_perm("crm.view_company"):
        fields.remove("company__name")
    if model is Company:
        fields += ["inn"]
    if params.get("q"):
        condition = Q()
        for field in fields:
            condition |= Q(**{field + "__icontains": params["q"]})
        qs = qs.filter(condition)
    filters = {
        Store: ["region", "format", "chain", "company"],
        Company: ["manager", "chains"],
        StoreFormat: ["chain"],
        Project: ["manager"],
        Task: ["assignee"],
    }.get(model, [])
    for field in filters:
        value = params.get(field)
        if field == "company" and not user.has_perm("crm.view_company"):
            continue
        if value:
            qs = (
                qs.filter(**{field + "__pk": value})
                if str(value).isascii() and str(value).isdigit()
                else qs.none()
            )
    if model in (Task, Project) and params.get("status"):
        qs = qs.filter(status=params["status"])
    if model is Task:
        view = params.get("view")
        if view == "mine":
            qs = qs.filter(assignee=user)
        elif view == "today":
            qs = qs.filter(due_at__date=timezone.localdate())
        elif view == "overdue":
            qs = qs.filter(due_at__lt=timezone.now()).exclude(status="done")
        elif view in ("active", "done"):
            qs = qs.filter(status=view)
    sort = params.get("sort", "name")
    sort_fields = fields + (["manager__last_name"] if model is Company else [])
    if sort.lstrip("-") not in sort_fields:
        sort = "name"
    if model is Store:
        qs = qs.select_related("chain", "company", "region", "format")
    elif model is Company:
        qs = qs.select_related("manager").prefetch_related("chains")
    return qs.order_by(sort, "pk").distinct()


def context(model, request):
    options = []
    choices = {
        Store: [
            ("region", "Регион", Region.objects.filter(canonical__isnull=True)),
            ("format", "Формат", StoreFormat.objects.select_related("chain")),
            ("chain", "Торговая сеть", Chain.objects.all()),
            ("company", "Клиент", Company.objects.all()),
        ],
        Company: [
            ("manager", "Менеджер", get_user_model().objects.filter(is_active=True)),
            ("chains", "Торговая сеть", Chain.objects.all()),
        ],
        StoreFormat: [("chain", "Торговая сеть", Chain.objects.all())],
    }.get(model, [])
    for key, label, qs in choices:
        if key == "company" and not request.user.has_perm("crm.view_company"):
            continue
        selected = request.GET.get(key, "")
        selected_ids = [selected] if selected.isascii() and selected.isdigit() else []
        qs = qs.filter(Q(pk__in=qs.values("pk")[:50]) | Q(pk__in=selected_ids))
        options.append(
            {
                "key": key,
                "reference_kind": {
                    "region": "regions",
                    "format": "formats",
                    "chain": "chains",
                    "chains": "chains",
                    "company": "companies",
                    "manager": "employees",
                }[key],
                "label": label,
                "items": [
                    (
                        str(obj.pk),
                        str(obj.get_full_name() or obj.username)
                        if key == "manager"
                        else str(obj),
                    )
                    for obj in qs
                ],
                "selected": request.GET.get(key, ""),
            }
        )
    qs = filtered(model, request.GET, request.user)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return {
        "objects": page,
        "page": page,
        "total": page.paginator.count,
        "querystring": params.urlencode(),
        "filters": options,
        "statuses": model._meta.get_field("status").choices
        if model in (Project, Task)
        else [],
        "sorts": STORE_COLUMNS
        if model is Store
        else [
            ("name", "Название"),
            *(
                [("inn", "ИНН"), ("manager__last_name", "Менеджер")]
                if model is Company
                else []
            ),
        ],
    }
