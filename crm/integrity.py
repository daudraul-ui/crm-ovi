"""Shared validation for browser, API and ORM relations."""

from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.db.models.signals import m2m_changed, pre_delete
from django.dispatch import receiver


def validate_inn(value):
    if not value:
        return
    weights = {
        10: ((2, 4, 10, 3, 5, 9, 4, 6, 8),),
        12: ((7, 2, 4, 10, 3, 5, 9, 4, 6, 8), (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)),
    }
    if len(value) not in weights or not value.isascii() or not value.isdigit():
        raise ValidationError("ИНН должен содержать 10 или 12 цифр.")
    for w in weights[len(value)]:
        if sum(int(n) * k for n, k in zip(value, w)) % 11 % 10 != int(value[len(w)]):
            raise ValidationError("Неверная контрольная цифра ИНН.")


def name_key(value):
    return " ".join(value.split()).casefold()


def validate_manager(instance):
    if not instance.manager_id:
        return
    previous = (
        type(instance)
        .objects.filter(pk=instance.pk)
        .values_list("manager_id", flat=True)
        .first()
        if instance.pk
        else None
    )
    if previous != instance.manager_id and (
        not instance.manager.is_active
        or not instance.manager.groups.filter(
            name__in=["Директор", "РОП", "Менеджер"]
        ).exists()
    ):
        raise ValidationError(
            {"manager": "Выберите активного директора, РОП или менеджера."}
        )


def chain_matches(name, pk=None):
    from difflib import SequenceMatcher

    from .models import Chain

    return [
        chain
        for chain in Chain.objects.exclude(pk=pk)
        if SequenceMatcher(None, name_key(chain.name), name_key(name)).ratio() >= 0.8
    ]


def validate_project(project, stores, additional):
    allowed = {project.company_id, *[c.pk for c in additional]}
    conflicts = [str(s) for s in stores if s.company_id and s.company_id not in allowed]
    if conflicts:
        raise ValidationError(
            {
                "stores": "Явно добавьте юридические лица магазинов в дополнительные клиенты проекта: "
                + ", ".join(conflicts)
            }
        )


@receiver(pre_delete)
def protect_relations(sender, instance, **kwargs):
    from .models import Named

    if not isinstance(instance, Named):
        return
    dependencies = []
    for rel in instance._meta.related_objects:
        if rel.one_to_many or rel.many_to_many:
            manager = getattr(instance, rel.get_accessor_name(), None)
            if manager is not None:
                dependencies.extend(list(manager.all()[:10]))
    for field in instance._meta.many_to_many:
        dependencies.extend(list(getattr(instance, field.name).all()[:10]))
    if dependencies:
        raise ProtectedError(
            "Используется: "
            + ", ".join(map(str, dependencies))
            + ". Архивируйте запись.",
            dependencies,
        )


@receiver(m2m_changed)
def validate_relations(sender, instance, action, reverse, pk_set, **kwargs):
    from .models import Chain, Project, Store

    if sender is Chain.companies.through and action in ("pre_remove", "pre_clear"):
        if reverse:
            stores = Store.objects.filter(company=instance)
            if action == "pre_remove":
                stores = stores.filter(chain_id__in=pk_set)
            if stores.exists():
                raise ValidationError("Юридическое лицо используется магазином сети.")
        else:
            keep = (
                instance.companies.exclude(pk__in=pk_set or []).values_list(
                    "pk", flat=True
                )
                if action == "pre_remove"
                else []
            )
            instance.validate_companies(keep)
    if sender is Project.stores.through and action == "pre_add":
        if reverse:
            for project in Project.objects.filter(pk__in=pk_set):
                validate_project(
                    project, [instance], project.additional_companies.all()
                )
        else:
            validate_project(
                instance,
                Store.objects.filter(pk__in=pk_set),
                instance.additional_companies.all(),
            )
    if sender is Project.additional_companies.through and action in (
        "pre_remove",
        "pre_clear",
    ):
        projects = (
            Project.objects.filter(additional_companies=instance)
            if reverse
            else [instance]
        )
        if reverse and action == "pre_remove":
            projects = projects.filter(pk__in=pk_set)
        for project in projects:
            excluded = [instance.pk] if reverse else pk_set or []
            remaining = (
                project.additional_companies.exclude(pk__in=excluded)
                if action == "pre_remove" or reverse
                else []
            )
            validate_project(project, project.stores.all(), remaining)
