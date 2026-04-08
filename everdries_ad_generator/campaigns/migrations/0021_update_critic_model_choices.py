from django.db import migrations, models


def forward(apps, schema_editor):
    """Replace any retired/preview critic model values with the new default."""
    APISettings = apps.get_model("campaigns", "APISettings")
    valid = {"gemini-2.5-flash", "gemini-2.5-pro"}
    APISettings.objects.exclude(critic_model__in=valid).update(
        critic_model="gemini-2.5-flash"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("campaigns", "0020_admessage_is_error"),
    ]

    operations = [
        migrations.AlterField(
            model_name="apisettings",
            name="critic_model",
            field=models.CharField(
                choices=[
                    ("gemini-2.5-flash", "Gemini 2.5 Flash (Fast)"),
                    ("gemini-2.5-pro", "Gemini 2.5 Pro (Higher quality)"),
                ],
                default="gemini-2.5-flash",
                help_text="Model for image quality critique",
                max_length=100,
            ),
        ),
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
