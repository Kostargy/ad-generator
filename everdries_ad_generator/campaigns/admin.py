from django.contrib import admin

from .models import Ad
from .models import AdMessage
from .models import Asset
from .models import Campaign
from .models import CustomerPersona
from .models import Generator


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


@admin.register(Generator)
class GeneratorAdmin(admin.ModelAdmin):
    list_display = ["title", "campaign", "customer_persona", "number_of_ads", "created_at"]
    list_filter = ["campaign", "created_at"]
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
