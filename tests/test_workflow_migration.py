import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_workflow_migration_preserves_existing_crm_rows():
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([("workflow", None)])
        from django.contrib.auth.models import User
        from django.utils import timezone

        from crm.models import Chain, Company, Project, Store, Task

        user = User.objects.create(username="preserved-workflow-migration")
        company = Company.objects.create(name="Существующий клиент", manager=user)
        chain = Chain.objects.create(name="Существующая сеть")
        store = Store.objects.create(
            name="Существующий магазин", code="00007", address="Адрес", chain=chain
        )
        project = Project.objects.create(
            name="Существующий проект", company=company, manager=user
        )
        # Populate the old schema without current application signal handlers.
        Project.stores.through.objects.create(project=project, store=store)
        task = Task.objects.create(
            name="Существующая задача",
            project=project,
            assignee=user,
            due_at=timezone.now(),
        )
        models = [Company, Project, Chain, Store, Task]
        before = {
            m._meta.label: list(m.objects.order_by("pk").values()) for m in models
        }
        before_links = list(Project.stores.through.objects.order_by("pk").values())
        MigrationExecutor(connection).migrate(latest)
        assert before == {
            m._meta.label: list(m.objects.order_by("pk").values()) for m in models
        }
        assert before_links == list(
            Project.stores.through.objects.order_by("pk").values()
        )
        assert Task.objects.get(pk=task.pk).assignee_id == user.pk
    finally:
        MigrationExecutor(connection).migrate(latest)
