from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import Group

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
    excluded = ["author", "uploaded_by", "archived"]
    if not user.has_perm("crm.view_finance"):
        excluded.append("financial_notes")
    if not user.has_perm("crm.view_company"):
        if model is models.Chain:
            excluded.append("companies")
        elif model is models.Store:
            excluded.append("company")
    return forms.modelform_factory(
        model,
        form=ResourceForm,
        exclude=excluded,
        widgets={
            "file": forms.FileInput(),
            "due_at": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
        },
    )


class EmployeeForm(UserCreationForm):
    roles = forms.ModelMultipleChoiceField(
        queryset=Group.objects.none(), label="Роли", widget=forms.CheckboxSelectMultiple
    )

    class Meta:
        model = get_user_model()
        fields = ["username", "first_name", "last_name", "email", "roles"]
        labels = {
            "username": "Логин",
            "first_name": "Имя",
            "last_name": "Фамилия",
            "email": "Email",
        }

    def __init__(self, *args, actor, **kwargs):
        from django.contrib.auth.models import Group

        super().__init__(*args, **kwargs)
        names = ["Директор", "РОП", "Менеджер", "Инженер", "Администратор"]
        if not actor.has_perm("crm.view_finance"):
            names.remove("Директор")
        self.fields["roles"].queryset = Group.objects.filter(name__in=names).order_by(
            "name"
        )
        self.fields["email"].required = True
        self.fields["password1"].label = "Пароль"
        self.fields["password2"].label = "Повторите пароль"


class EmployeeChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return (
            f"{obj.get_full_name()} ({obj.username})"
            if obj.get_full_name()
            else obj.username
        )


class ResourceForm(forms.ModelForm):
    record_version = forms.IntegerField(required=False, widget=forms.HiddenInput)

    def _save_m2m(self):
        if isinstance(self.instance, models.Project):
            self.instance.additional_companies.add(
                *self.cleaned_data.get("additional_companies", [])
            )
            self.instance.stores.set(self.cleaned_data.get("stores", []))
            self.instance.additional_companies.set(
                self.cleaned_data.get("additional_companies", [])
            )
            return
        super()._save_m2m()

    def __init__(self, *args, **kwargs):
        from django.contrib.auth import get_user_model
        from django.db.models import Q

        super().__init__(*args, **kwargs)
        if self.instance.pk and hasattr(self.instance, "version"):
            self.fields["record_version"].initial = self.instance.version
            self.fields["record_version"].required = True
        if self._meta.model is models.Chain:
            self.fields["confirm_similar"] = forms.BooleanField(
                required=False,
                label="Я проверил похожие сети и подтверждаю отдельную запись",
            )
        if "manager" in self.fields:
            current = self.instance.manager_id if self.instance.pk else None
            self.fields["manager"] = EmployeeChoiceField(
                label="Ответственный менеджер",
                queryset=get_user_model()
                .objects.filter(
                    Q(is_active=True, groups__name__in=["Директор", "РОП", "Менеджер"])
                    | Q(pk=current)
                )
                .order_by("last_name", "first_name", "username")
                .distinct(),
            )
        if self._meta.model is models.Store:
            raw = (
                self.data.get("chain")
                if self.is_bound
                else self.initial.get("chain", self.instance.chain_id)
            )
            chain_id = str(raw or "")
            chain = (
                models.Chain.objects.filter(pk=chain_id).first()
                if chain_id.isascii() and chain_id.isdigit()
                else None
            )
            if "company" in self.fields:
                self.fields["company"].queryset = (
                    chain.companies.all() if chain else models.Company.objects.none()
                )
            self.fields["format"].queryset = (
                models.StoreFormat.objects.filter(chain=chain)
                if chain
                else models.StoreFormat.objects.none()
            )
            self.fields["region"].queryset = models.Region.objects.filter(
                canonical__isnull=True
            )
        self.order_fields(
            ["name", "chain", "company", "region", "format", "code", "address"]
        )
        from .widgets import SearchSelect, SearchSelectMultiple
        for field in self.fields.values():
            if isinstance(field, forms.ModelChoiceField):
                kind = next((key for key, model in MODELS.items() if model is field.queryset.model), 'employees')
                widget = SearchSelectMultiple if isinstance(field, forms.ModelMultipleChoiceField) else SearchSelect
                field.widget = widget(attrs={'data-reference-kind':kind})
                field.widget.choices = field.choices

    def clean(self):
        data = super().clean()
        if self._meta.model is models.Chain and data.get("name"):
            from .integrity import chain_matches

            matches = chain_matches(data["name"], self.instance.pk)
            if matches and not data.get("confirm_similar"):
                raise forms.ValidationError(
                    "Похожие сети: "
                    + ", ".join(map(str, matches[:10]))
                    + ". Используйте существующую сеть или подтвердите отдельную запись."
                )
        if (
            self.instance.pk
            and hasattr(self.instance, "version")
            and data.get("record_version") != self.instance.version
        ):
            raise forms.ValidationError(
                "Запись изменена другим сотрудником. Обновите карточку и повторите изменения."
            )
        if self._meta.model is models.Project and data.get("company"):
            from .integrity import validate_project

            candidate = models.Project(company=data["company"])
            validate_project(
                candidate, data.get("stores", []), data.get("additional_companies", [])
            )
        if self._meta.model is models.Chain and "companies" in data:
            self.instance.validate_companies(
                [company.pk for company in data["companies"]]
            )
        return data
