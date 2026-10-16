from django.db import migrations


def backfill_missing_openbench_columns(apps, schema_editor):
    app_config = apps.get_app_config("OpenBench")
    connection = schema_editor.connection

    with connection.cursor() as cursor:
        existing_tables = set(connection.introspection.table_names(cursor))

    for model in app_config.get_models():
        table_name = model._meta.db_table
        if table_name not in existing_tables:
            continue

        with connection.cursor() as cursor:
            table_description = connection.introspection.get_table_description(cursor, table_name)
        existing_columns = {column.name for column in table_description}

        for field in model._meta.local_fields:
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

            schema_editor.add_field(model, field)
            existing_columns.add(field.column)


class Migration(migrations.Migration):
    dependencies = [
        ("OpenBench", "0003_backfill_missing_test_columns"),
    ]

    operations = [
        migrations.RunPython(backfill_missing_openbench_columns, migrations.RunPython.noop),
    ]
