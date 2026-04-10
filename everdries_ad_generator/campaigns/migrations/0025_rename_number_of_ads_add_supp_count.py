from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("campaigns", "0024_apisettings_anthropic"),
    ]

    operations = [
        migrations.RenameField(
            model_name="generator",
            old_name="number_of_ads",
            new_name="number_of_headlines",
        ),
        migrations.AddField(
            model_name="generator",
            name="number_of_supplementary_copy",
            field=models.PositiveIntegerField(default=5),
        ),
    ]
