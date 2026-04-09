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
from django.utils import timezone

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
        # Streaming state: Ad records keyed by their batch index. Populated
        # by the on_image_saved callback during generate_batch, then read
        # by _critique_and_revise to update each row in place.
        self._ads_by_index: dict[int, "Ad"] = {}
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

            # Step 1: Build prompts (sync) so we know the real total
            prompts = self.adapter.build_prompts()
            if not prompts:
                logger.warning("No prompts for Generator %s", self.generator.id)
                self.generator.status = Generator.STATUS_FAILED
                self.generator.save(update_fields=["status", "updated_at"])
                return ads_created

            # Step 2: Mark processing with progress fields populated.
            # Reset ads count is implicit because the row count is read live.
            self.generator.status = Generator.STATUS_PROCESSING
            self.generator.started_at = timezone.now()
            self.generator.total_expected = len(prompts)
            self.generator.save(
                update_fields=[
                    "status",
                    "started_at",
                    "total_expected",
                    "updated_at",
                ]
            )

            logger.info(
                "Generating %d images for Generator %s",
                len(prompts),
                self.generator.id,
            )

            # Step 3: Generate images. Each successful image is persisted as
            # an Ad row immediately via the on_image_saved callback inside
            # generate_batch, so partial successes are visible in the UI
            # even if the worker dies mid-run.
            generated = asyncio.run(self._generate_images(prompts))

            # Step 4: Critique & auto-revise images
            logger.info("Critiquing %d images...", len(generated))
            generated, critiques = self._critique_and_revise(generated)

            # Step 5: Update existing Ad rows with critique data (and the
            # revised image, if critique produced one).
            for img, critique in zip(generated, critiques):
                index = self._index_of(img)
                self._update_ad_with_critique(index, img, critique)

            ads_created = list(self._ads_by_index.values())

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
        """Generate images asynchronously.

        The on_image_saved callback runs in a thread executor (see
        ImageGenerator.generate_batch), so it can safely use sync Django ORM.
        """
        self.image_generator = ImageGenerator(
            output_dir=self.output_dir,
            checkpoint_dir=self.output_dir / "checkpoints",
            gemini_model=self._gemini_model,
            primary_provider=self._primary_provider,
            fallback_provider=self._fallback_provider,
        )
        return await self.image_generator.generate_batch(
            prompts=prompts,
            dry_run=False,
            on_image_saved=self._create_ad_for_index,
        )

    def _create_ad_for_index(self, index: int, gen_image: GeneratedImage) -> None:
        """Callback fired by generate_batch the moment an image lands on disk."""
        ad = self._create_ad(gen_image, critique=None)
        if ad:
            self._ads_by_index[index] = ad
            logger.info("Streamed Ad %s for image index %d", ad.id, index)

    def _index_of(self, gen_image: GeneratedImage) -> int:
        """Recover the batch index from a GeneratedImage's generation_id."""
        gid = gen_image.generation_id or ""
        # generation_id format: "gen-0007"
        try:
            return int(gid.split("-")[-1])
        except (ValueError, IndexError):
            return -1

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

    def _update_ad_with_critique(
        self,
        index: int,
        gen_image: GeneratedImage,
        critique: CritiqueResult,
    ) -> None:
        """Update the streamed Ad row with critique data + revised image.

        If the streaming callback never managed to create the Ad (e.g. it
        raised), fall back to creating it now from scratch so we don't lose
        the image entirely.
        """
        from everdries_ad_generator.campaigns.models import Ad  # noqa: F401

        ad = self._ads_by_index.get(index)
        if ad is None:
            ad = self._create_ad(gen_image, critique)
            if ad and index >= 0:
                self._ads_by_index[index] = ad
            return

        # If critique produced a revised image, swap the file on the Ad row.
        source = Path(gen_image.image_path)
        try:
            current_name = Path(ad.image.name).name if ad.image else ""
        except Exception:
            current_name = ""
        if source.exists() and source.name != current_name:
            try:
                with open(source, "rb") as f:
                    ad.image.save(
                        f"ad_{self.generator.id}_{source.name}",
                        File(f),
                        save=False,
                    )
            except Exception as e:
                logger.warning("Failed to swap revised image on Ad %s: %s", ad.id, e)

        revision_count = getattr(critique, "_revision_count", 0)
        ad.critique_score = critique.overall_score
        ad.critique_passed = critique.passed
        ad.critique_summary = critique.summary
        ad.critique_data = critique.to_dict()
        ad.was_auto_revised = revision_count > 0
        ad.revision_count = revision_count
        try:
            ad.save()
        except Exception as e:
            logger.error("Failed to save critique data on Ad %s: %s", ad.id, e)

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
