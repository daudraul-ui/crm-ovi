import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_task0012_preserves_legacy_data():
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([("crm", "0004_finalize_global_regions")])
        apps = executor.loader.project_state(
            [("crm", "0004_finalize_global_regions")]
        ).apps
        User = apps.get_model("auth", "User")
        Company = apps.get_model("crm", "Company")
        Chain = apps.get_model("crm", "Chain")
        Store = apps.get_model("crm", "Store")
        Project = apps.get_model("crm", "Project")
        user = User.objects.create(username="legacy")
        one = Company.objects.create(name="Один", inn="invalid-inn", manager=user)
        two = Company.objects.create(name="Два", inn="invalid-inn", manager=user)
        chain = Chain.objects.create(name="Сеть")
        chain.companies.add(one, two)
        store = Store.objects.create(
            name="Магазин", code="0001", chain=chain, company=two, address="Адрес"
        )
        project = Project.objects.create(name="История", company=one, manager=user)
        project.stores.add(store)
        executor = MigrationExecutor(connection)
        executor.migrate(latest)
        from crm.models import Company as NewCompany
        from crm.models import Project as NewProject
        from crm.models import Store as NewStore

        assert NewCompany.objects.filter(inn="invalid-inn").count() == 2
        assert NewStore.objects.get(pk=store.pk).code == "0001"
        current = NewProject.objects.get(pk=project.pk)
        assert current.stores.get().company_id == two.pk
        assert not current.additional_companies.exists()
        assert current.version == 1 and not current.archived
    finally:
        MigrationExecutor(connection).migrate(latest)
