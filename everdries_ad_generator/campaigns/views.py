import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .models import Ad
from .models import AdMessage
from .models import APISettings
from .models import Asset
from .models import Campaign
from .models import CustomerPersona
from .models import Generator


@login_required
def dashboard(request):
    """Main dashboard showing campaigns list."""
    campaigns = Campaign.objects.filter(created_by=request.user)
    context = {
        "active_nav": "generator",
        "active_tab": "campaigns",
        "campaigns": campaigns,
    }
    return render(request, "campaigns/dashboard.html", context)


@login_required
def generator_list(request, campaign_id):
    """Generator tab - shows generators for a campaign."""
    campaign = get_object_or_404(Campaign, id=campaign_id, created_by=request.user)
    generators = campaign.generators.all()

    context = {
        "active_nav": "generator",
        "active_tab": "generator",
        "campaign": campaign,
        "generators": generators,
    }
    return render(request, "campaigns/generator.html", context)


@login_required
def ads_list(request, campaign_id):
    """Ads list view - shows all ads for a campaign."""
    campaign = get_object_or_404(Campaign, id=campaign_id, created_by=request.user)
    ads = Ad.objects.filter(generator__campaign=campaign).select_related("generator")

    context = {
        "active_tab": "ads",
        "campaign": campaign,
        "ads": ads,
    }
    return render(request, "campaigns/ads_list.html", context)


@login_required
def ad_detail(request, campaign_id, ad_id):
    """Ad detail view with chat interface."""
    campaign = get_object_or_404(Campaign, id=campaign_id, created_by=request.user)
    ad = get_object_or_404(Ad, id=ad_id, generator__campaign=campaign)
    messages_list = ad.messages.all()

    context = {
        "active_tab": "ads",
        "campaign": campaign,
        "ad": ad,
        "messages": messages_list,
    }
    return render(request, "campaigns/ad_detail.html", context)


@login_required
@require_POST
def ad_approve(request, ad_id):
    """Set ad status to approved via AJAX."""
    ad = get_object_or_404(Ad, id=ad_id, generator__campaign__created_by=request.user)
    ad.status = Ad.STATUS_APPROVED
    ad.save()
    return JsonResponse({
        "status": "success",
        "ad_id": ad.id,
        "new_status": ad.status,
    })


@login_required
@require_POST
def ad_reject(request, ad_id):
    """Set ad status to rejected via AJAX."""
    ad = get_object_or_404(Ad, id=ad_id, generator__campaign__created_by=request.user)
    ad.status = Ad.STATUS_REJECTED
    ad.save()
    return JsonResponse({
        "status": "success",
        "ad_id": ad.id,
        "new_status": ad.status,
    })


@login_required
@require_POST
def ad_message(request, ad_id):
    """Add a chat message to an ad via AJAX."""
    ad = get_object_or_404(Ad, id=ad_id, generator__campaign__created_by=request.user)

    try:
        data = json.loads(request.body)
        content = data.get("content", "").strip()

        if not content:
            return JsonResponse({"error": "Message content is required."}, status=400)

        message = AdMessage.objects.create(
            ad=ad,
            role=AdMessage.ROLE_USER,
            content=content,
        )

        return JsonResponse({
            "id": message.id,
            "role": message.role,
            "content": message.content,
            "created_at": message.created_at.isoformat(),
        })
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)


@login_required
def campaign_create(request):
    """Create a new campaign."""
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()

        # Basic validation
        if not name:
            messages.error(request, "Campaign name is required.")
            return redirect("campaigns:campaign_create")

        # Create the campaign
        campaign = Campaign.objects.create(
            name=name,
            description=description,
            created_by=request.user,
        )

        messages.success(request, f"Campaign '{name}' created successfully.")
        return redirect("campaigns:generator", campaign_id=campaign.id)

    context = {
        "page_title": "New Campaign",
        "is_edit": False,
        "form_action": reverse("campaigns:campaign_create"),
        "products": Campaign.PRODUCT_CHOICES,
        "campaign": None,
    }
    return render(request, "campaigns/campaign_form.html", context)


@login_required
def campaign_edit(request, campaign_id):
    """Edit an existing campaign."""
    campaign = get_object_or_404(Campaign, id=campaign_id, created_by=request.user)

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()

        # Basic validation
        if not name:
            messages.error(request, "Campaign name is required.")
            return redirect("campaigns:campaign_edit", campaign_id=campaign_id)

        # Update the campaign
        campaign.name = name
        campaign.description = description
        campaign.save()

        messages.success(request, f"Campaign '{name}' updated successfully.")
        return redirect("campaigns:dashboard")

    context = {
        "page_title": "Edit Campaign",
        "is_edit": True,
        "form_action": reverse("campaigns:campaign_edit", args=[campaign_id]),
        "products": Campaign.PRODUCT_CHOICES,
        "campaign": campaign,
    }
    return render(request, "campaigns/campaign_form.html", context)


