from django.forms.models import model_to_dict

from .models import AuditEvent, Notification


def snapshot(obj):
    if obj is None:
        return {}
    data = model_to_dict(obj)
    # Never copy file paths, personal contact details or financial text into audit.
    for key in ("file", "financial_notes", "details", "phone", "email", "text"):
        if key in data:
            data[key] = "[скрыто]"
    return {k: str(v) for k, v in data.items()}


def audit(user, action, obj, before=None):
    AuditEvent.objects.create(
        actor=user if user.is_authenticated else None,
        action=action,
        model=obj._meta.label,
        object_id=str(obj.pk),
        before=before or {},
        after={} if action == "delete" else snapshot(obj),
    )


def assigned(task):
    Notification.objects.create(
        user=task.assignee, text=f"Вам назначена задача: {task.name}"
    )


class NotificationChannel:
    """Extension interface for future delivery adapters. No external sending in stage A."""

    def deliver(self, notification):
        raise NotImplementedError
