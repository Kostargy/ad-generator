"""
Celery tasks for async image generation and revision.
"""

import json
import logging
import re
import traceback

from celery import shared_task
from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)


def _friendly_error_message(exc: Exception) -> str:
    """Extract a user-facing message from a provider exception.

    OpenAI/Gemini SDK exceptions stringify as `Error code: 400 - {...json...}`.
    Pull the inner `error.message` if we can; otherwise fall back to str(exc).
    """
    raw = str(exc)
    # Try to find a JSON-ish payload after the dash
    match = re.search(r"\{.*\}", raw)
    if match:
        payload = match.group(0)
        try:
            data = json.loads(payload.replace("'", '"'))
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            err = data.get("error") if isinstance(data.get("error"), dict) else data
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])
    return raw


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def generate_ads_task(self, generator_id: int) -> dict:
    """Generate ads for a Generator asynchronously."""
    print(f"\n{'='*60}")
    print(f"[CELERY TASK] generate_ads_task started")
    print(f"[CELERY TASK] Generator ID: {generator_id}")
    print(f"[CELERY TASK] Task ID: {self.request.id}")
    print(f"{'='*60}\n")

    from everdries_ad_generator.campaigns.models import Generator
    from everdries_ad_generator.campaigns.services import GenerationService

    try:
        generator = Generator.objects.get(id=generator_id)
        print(f"[CELERY TASK] Found generator: {generator.title}")
    except Generator.DoesNotExist:
        print(f"[CELERY TASK] ERROR: Generator {generator_id} not found")
        return {"status": "error", "message": f"Generator {generator_id} not found"}

    try:
        print(f"[CELERY TASK] Creating GenerationService...")
        service = GenerationService(generator)

        print(f"[CELERY TASK] Estimated images: {service.get_estimated_count()}")
        print(f"[CELERY TASK] Starting generation...")

        ads = service.run()

        print(f"\n{'='*60}")
        if ads:
            print(f"[CELERY TASK] SUCCESS: {len(ads)} ads created")
            status = "success"
        else:
            print(f"[CELERY TASK] FAILED: 0 ads created (all images failed)")
            status = "failed"
        print(f"{'='*60}\n")

        return {
            "status": status,
            "generator_id": generator_id,
            "ads_created": len(ads),
            "ad_ids": [ad.id for ad in ads],
        }

    except Exception as exc:
        print(f"\n{'='*60}")
        print(f"[CELERY TASK] ERROR: {exc}")
        print(f"[CELERY TASK] Traceback:")
        traceback.print_exc()
        print(f"{'='*60}\n")

        # Mark as failed
        try:
            generator.status = Generator.STATUS_FAILED
            generator.save(update_fields=["status", "updated_at"])
        except Exception:
            pass

        # Retry with backoff
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def revise_ad_task(self, ad_id: int, message_id: int) -> dict:
    """Revise an ad image based on user instructions."""
    print(f"\n{'='*60}")
    print(f"[CELERY TASK] revise_ad_task started")
    print(f"[CELERY TASK] Ad ID: {ad_id}, Message ID: {message_id}")
    print(f"[CELERY TASK] Task ID: {self.request.id}")
    print(f"{'='*60}\n")

    from everdries_ad_generator.campaigns.models import Ad, AdMessage
    from everdries_ad_generator.campaigns.services.revision_service import RevisionService

    try:
        ad = Ad.objects.get(id=ad_id)
        message = AdMessage.objects.get(id=message_id)
        print(f"[CELERY TASK] Found ad: {ad.headline}")
        print(f"[CELERY TASK] Instructions: {message.content[:100]}")
    except (Ad.DoesNotExist, AdMessage.DoesNotExist) as e:
        print(f"[CELERY TASK] ERROR: {e}")
        return {"status": "error", "message": str(e)}

    try:
        print(f"[CELERY TASK] Creating RevisionService...")
        service = RevisionService(ad)

        print(f"[CELERY TASK] Starting revision...")
        revised_path = service.run(message.content)

        # Update ad with new image
        print(f"[CELERY TASK] Saving revised image...")
        with open(revised_path, "rb") as f:
            ad.image.save(f"revised_{ad.id}.png", ContentFile(f.read()), save=True)

        # Create assistant response
        AdMessage.objects.create(
            ad=ad,
            role=AdMessage.ROLE_ASSISTANT,
            content="Done! I've updated the image based on your request.",
        )

        print(f"\n{'='*60}")
        print(f"[CELERY TASK] SUCCESS: Image revised")
        print(f"{'='*60}\n")

        return {
            "status": "success",
            "ad_id": ad_id,
            "image_url": ad.image.url,
        }

    except Exception as exc:
        print(f"\n{'='*60}")
        print(f"[CELERY TASK] ERROR: {exc}")
        print(f"[CELERY TASK] Traceback:")
        traceback.print_exc()
        print(f"{'='*60}\n")

        # Create error response message
        AdMessage.objects.create(
            ad=ad,
            role=AdMessage.ROLE_ASSISTANT,
            content=f"Sorry, I couldn't complete the revision: {_friendly_error_message(exc)}",
            is_error=True,
        )

        # Don't retry on revision errors - user can send another message
        return {
            "status": "error",
            "ad_id": ad_id,
            "error": str(exc),
        }
