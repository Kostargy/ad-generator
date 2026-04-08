"""
Generation service that orchestrates the image generation pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.files import File

from .critic_service import CritiqueResult, ImageCritic
from .image_gen import GeneratedImage, ImageGenerator, RevisionContext
from .image_gen_adapter import ImageGenAdapter

if TYPE_CHECKING:
    from everdries_ad_generator.campaigns.models import Ad, Generator

logger = logging.getLogger(__name__)


class GenerationService:
    """Orchestrates image generation for a Django Generator."""

    def __init__(self, generator: "Generator"):
        self.generator = generator
        self.adapter = ImageGenAdapter(generator)
        self.output_dir = Path(settings.MEDIA_ROOT) / "generated" / str(generator.id)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.critic = ImageCritic()
        self.image_generator: ImageGenerator | None = None
        # Settings loaded from database
        self._gemini_model: str | None = None
        self._primary_provider: str = "gemini"
        self._fallback_provider: str = "openai"

    def _load_api_settings(self) -> None:
        """Load API keys and model settings from database."""
        from everdries_ad_generator.campaigns.models import APISettings

        api_settings = APISettings.get_settings()
        if api_settings.gemini_api_key:
            os.environ["GEMINI_API_KEY"] = api_settings.gemini_api_key
        if api_settings.openai_api_key:
            os.environ["OPENAI_API_KEY"] = api_settings.openai_api_key

        # Store model from database settings
        self._gemini_model = api_settings.gemini_model or None
        if self._gemini_model:
            logger.info("Using Gemini model: %s", self._gemini_model)

        # Set provider priority
        if api_settings.primary_provider == "openai":
            self._primary_provider = "openai"
            self._fallback_provider = "gemini"
            logger.info("Primary provider: OpenAI, fallback: Gemini")
        elif api_settings.primary_provider == "gemini":
            self._primary_provider = "gemini"
            self._fallback_provider = "openai"
            logger.info("Primary provider: Gemini, fallback: OpenAI")
        elif api_settings.primary_provider == "gemini_only":
            self._primary_provider = "gemini"
            self._fallback_provider = "none"
            logger.info("Primary provider: Gemini only (no fallback)")
        elif api_settings.primary_provider == "openai_only":
            self._primary_provider = "openai"
            self._fallback_provider = "none"
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

            # Step 4: Critique & auto-revise images
            logger.info("Critiquing %d images...", len(generated))
            generated, critiques = self._critique_and_revise(generated)

            # Step 5: Create Ad records (sync, after async)
            for img, critique in zip(generated, critiques):
                ad = self._create_ad(img, critique)
                if ad:
                    ads_created.append(ad)

            # Step 6: Mark completed or failed based on results
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
        self.image_generator = ImageGenerator(
            output_dir=self.output_dir,
            checkpoint_dir=self.output_dir / "checkpoints",
            gemini_model=self._gemini_model,
            primary_provider=self._primary_provider,
            fallback_provider=self._fallback_provider,
        )
        return await self.image_generator.generate_batch(prompts=prompts, dry_run=False)

    def _critique_and_revise(
        self, images: list[GeneratedImage], max_revisions: int = 2
    ) -> tuple[list[GeneratedImage], list[CritiqueResult]]:
        """Critique images and auto-revise if needed.

        For each image:
        1. Critique against 7 quality checks
        2. If issues found, revise from original and re-critique
        3. Keep the best-scoring version
        """
        final_images: list[GeneratedImage] = []
        critiques: list[CritiqueResult] = []

        product_name = self.generator.campaign.name

        for img in images:
            try:
                # Initial critique
                critique = self.critic.critique(
                    image_path=Path(img.image_path),
                    product_name=product_name,
                    prompt_name=img.prompt.image_prompt_name,
                    expected_price="5 for $69.95",
                    aspect_ratio=img.prompt.aspect_ratio,
                )

                logger.info(
                    "Critique %s: score=%.1f, passed=%s, issues=%d",
                    Path(img.image_path).name,
                    critique.overall_score,
                    critique.passed,
                    len(critique.issues),
                )

                best_image = img
                best_critique = critique
                revision_count = 0

                # Auto-revise loop if needed
                if critique.needs_revision and self.image_generator:
                    for rev_num in range(max_revisions):
                        if not critique.needs_revision:
                            break

                        logger.info(
                            "Revising %s (attempt %d): %s",
                            Path(img.image_path).name,
                            rev_num + 1,
                            critique.revision_instructions[:100],
                        )

                        # Build revision context
                        context = RevisionContext(
                            reference_images=[
                                Path(p) for p in img.prompt.reference_images if p
                            ],
                            logo_images=[],
                            style_reference=(
                                Path(img.prompt.style_reference)
                                if img.prompt.style_reference
                                else None
                            ),
                            prompt_text=img.prompt.prompt_text,
                            aspect_ratio=img.prompt.aspect_ratio,
                            product_name=img.prompt.product_name,
                        )

                        # Revise from ORIGINAL image
                        try:
                            revised_path = asyncio.run(
                                self.image_generator.revise_image(
                                    image_path=Path(img.image_path),
                                    instructions=critique.revision_instructions,
                                    context=context,
                                )
                            )

                            revision_count += 1

                            # Re-critique the revision
                            rev_critique = self.critic.critique(
                                image_path=revised_path,
                                product_name=product_name,
                                prompt_name=img.prompt.image_prompt_name,
                                expected_price="5 for $69.95",
                                aspect_ratio=img.prompt.aspect_ratio,
                            )

                            logger.info(
                                "Revision %d critique: score=%.1f, passed=%s",
                                rev_num + 1,
                                rev_critique.overall_score,
                                rev_critique.passed,
                            )

                            # Keep if better
                            if rev_critique.overall_score > best_critique.overall_score:
                                # Create a new GeneratedImage with revised path
                                best_image = GeneratedImage(
                                    image_path=str(revised_path),
                                    prompt=img.prompt,
                                    generation_id=img.generation_id,
                                    timestamp=img.timestamp,
                                )
                                best_critique = rev_critique

                            # Stop if passed or no improvement
                            if (
                                rev_critique.passed
                                or rev_critique.overall_score <= critique.overall_score
                            ):
                                break

                            critique = rev_critique

                        except Exception as e:
                            logger.warning("Revision failed: %s", e)
                            break

                # Store revision count on critique for _create_ad
                best_critique._revision_count = revision_count

                final_images.append(best_image)
                critiques.append(best_critique)

            except Exception as e:
                logger.warning("Critique failed for %s: %s", img.image_path, e)
                # Create a default critique result
                default_critique = CritiqueResult(
                    image_path=Path(img.image_path),
                    overall_score=5.0,
                    passed=True,
                    summary=f"Critique skipped: {e}",
                )
                default_critique._revision_count = 0
                final_images.append(img)
                critiques.append(default_critique)

        passed = sum(1 for c in critiques if c.passed)
        logger.info(
            "Critique complete: %d/%d passed, %d revised",
            passed,
            len(critiques),
            sum(1 for c in critiques if getattr(c, "_revision_count", 0) > 0),
        )

        return final_images, critiques

    def _create_ad(
        self, gen_image: GeneratedImage, critique: CritiqueResult | None = None
    ) -> "Ad | None":
        """Create an Ad record from a generated image with critique data."""
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

            # Store critique data if available
            if critique:
                revision_count = getattr(critique, "_revision_count", 0)
                ad.critique_score = critique.overall_score
                ad.critique_passed = critique.passed
                ad.critique_summary = critique.summary
                ad.critique_data = critique.to_dict()
                ad.was_auto_revised = revision_count > 0
                ad.revision_count = revision_count

            ad.save()
            return ad

        except Exception as e:
            logger.error("Failed to create Ad: %s", e)
            return None

    def get_estimated_count(self) -> int:
        """Return estimated image count."""
        return self.adapter.get_estimated_count()
