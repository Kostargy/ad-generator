"""
Generation service that orchestrates the image generation pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.files import File

# Add image-gen to path
IMAGE_GEN_PATH = Path(__file__).resolve().parents[4] / "image-gen"
if str(IMAGE_GEN_PATH) not in sys.path:
    sys.path.insert(0, str(IMAGE_GEN_PATH))

from image_gen.config import load_config
from image_gen.generator import GeneratedImage, ImageGenerator

from .image_gen_adapter import ImageGenAdapter

if TYPE_CHECKING:
    from everdries_ad_generator.campaigns.models import Ad, Generator

logger = logging.getLogger(__name__)


class GenerationService:
    """Orchestrates image generation for a Django Generator."""

    def __init__(self, generator: "Generator"):
        self.generator = generator
        self.adapter = ImageGenAdapter(generator)
        self.config = load_config(IMAGE_GEN_PATH / "config.yaml")
        self.output_dir = Path(settings.MEDIA_ROOT) / "generated" / str(generator.id)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _load_api_settings(self):
        """Load API keys and model settings from database."""
        import os

        from everdries_ad_generator.campaigns.models import APISettings

        api_settings = APISettings.get_settings()
        if api_settings.gemini_api_key:
            os.environ["GEMINI_API_KEY"] = api_settings.gemini_api_key
        if api_settings.openai_api_key:
            os.environ["OPENAI_API_KEY"] = api_settings.openai_api_key

        # Override model from database settings
        if api_settings.gemini_model:
            self.config.gemini.image_model_name = api_settings.gemini_model
            logger.info("Using Gemini model: %s", api_settings.gemini_model)

        # Set provider priority
        if api_settings.primary_provider == "openai":
            self.config.primary_provider = "openai"
            self.config.fallback_provider = "gemini"
            logger.info("Primary provider: OpenAI, fallback: Gemini")
        elif api_settings.primary_provider == "gemini":
            self.config.primary_provider = "gemini"
            self.config.fallback_provider = "openai"
            logger.info("Primary provider: Gemini, fallback: OpenAI")
        elif api_settings.primary_provider == "gemini_only":
            self.config.primary_provider = "gemini"
            self.config.fallback_provider = "none"
            logger.info("Primary provider: Gemini only (no fallback)")
        elif api_settings.primary_provider == "openai_only":
            self.config.primary_provider = "openai"
            self.config.fallback_provider = "none"
            logger.info("Primary provider: OpenAI only (no fallback)")

    def run(self) -> list["Ad"]:
        """Run generation synchronously."""
        from everdries_ad_generator.campaigns.models import Ad, Generator

        ads_created: list[Ad] = []

        try:
            # Step 0: Load API keys and model settings from database
            self._load_api_settings()

            # Step 1: Mark processing (sync, before async)
            self.generator.status = Generator.STATUS_PROCESSING
            self.generator.save(update_fields=["status", "updated_at"])

            # Step 2: Build prompts (sync)
            prompts = self.adapter.build_prompts()
            if not prompts:
                logger.warning("No prompts for Generator %s", self.generator.id)
                self.generator.status = Generator.STATUS_FAILED
                self.generator.save(update_fields=["status", "updated_at"])
                return ads_created

            logger.info(
                "Generating %d images for Generator %s",
                len(prompts),
                self.generator.id,
            )

            # Step 3: Generate images (async part only)
            generated = asyncio.run(self._generate_images(prompts))

            # Step 4: Create Ad records (sync, after async)
            for img in generated:
                ad = self._create_ad(img)
                if ad:
                    ads_created.append(ad)

            # Step 5: Mark completed or failed based on results
            if ads_created:
                self.generator.status = Generator.STATUS_COMPLETED
                logger.info(
                    "Generator %s completed: %d ads created",
                    self.generator.id,
                    len(ads_created),
                )
            else:
                self.generator.status = Generator.STATUS_FAILED
                logger.warning(
                    "Generator %s failed: no ads created (all images failed)",
                    self.generator.id,
                )
            self.generator.save(update_fields=["status", "updated_at"])

        except Exception as e:
            logger.exception("Generator %s failed: %s", self.generator.id, e)
            self.generator.status = Generator.STATUS_FAILED
            self.generator.save(update_fields=["status", "updated_at"])
            raise

        return ads_created

    async def _generate_images(self, prompts: list) -> list[GeneratedImage]:
        """Generate images asynchronously (no Django ORM calls here)."""
        generator = ImageGenerator(
            config=self.config.gemini,
            output_dir=self.output_dir,
            checkpoint_dir=self.output_dir / "checkpoints",
            app_config=self.config,
        )
        return await generator.generate_batch(prompts=prompts, dry_run=False)

    def _create_ad(self, gen_image: GeneratedImage) -> "Ad | None":
        """Create an Ad record from a generated image."""
        from everdries_ad_generator.campaigns.models import Ad

        try:
            ad = Ad(
                generator=self.generator,
                headline=gen_image.prompt.image_prompt_name[:255],
                status=Ad.STATUS_PENDING,
            )

            # Save image file
            source = Path(gen_image.image_path)
            if source.exists():
                filename = f"ad_{self.generator.id}_{source.name}"
                with open(source, "rb") as f:
                    ad.image.save(filename, File(f), save=False)

            # Store metadata for revisions
            ad.generation_metadata = {
                "prompt_text": gen_image.prompt.prompt_text,
                "product_name": gen_image.prompt.product_name,
                "aspect_ratio": gen_image.prompt.aspect_ratio,
                "style_variant": gen_image.prompt.style_variant,
                "reference_image_paths": [str(p) for p in gen_image.prompt.reference_images],
                "style_reference_path": (
                    str(gen_image.prompt.style_reference)
                    if gen_image.prompt.style_reference
                    else None
                ),
                "generation_id": gen_image.generation_id,
                "timestamp": gen_image.timestamp,
            }

            ad.save()
            return ad

        except Exception as e:
            logger.error("Failed to create Ad: %s", e)
            return None

    def get_estimated_count(self) -> int:
        """Return estimated image count."""
        return self.adapter.get_estimated_count()
