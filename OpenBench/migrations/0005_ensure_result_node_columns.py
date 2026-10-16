from django.db import migrations


def ensure_result_node_columns(apps, schema_editor):
    table_name = "OpenBench_result"
    connection = schema_editor.connection

    with connection.cursor() as cursor:
        table_description = connection.introspection.get_table_description(cursor, table_name)
    existing_columns = {column.name for column in table_description}

    for column_name in (
        "dev_nodes",
        "dev_time",
        "dev_time_scaled",
        "base_nodes",
        "base_time",
        "base_time_scaled",
    ):
        if column_name in existing_columns:
            continue
        quoted_table = schema_editor.quote_name(table_name)
        quoted_column = schema_editor.quote_name(column_name)
        schema_editor.execute(
            f"ALTER TABLE {quoted_table} ADD COLUMN {quoted_column} bigint NOT NULL DEFAULT 0"
        )
        existing_columns.add(column_name)


class Migration(migrations.Migration):
    dependencies = [
        ("OpenBench", "0004_backfill_missing_openbench_columns"),
    ]

    operations = [
        migrations.RunPython(ensure_result_node_columns, migrations.RunPython.noop),
    ]
