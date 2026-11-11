from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, PermissionDenied
from rest_framework.response import Response
from rest_framework.routers import DefaultRouter

from .forms import MODELS
from .models import (
    Chain,
    Comment,
    ConcurrentRecord,
    Document,
    Named,
    Project,
    Region,
    Store,
    Task,
)
from .security import allowed
from .services import assigned, audit, snapshot


class ValidatedSerializer(serializers.ModelSerializer):
    def get_fields(self):
        fields = super().get_fields()
        if self.Meta.model is Chain:
            fields["confirm_similar"] = serializers.BooleanField(
                required=False, write_only=True
            )
        if issubclass(self.Meta.model, ConcurrentRecord):
            fields["version"] = serializers.IntegerField(required=False, min_value=1)
            if "archived" in fields:
                fields["archived"].read_only = True
        if not self.context["request"].user.has_perm("crm.view_finance"):
            fields.pop("financial_notes", None)
        if "region" in fields:
            fields["region"].queryset = Region.objects.filter(canonical__isnull=True)
        if self.Meta.model is Region:
            for name in ("normalized_name", "canonical", "legacy_data"):
                fields.pop(name, None)
        if not self.context["request"].user.has_perm("crm.view_company"):
            if self.Meta.model is Chain:
                fields.pop("companies", None)
            elif self.Meta.model is Store:
                fields.pop("company", None)
        if "file" in fields:
            fields["file"].write_only = True
        return fields

    def validate(self, attrs):
        if self.Meta.model is Chain:
            from .integrity import chain_matches

            confirmed = attrs.pop("confirm_similar", False)
            name = attrs.get("name", self.instance.name if self.instance else "")
            matches = chain_matches(name, self.instance.pk if self.instance else None)
            if matches and not confirmed:
                raise serializers.ValidationError(
                    {
                        "name": "Похожие сети: "
                        + ", ".join(map(str, matches[:10]))
                        + ". Выберите существующую сеть либо подтвердите отдельную запись (confirm_similar)."
                    }
                )
        if self.instance and isinstance(self.instance, ConcurrentRecord):
            if attrs.get("version") != self.instance.version:
                raise Conflict()
        elif "version" in attrs:
            attrs.pop("version")
        if "financial_notes" in self.initial_data and not self.context[
            "request"
        ].user.has_perm("crm.view_finance"):
            raise PermissionDenied("Нет доступа к финансам")
        import copy

        obj = copy.copy(self.instance) if self.instance else self.Meta.model()
        for key, value in attrs.items():
            if not obj._meta.get_field(key).many_to_many:
                setattr(obj, key, value)
        try:
            obj.full_clean(exclude=["author", "uploaded_by"])
            if isinstance(obj, Project):
                from .integrity import validate_project

                validate_project(
                    obj,
                    attrs.get("stores", obj.stores.all() if obj.pk else []),
                    attrs.get(
                        "additional_companies",
                        obj.additional_companies.all() if obj.pk else [],
                    ),
                )
            if isinstance(obj, Chain) and "companies" in attrs:
                obj.validate_companies([company.pk for company in attrs["companies"]])
            if (
                "manager" in attrs
                and not attrs["manager"].is_active
                and (
                    not self.instance or self.instance.manager_id != attrs["manager"].pk
                )
            ):
                raise DjangoValidationError(
                    {"manager": "Выберите активного сотрудника"}
                )
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            )
        return attrs

    def update(self, instance, validated_data):
        # Add explicitly confirmed clients before attaching stores; remove old ones last.
        if isinstance(instance, Project):
            additional = validated_data.pop(
                "additional_companies", list(instance.additional_companies.all())
            )
            stores = validated_data.pop("stores", list(instance.stores.all()))
            instance.additional_companies.add(*additional)
            obj = super().update(instance, validated_data)
            obj.stores.set(stores)
            obj.additional_companies.set(additional)
            return obj
        return super().update(instance, validated_data)

    def create(self, validated_data):
        if self.Meta.model is Project:
            stores = validated_data.pop("stores", [])
            obj = super().create(validated_data)
            obj.stores.set(stores)
            return obj
        return super().create(validated_data)


class Conflict(APIException):
    status_code = 409
    default_detail = (
        "Запись изменена. Загрузите актуальную карточку и повторите изменения."
    )


class ResourceViewSet(viewsets.ModelViewSet):
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        action = {
            "list": "view",
            "retrieve": "view",
            "create": "add",
            "update": "change",
            "partial_update": "change",
            "destroy": "delete",
            "archive": "change",
        }.get(self.action, "view")
        if not allowed(request.user, self.queryset.model, action):
            raise PermissionDenied()

    @action(detail=True, methods=["post"])
    def archive(self, request, pk=None):
        with transaction.atomic():
            obj = self.get_queryset().select_for_update().get(pk=pk)
            if not isinstance(obj, Named):
                raise serializers.ValidationError(
                    "Архив для этого типа не поддерживается."
                )
            if request.data.get("version") != obj.version:
                raise Conflict()
            if type(request.data.get("archived")) is not bool:
                raise serializers.ValidationError("Укажите archived: true или false.")
            before = snapshot(obj)
            obj.archived = request.data["archived"]
            obj.save()
            audit(request.user, "archive" if obj.archived else "restore", obj, before)
            return Response(self.get_serializer(obj).data)

    def perform_create(self, serializer):
        with transaction.atomic():
            extra = {}
            if serializer.Meta.model is Document:
                extra["uploaded_by"] = self.request.user
            if serializer.Meta.model is Comment:
                extra["author"] = self.request.user
            obj = serializer.save(**extra)
            audit(self.request.user, "create", obj)
            if isinstance(obj, Task):
                assigned(obj)

    def perform_update(self, serializer):
        with transaction.atomic():
            if isinstance(serializer.instance, ConcurrentRecord):
                current = (
                    type(serializer.instance)
                    .objects.select_for_update()
                    .get(pk=serializer.instance.pk)
                )
                if current.version != serializer.validated_data.get("version"):
                    raise Conflict()
            before = snapshot(serializer.instance)
            old = (
                serializer.instance.assignee_id
                if isinstance(serializer.instance, Task)
                else None
            )
            obj = serializer.save()
            audit(self.request.user, "update", obj, before)
            if isinstance(obj, Task) and old != obj.assignee_id:
                assigned(obj)

    def perform_destroy(self, instance):
        from django.db.models.deletion import ProtectedError

        with transaction.atomic():
            audit(self.request.user, "delete", instance, snapshot(instance))
            try:
                instance.delete()
            except ProtectedError as exc:
                raise serializers.ValidationError(str(exc.args[0]))


router = DefaultRouter()
for key, model in MODELS.items():
    meta = type(
        "Meta",
        (),
        {
            "model": model,
            "fields": "__all__",
            "read_only_fields": ["author", "uploaded_by", "created_at"],
        },
    )
    serializer = type(
        f"{model.__name__}Serializer", (ValidatedSerializer,), {"Meta": meta}
    )
    view = type(
        f"{model.__name__}ViewSet",
        (ResourceViewSet,),
        {
            "queryset": model.objects.filter(canonical__isnull=True)
            if model is Region
            else model.objects.all(),
            "serializer_class": serializer,
        },
    )
    router.register(key, view, basename=key)
