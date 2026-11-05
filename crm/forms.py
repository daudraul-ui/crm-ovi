from django import forms

from . import models

MODELS = {
    "companies": models.Company,
    "contacts": models.Contact,
    "chains": models.Chain,
    "regions": models.Region,
    "formats": models.StoreFormat,
    "stores": models.Store,
    "projects": models.Project,
    "tasks": models.Task,
    "documents": models.Document,
    "comments": models.Comment,
}
LABELS = {
    "companies": "Клиенты",
    "contacts": "Контактные лица",
    "chains": "Торговые сети",
    "regions": "Регионы",
    "formats": "Форматы",
    "stores": "Магазины",
    "projects": "Проекты",
    "tasks": "Задачи",
    "documents": "Документы",
    "comments": "Комментарии",
}


def form_class(model, user):
    excluded = ["author", "uploaded_by"]
    if not user.has_perm("crm.view_finance"):
        excluded.append("financial_notes")
    return forms.modelform_factory(
        model,
        exclude=excluded,
        widgets={
            "file": forms.FileInput(),
            "due_at": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
        },
    )
