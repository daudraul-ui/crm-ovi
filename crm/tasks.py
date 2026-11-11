from celery import shared_task
from django.utils import timezone

from .models import Notification, Task


@shared_task
def notify_overdue():
    for task in (
        Task.objects.exclude(status="done").filter(due_at__lt=timezone.now()).iterator()
    ):
        Notification.objects.get_or_create(
            key=f"overdue:{task.pk}:{task.assignee_id}:{task.due_at.isoformat()}",
            defaults={
                "user_id": task.assignee_id,
                "text": f"Просрочена задача: {task.name}",
            },
        )
