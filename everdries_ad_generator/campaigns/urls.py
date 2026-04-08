from django.urls import path

from . import views

app_name = "campaigns"

urlpatterns = [
    # Dashboard (campaigns list)
    path("", views.dashboard, name="dashboard"),

    # Settings
    path("settings/", views.settings_view, name="settings"),

    # Product CRUD
    path("products/new/", views.campaign_create, name="campaign_create"),
    path("products/<int:campaign_id>/edit/", views.campaign_edit, name="campaign_edit"),

    # Product-scoped tabs
    path("products/<int:campaign_id>/generator/", views.generator_list, name="generator_list"),
    path("products/<int:campaign_id>/generator-statuses/", views.generator_statuses, name="generator_statuses"),
    path("products/<int:campaign_id>/export/", views.export_approved_images, name="export_approved_images"),
    path("products/<int:campaign_id>/ads/", views.ads_list, name="ads_list"),
    # Keep old review URL as alias for backwards compatibility
    path("products/<int:campaign_id>/review/", views.ads_list, name="review"),

    # Generators (within product)
    path("products/<int:campaign_id>/generators/new/", views.generator_create, name="generator_create"),
    path("products/<int:campaign_id>/generators/<int:generator_id>/edit/", views.generator_edit, name="generator_edit"),
    path("products/<int:campaign_id>/generators/<int:generator_id>/generate/", views.generate_ads, name="generate_ads"),
    path("products/<int:campaign_id>/generate-headlines/", views.generate_headlines, name="generate_headlines"),

    # Ads (within product)
    path("products/<int:campaign_id>/ads/<int:ad_id>/", views.ad_detail, name="ad_detail"),

    # Ad Actions (AJAX)
    path("ads/<int:ad_id>/approve/", views.ad_approve, name="ad_approve"),
    path("ads/<int:ad_id>/reject/", views.ad_reject, name="ad_reject"),
    path("ads/<int:ad_id>/message/", views.ad_message, name="ad_message"),
    path("ads/<int:ad_id>/revision-status/", views.ad_revision_status, name="ad_revision_status"),

    # Personas
    path("personas/", views.persona_list, name="persona_list"),
    path("personas/create/", views.persona_create, name="persona_create"),
    path("personas/<int:persona_id>/edit/", views.persona_edit, name="persona_edit"),
    path("personas/<int:persona_id>/delete/", views.persona_delete, name="persona_delete"),

    # Assets (AJAX)
    path("assets/upload/", views.asset_upload, name="asset_upload"),
]
