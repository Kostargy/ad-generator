from django.contrib import admin

from .models import Ad
from .models import AdMessage
from .models import APISettings
from .models import Asset
from .models import Campaign
from .models import CustomerPersona
from .models import Generator
from .models import ProductReference
from .models import StyleReference


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ["name", "product", "created_by", "created_at"]
    list_filter = ["product", "created_at"]
    search_fields = ["name", "description"]


@admin.register(CustomerPersona)
class CustomerPersonaAdmin(admin.ModelAdmin):
    list_display = ["name", "created_by", "created_at"]
    search_fields = ["name", "description"]


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = ["name", "asset_type", "created_by", "created_at"]
    list_filter = ["asset_type", "created_at"]
    search_fields = ["name"]


class _AssetTypeAdmin(admin.ModelAdmin):
    """Base admin for Asset proxy models that pre-filters by asset_type."""

    asset_type: str = ""
    list_display = ["name", "image", "created_by", "created_at"]
    search_fields = ["name"]
    fields = ["name", "image", "created_by"]

    def get_queryset(self, request):
        return super().get_queryset(request).filter(asset_type=self.asset_type)

    def save_model(self, request, obj, form, change):
        obj.asset_type = self.asset_type
        if not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(StyleReference)
class StyleReferenceAdmin(_AssetTypeAdmin):
    asset_type = Asset.TYPE_STYLE


@admin.register(ProductReference)
class ProductReferenceAdmin(_AssetTypeAdmin):
    asset_type = Asset.TYPE_PRODUCT


@admin.register(Generator)
class GeneratorAdmin(admin.ModelAdmin):
    list_display = ["title", "campaign", "status", "customer_persona", "number_of_headlines", "number_of_supplementary_copy", "created_at"]
    list_filter = ["status", "campaign", "created_at"]
    search_fields = ["title", "brief"]


@admin.register(Ad)
class AdAdmin(admin.ModelAdmin):
    list_display = ["headline", "generator", "status", "created_at"]
    list_filter = ["status", "generator__campaign", "created_at"]
    search_fields = ["headline", "description"]


@admin.register(AdMessage)
class AdMessageAdmin(admin.ModelAdmin):
    list_display = ["ad", "role", "content_preview", "created_at"]
    list_filter = ["role", "created_at"]
    search_fields = ["content"]

    def content_preview(self, obj):
        return obj.content[:50] + "..." if len(obj.content) > 50 else obj.content
    content_preview.short_description = "Content"


@admin.register(APISettings)
class APISettingsAdmin(admin.ModelAdmin):
    list_display = ["primary_provider", "gemini_model", "critic_model", "updated_at"]
    readonly_fields = ["updated_at"]
    fieldsets = (
        ("Provider", {
            "fields": ("primary_provider",),
        }),
        ("API Keys", {
            "fields": ("gemini_api_key", "openai_api_key"),
            "description": "Stored in plain text and visible here. Treat this admin page as sensitive.",
        }),
        ("Models", {
            "fields": ("gemini_model", "critic_model", "critic_max_retries"),
        }),
        ("Master Prompt", {
            "fields": ("master_prompt",),
        }),
        ("Metadata", {
            "fields": ("updated_at",),
        }),
    )

    def has_add_permission(self, request):
        # Singleton: only allow one row.
        return not APISettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
