from django.db import migrations, models


# The Result counters 0005 adds to the database when they're missing, now in Django's state too, so
# makemigrations has nothing left to do. State only: 0005 has made the columns already. The name is the one
# makemigrations gives it, which is what bobby recorded when it made and applied this migration itself.

COUNTERS = ('base_nodes', 'base_time', 'base_time_scaled', 'dev_nodes', 'dev_time', 'dev_time_scaled')


class Migration(migrations.Migration):

    dependencies = [
        ('OpenBench', '0005_ensure_result_node_columns'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(model_name='result', name=name, field=models.BigIntegerField(default=0))
                for name in COUNTERS
            ],
        ),
    ]
