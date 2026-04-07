"""
Celery tasks for async image generation.
"""

import logging
import traceback

from celery import shared_task

logger = logging.getLogger(__name__)


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
