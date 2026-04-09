from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("campaigns", "0022_generator_progress_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="generator",
            name="supplementary_copy",
            field=models.TextField(
                blank=True,
                help_text="Feature callouts / supporting copy lines, one per line. Rendered alongside the headline on every ad.",
            ),
        ),
    ]