@login_required
def generator_create(request, campaign_id):
    """Create a new generator within a campaign."""
    campaign = get_object_or_404(Campaign, id=campaign_id, created_by=request.user)
    personas = CustomerPersona.objects.filter(created_by=request.user)
    style_assets = Asset.objects.filter(created_by=request.user, asset_type=Asset.TYPE_STYLE)
    product_assets = Asset.objects.filter(created_by=request.user, asset_type=Asset.TYPE_PRODUCT)

    if request.method == "POST":
        title = request.POST.get("title", "").strip()
        brief = request.POST.get("brief", "").strip()
        headlines = request.POST.get("headlines", "").strip()
        persona_id = request.POST.get("customer_persona", "").strip()
        style_ref_ids = request.POST.getlist("style_references")
        product_ref_ids = request.POST.getlist("product_references")
        number_of_ads = request.POST.get("number_of_ads", "5")
        dimensions = request.POST.get("dimensions", "").strip()
        placement = request.POST.get("placement", "").strip()
        is_template = request.POST.get("is_template") == "on"

        # Basic validation
        if not title:
            messages.error(request, "Title is required.")
            return redirect("campaigns:generator_create", campaign_id=campaign_id)

        # Parse number_of_ads
        try:
            number_of_ads = int(number_of_ads)
        except ValueError:
            number_of_ads = 5

        # Get persona if selected
        persona = None
        if persona_id:
            try:
                persona = CustomerPersona.objects.get(id=persona_id, created_by=request.user)
            except CustomerPersona.DoesNotExist:
                pass

        # Create the generator
        generator = Generator.objects.create(
            campaign=campaign,
            title=title,
            brief=brief,
            headlines=headlines,
            customer_persona=persona,
            number_of_ads=number_of_ads,
            dimensions=dimensions,
            placement=placement,
            is_template=is_template,
        )

        # Set many-to-many relationships
        if style_ref_ids:
            generator.style_references.set(
                Asset.objects.filter(id__in=style_ref_ids, created_by=request.user, asset_type=Asset.TYPE_STYLE)
            )
        if product_ref_ids:
            generator.product_references.set(
                Asset.objects.filter(id__in=product_ref_ids, created_by=request.user, asset_type=Asset.TYPE_PRODUCT)
            )

        # Trigger generation task
        from .tasks import generate_ads_task

        print(f"[VIEW] generator_create: triggering generation for generator {generator.id}")
        generator.status = Generator.STATUS_PROCESSING
        generator.save(update_fields=["status"])
        result = generate_ads_task.delay(generator.id)
        print(f"[VIEW] generator_create: task dispatched with ID {result.id}")

        messages.success(request, f"Generator '{title}' created. Image generation started.")
        return redirect("campaigns:generator_list", campaign_id=campaign_id)

    context = {
        "page_title": "New Generator",
        "is_edit": False,
        "form_action": reverse("campaigns:generator_create", args=[campaign_id]),
        "campaign": campaign,
        "generator": None,
        "personas": personas,
        "style_assets": style_assets,
        "product_assets": product_assets,
    }
    return render(request, "campaigns/generator_form.html", context)


@login_required
@require_POST
def generate_ads(request, campaign_id, generator_id):
    """Trigger ad generation for a generator via Celery task."""
    from .tasks import generate_ads_task

    print(f"[VIEW] generate_ads called: campaign={campaign_id}, generator={generator_id}")

    campaign = get_object_or_404(Campaign, id=campaign_id, created_by=request.user)
    generator = get_object_or_404(Generator, id=generator_id, campaign=campaign)

    print(f"[VIEW] Generator found: {generator.title}, status={generator.status}")

    # Check if already processing
    if generator.status == Generator.STATUS_PROCESSING:
        print(f"[VIEW] Already processing, skipping")
        messages.warning(request, "Generation is already in progress.")
        return redirect("campaigns:generator_list", campaign_id=campaign_id)

    # Mark as processing immediately
    generator.status = Generator.STATUS_PROCESSING
    generator.save(update_fields=["status", "updated_at"])

    # Dispatch Celery task (non-blocking)
    print(f"[VIEW] Dispatching Celery task for generator {generator.id}")
    result = generate_ads_task.delay(generator.id)
    print(f"[VIEW] Task dispatched: {result.id}")

    messages.success(request, f"Image generation started for '{generator.title}'. Check back shortly.")
    return redirect("campaigns:generator_list", campaign_id=campaign_id)


