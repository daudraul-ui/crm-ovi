from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("crm", "0003_consolidate_global_regions")]
    operations = [
        migrations.RemoveField(model_name="region", name="chain"),
        migrations.AddConstraint(
            model_name="region",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("canonical__isnull", True), ("normalized_name__isnull", False)
                    ),
                    models.Q(
                        ("canonical__isnull", False), ("normalized_name__isnull", True)
                    ),
                    _connector="OR",
                ),
                name="region_canonical_key",
            ),
        ),
    ]
