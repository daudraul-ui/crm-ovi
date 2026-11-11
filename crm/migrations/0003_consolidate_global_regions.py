from django.db import migrations


def consolidate_regions(apps, schema_editor):
    Region = apps.get_model("crm", "Region")
    Store = apps.get_model("crm", "Store")
    Audit = apps.get_model("crm", "AuditEvent")
    db = schema_editor.connection.alias
    canonical = {}
    for region in Region.objects.using(db).order_by("pk"):
        key = "".join(region.name.split()).casefold()
        root = canonical.setdefault(key, region.pk)
        Region.objects.using(db).filter(pk=region.pk).update(
            legacy_data={"chain_id": region.chain_id, "name": region.name},
            normalized_name=key if root == region.pk else None,
            canonical_id=None if root == region.pk else root,
        )
        if root != region.pk:
            for store in Store.objects.using(db).filter(region_id=region.pk):
                Audit.objects.using(db).create(
                    action="region_consolidated",
                    model="crm.Store",
                    object_id=str(store.pk),
                    before={"region": region.pk},
                    after={"region": root},
                )
            Store.objects.using(db).filter(region_id=region.pk).update(region_id=root)


def restore_regions(apps, schema_editor):
    Region = apps.get_model("crm", "Region")
    Chain = apps.get_model("crm", "Chain")
    Store = apps.get_model("crm", "Store")
    Audit = apps.get_model("crm", "AuditEvent")
    db = schema_editor.connection.alias
    Region.objects.using(db).update(normalized_name=None, canonical_id=None)
    for region in Region.objects.using(db).all():
        chain_id = region.legacy_data.get("chain_id")
        if not Chain.objects.using(db).filter(pk=chain_id).exists():
            chain_id = (
                Chain.objects.using(db)
                .get_or_create(name="Регионы: восстановление старой схемы")[0]
                .pk
            )
        Region.objects.using(db).filter(pk=region.pk).update(chain_id=chain_id)
    for event in Audit.objects.using(db).filter(
        action="region_consolidated", model="crm.Store"
    ):
        Store.objects.using(db).filter(
            pk=event.object_id, region_id=event.after["region"]
        ).update(region_id=event.before["region"])


class Migration(migrations.Migration):
    dependencies = [("crm", "0002_alter_company_options_remove_region_chain_and_more")]
    operations = [migrations.RunPython(consolidate_regions, restore_regions)]
