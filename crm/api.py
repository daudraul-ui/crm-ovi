from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.routers import DefaultRouter

from .forms import MODELS
from .models import Comment, Document, Task
from .security import allowed
from .services import assigned, audit, snapshot


class ValidatedSerializer(serializers.ModelSerializer):
    def get_fields(self):
        fields = super().get_fields()
        if not self.context["request"].user.has_perm("crm.view_finance"):
            fields.pop("financial_notes", None)
        if "file" in fields:
            fields["file"].write_only = True
        return fields

    def validate(self, attrs):
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
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            )
        return attrs


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
        }.get(self.action, "view")
        if not allowed(request.user, self.queryset.model, action):
            raise PermissionDenied()

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
            except ProtectedError:
                raise serializers.ValidationError(
                    "Объект используется другими записями"
                )


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
        {"queryset": model.objects.all(), "serializer_class": serializer},
    )
    router.register(key, view, basename=key)