@login_required
def generator_edit(request, campaign_id, generator_id):
    """Edit an existing generator."""
    campaign = get_object_or_404(Campaign, id=campaign_id, created_by=request.user)
    generator = get_object_or_404(Generator, id=generator_id, campaign=campaign)
    personas = CustomerPersona.objects.filter(created_by=request.user)
    style_assets = Asset.objects.filter(created_by=request.user, asset_type=Asset.TYPE_STYLE)
    product_assets = Asset.objects.filter(created_by=request.user, asset_type=Asset.TYPE_PRODUCT)
    view_only = request.GET.get("view") == "true"

    if request.method == "POST":
        title = request.POST.get("title", "").strip()
        brief = request.POST.get("brief", "").strip()
        headlines = request.POST.get("headlines", "").strip()
        persona_id = request.POST.get("customer_persona", "").strip()
        style_ref_ids = request.POST.getlist("style_references")
        product_ref_ids = request.POST.getlist("product_references")
        number_of_ads = request.POST.get("number_of_ads", "5")
        dimensions = request.POST.get("dimensions", "").strip()
        placement = request.POST.get("placement", "").strip()
        is_template = request.POST.get("is_template") == "on"

        # Basic validation
        if not title:
            messages.error(request, "Title is required.")
            return redirect("campaigns:generator_edit", campaign_id=campaign_id, generator_id=generator_id)

        # Parse number_of_ads
        try:
            number_of_ads = int(number_of_ads)
        except ValueError:
            number_of_ads = 5

        # Get persona if selected
        persona = None
        if persona_id:
            try:
                persona = CustomerPersona.objects.get(id=persona_id, created_by=request.user)
            except CustomerPersona.DoesNotExist:
                pass

        # Update the generator
        generator.title = title
        generator.brief = brief
        generator.headlines = headlines
        generator.customer_persona = persona
        generator.number_of_ads = number_of_ads
        generator.dimensions = dimensions
        generator.placement = placement
        generator.is_template = is_template
        generator.save()

        # Update many-to-many relationships
        generator.style_references.set(
            Asset.objects.filter(id__in=style_ref_ids, created_by=request.user, asset_type=Asset.TYPE_STYLE)
        )
        generator.product_references.set(
            Asset.objects.filter(id__in=product_ref_ids, created_by=request.user, asset_type=Asset.TYPE_PRODUCT)
        )

        messages.success(request, f"Generator '{title}' updated successfully.")
        return redirect("campaigns:generator_list", campaign_id=campaign_id)

    context = {
        "page_title": "View Generator" if view_only else "Edit Generator",
        "is_edit": True,
        "view_only": view_only,
        "form_action": reverse("campaigns:generator_edit", args=[campaign_id, generator_id]),
        "campaign": campaign,
        "generator": generator,
        "personas": personas,
        "style_assets": style_assets,
        "product_assets": product_assets,
    }
    return render(request, "campaigns/generator_form.html", context)


@login_required
@require_POST
def persona_create(request):
    """Create a new customer persona via AJAX."""
    try:
        data = json.loads(request.body)
        name = data.get("name", "").strip()
        description = data.get("description", "").strip()

        if not name:
            return JsonResponse({"error": "Name is required."}, status=400)

        persona = CustomerPersona.objects.create(
            name=name,
            description=description,
            created_by=request.user,
        )

        return JsonResponse({
            "id": persona.id,
            "name": persona.name,
            "description": persona.description,
        })
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)


@login_required
@require_POST
def asset_upload(request):
    """Upload an asset image via AJAX."""
    name = request.POST.get("name", "").strip()
    asset_type = request.POST.get("asset_type", "").strip()
    image = request.FILES.get("image")

    if asset_type not in [Asset.TYPE_STYLE, Asset.TYPE_PRODUCT]:
        return JsonResponse({"error": "Invalid asset type."}, status=400)

    if not image:
        return JsonResponse({"error": "Image is required."}, status=400)

    # Use filename as name if not provided
    if not name:
        name = image.name.rsplit(".", 1)[0] if "." in image.name else image.name

    asset = Asset.objects.create(
        name=name,
        asset_type=asset_type,
        image=image,
        created_by=request.user,
    )

    return JsonResponse({
        "id": asset.id,
        "name": asset.name,
        "asset_type": asset.asset_type,
        "image_url": asset.image.url,
    })


@login_required
def settings_view(request):
    """Settings page for API keys configuration."""
    api_settings = APISettings.get_settings()

    if request.method == "POST":
        primary_provider = request.POST.get("primary_provider", "").strip()
        gemini_key = request.POST.get("gemini_api_key", "").strip()
        openai_key = request.POST.get("openai_api_key", "").strip()
        gemini_model = request.POST.get("gemini_model", "").strip()

        if primary_provider:
            api_settings.primary_provider = primary_provider
        api_settings.gemini_api_key = gemini_key
        api_settings.openai_api_key = openai_key
        if gemini_model:
            api_settings.gemini_model = gemini_model
        api_settings.save()

        messages.success(request, "API settings updated successfully.")
        return redirect("campaigns:settings")

    context = {
        "active_nav": "settings",
        "api_settings": api_settings,
        "provider_choices": APISettings.PROVIDER_CHOICES,
        "gemini_model_choices": APISettings.GEMINI_MODEL_CHOICES,
    }
    return render(request, "campaigns/settings.html", context)
