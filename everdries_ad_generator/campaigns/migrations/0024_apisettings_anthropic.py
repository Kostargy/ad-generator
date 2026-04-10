from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("campaigns", "0023_add_supplementary_copy_to_generator"),
    ]

    operations = [
        migrations.AddField(
            model_name="apisettings",
            name="anthropic_api_key",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="apisettings",
            name="headline_anthropic_model",
            field=models.CharField(
                choices=[
                    ("claude-haiku-4-5-20251001", "Claude Haiku 4.5 (Fast, recommended for headlines)"),
                    ("claude-sonnet-4-6", "Claude Sonnet 4.6 (Balanced)"),
                    ("claude-opus-4-6", "Claude Opus 4.6 (Highest quality)"),
                ],
                default="claude-haiku-4-5-20251001",
                help_text="Claude model used to generate headlines and supplementary copy",
                max_length=100,
            ),
        ),
    ]
