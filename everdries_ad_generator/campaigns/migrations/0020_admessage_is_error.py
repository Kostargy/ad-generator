from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("campaigns", "0019_add_critic_max_retries"),
    ]

    operations = [
        migrations.AddField(
            model_name="admessage",
            name="is_error",
            field=models.BooleanField(default=False),
        ),
    ]
