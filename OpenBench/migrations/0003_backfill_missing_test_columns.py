from django.db import migrations


def backfill_missing_test_columns(apps, schema_editor):
    test_model = apps.get_model("OpenBench", "Test")
    connection = schema_editor.connection

    with connection.cursor() as cursor:
        table_description = connection.introspection.get_table_description(
            cursor, test_model._meta.db_table
        )
    existing_columns = {column.name for column in table_description}

    for field in test_model._meta.local_fields:
        if field.primary_key or field.column in existing_columns:
            continue

        can_add_safely = (
            field.null
            or field.has_default()
            or getattr(field, "auto_now", False)
            or getattr(field, "auto_now_add", False)
        )
        if not can_add_safely:
            continue

        schema_editor.add_field(test_model, field)
        existing_columns.add(field.column)


class Migration(migrations.Migration):
    dependencies = [
        ("OpenBench", "0002_ensure_spsa_column"),
    ]

    operations = [
        migrations.RunPython(backfill_missing_test_columns, migrations.RunPython.noop),
    ]
