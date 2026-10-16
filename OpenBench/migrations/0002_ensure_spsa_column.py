from django.db import migrations


def ensure_spsa_column(apps, schema_editor):
    table_name = "OpenBench_test"
    column_name = "spsa"

    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"PRAGMA table_info({schema_editor.quote_name(table_name)})")
        existing_columns = {row[1] for row in cursor.fetchall()}

    if column_name not in existing_columns:
        quoted_table = schema_editor.quote_name(table_name)
        quoted_column = schema_editor.quote_name(column_name)
        schema_editor.execute(
            f"ALTER TABLE {quoted_table} ADD COLUMN {quoted_column} text NULL"
        )


class Migration(migrations.Migration):

    dependencies = [
        ("OpenBench", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(ensure_spsa_column, migrations.RunPython.noop),
    ]
