from django.db import migrations, models


def relabel_product_to_model(apps, schema_editor):
    Asset = apps.get_model("campaigns", "Asset")
    Asset.objects.filter(asset_type="product").update(asset_type="model")


def relabel_model_to_product(apps, schema_editor):
    Asset = apps.get_model("campaigns", "Asset")
    Asset.objects.filter(asset_type="model").update(asset_type="product")


class Migration(migrations.Migration):

    dependencies = [
        ("campaigns", "0025_rename_number_of_ads_add_supp_count"),
    ]

    operations = [
        # Step 1: relabel existing Asset rows.
        migrations.RunPython(relabel_product_to_model, relabel_model_to_product),
        # Step 2: update Asset.asset_type choices.
        migrations.AlterField(
            model_name="asset",
            name="asset_type",
            field=models.CharField(
                choices=[
                    ("style", "Style Reference"),
                    ("model", "Model Image"),
                    ("flat_lay", "Flat-Lay / Ghost Mannequin"),
                ],
                max_length=20,
            ),
        ),
        # Step 3: rename Generator.product_references → model_references.
        migrations.RenameField(
            model_name="generator",
            old_name="product_references",
            new_name="model_references",
        ),
        # Step 4: update related_name + limit_choices_to on the renamed field.
        migrations.AlterField(
            model_name="generator",
            name="model_references",
            field=models.ManyToManyField(
                blank=True,
                limit_choices_to={"asset_type": "model"},
                related_name="generators_as_model",
                to="campaigns.asset",
            ),
        ),
        # Step 5: add flat_lay_references M2M.
        migrations.AddField(
            model_name="generator",
            name="flat_lay_references",
            field=models.ManyToManyField(
                blank=True,
                limit_choices_to={"asset_type": "flat_lay"},
                related_name="generators_as_flat_lay",
                to="campaigns.asset",
            ),
        ),
        # Step 6: rename ProductReference proxy → ModelReference.
        migrations.RenameModel(
            old_name="ProductReference",
            new_name="ModelReference",
        ),
        migrations.AlterModelOptions(
            name="modelreference",
            options={
                "proxy": True,
                "verbose_name": "Model Image",
                "verbose_name_plural": "Model Images",
            },
        ),
        # Step 7: create FlatLayReference proxy for admin grouping.
        migrations.CreateModel(
            name="FlatLayReference",
            fields=[],
            options={
                "verbose_name": "Flat-Lay / Ghost Mannequin",
                "verbose_name_plural": "Flat-Lays / Ghost Mannequins",
                "proxy": True,
                "indexes": [],
                "constraints": [],
            },
            bases=("campaigns.asset",),
        ),
    ]
