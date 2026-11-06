import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_region_migration_preserves_records_and_store_links():
    old_target = [("crm", "0001_initial")]
    new_target = [("crm", "0004_finalize_global_regions")]
    executor = MigrationExecutor(connection)
    executor.migrate(old_target)
    old = executor.loader.project_state(old_target).apps
    try:
        Chain = old.get_model("crm", "Chain")
        Region = old.get_model("crm", "Region")
        Store = old.get_model("crm", "Store")
        Format = old.get_model("crm", "StoreFormat")
        User = old.get_model("auth", "User")
        Company = old.get_model("crm", "Company")
        Project = old.get_model("crm", "Project")
        a = Chain.objects.create(name="Сеть A")
        b = Chain.objects.create(name="Сеть B")
        first = Region.objects.create(chain=a, name=" Московская область ")
        duplicate = Region.objects.create(chain=b, name="МОСКОВСКАЯОБЛАСТЬ")
        other = Region.objects.create(chain=b, name="Татарстан")
        fmt = Format.objects.create(chain=b, name="Формат B")
        store_a = Store.objects.create(
            name="Первый", chain=a, region=first, code="1", address="Адрес A"
        )
        store_b = Store.objects.create(
            name="Второй",
            chain=b,
            region=duplicate,
            format=fmt,
            code="2",
            address="Адрес B",
        )
        store_c = Store.objects.create(
            name="Третий", chain=b, region=other, code="3", address="Адрес C"
        )
        user = User.objects.create(username="legacy")
        company = Company.objects.create(name="Существующее ЮЛ", manager=user)
        project = Project.objects.create(
            name="Существующий проект", company=company, manager=user
        )
        project.stores.add(store_a, store_b)
        executor = MigrationExecutor(connection)
        executor.migrate(new_target)
        new = executor.loader.project_state(new_target).apps
        Region = new.get_model("crm", "Region")
        Store = new.get_model("crm", "Store")
        assert Region.objects.count() == 3  # No original region row deleted.
        assert Region.objects.filter(canonical__isnull=True).count() == 2
        alias = Region.objects.get(pk=duplicate.pk)
        assert alias.canonical_id == first.pk
        assert (
            alias.name == "МОСКОВСКАЯОБЛАСТЬ" and alias.legacy_data["chain_id"] == b.pk
        )
        assert Region.objects.get(pk=first.pk).legacy_data == {
            "chain_id": a.pk,
            "name": " Московская область ",
        }
        assert Store.objects.count() == 3
        assert Store.objects.get(pk=store_b.pk).region_id == first.pk
        assert Store.objects.get(pk=store_b.pk).format_id == fmt.pk
        assert Store.objects.get(pk=store_b.pk).chain_id == b.pk
        assert Store.objects.get(pk=store_c.pk).region_id == other.pk
        assert not Store.objects.exclude(company_id=None).exists()
        assert (
            new.get_model("crm", "Company").objects.get(pk=company.pk).manager_id
            == user.pk
        )
        assert (
            new.get_model("crm", "Project").objects.get(pk=project.pk).stores.count()
            == 2
        )
        event = new.get_model("crm", "AuditEvent").objects.get(
            action="region_consolidated"
        )
        assert event.before == {"region": duplicate.pk} and event.after == {
            "region": first.pk
        }
    finally:
        MigrationExecutor(connection).migrate(new_target)
