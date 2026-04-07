from django.urls import path

from . import views

app_name = "campaigns"

urlpatterns = [
    # Dashboard (campaigns list)
    path("", views.dashboard, name="dashboard"),

    # Campaign CRUD
    path("campaigns/new/", views.campaign_create, name="campaign_create"),
    path("campaigns/<int:campaign_id>/edit/", views.campaign_edit, name="campaign_edit"),

    # Campaign-scoped tabs
    path("campaigns/<int:campaign_id>/generator/", views.generator_list, name="generator_list"),
    path("campaigns/<int:campaign_id>/ads/", views.ads_list, name="ads_list"),
    # Keep old review URL as alias for backwards compatibility
    path("campaigns/<int:campaign_id>/review/", views.ads_list, name="review"),

    # Generators (within campaign)
    path("campaigns/<int:campaign_id>/generators/new/", views.generator_create, name="generator_create"),
    path("campaigns/<int:campaign_id>/generators/<int:generator_id>/edit/", views.generator_edit, name="generator_edit"),

    # Ads (within campaign)
    path("campaigns/<int:campaign_id>/ads/<int:ad_id>/", views.ad_detail, name="ad_detail"),

    # Ad Actions (AJAX)
    path("ads/<int:ad_id>/approve/", views.ad_approve, name="ad_approve"),
    path("ads/<int:ad_id>/reject/", views.ad_reject, name="ad_reject"),
    path("ads/<int:ad_id>/message/", views.ad_message, name="ad_message"),

    # Personas (AJAX)
    path("personas/create/", views.persona_create, name="persona_create"),

    # Assets (AJAX)
    path("assets/upload/", views.asset_upload, name="asset_upload"),
]
