from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("campaigns", "0026_rename_product_to_model_add_flat_lay"),
    ]

    operations = [
        migrations.AddField(
            model_name="generator",
            name="celery_task_id",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AlterField(
            model_name="generator",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("processing", "Processing"),
                    ("completed", "Completed"),
                    ("failed", "Failed"),
                    ("cancelled", "Cancelled"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
    ]
