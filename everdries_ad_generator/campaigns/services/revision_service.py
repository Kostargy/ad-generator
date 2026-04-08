"""
Revision service that handles ad image revisions based on user instructions.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings

from .image_gen import ImageGenerator, RevisionContext

if TYPE_CHECKING:
    from everdries_ad_generator.campaigns.models import Ad

logger = logging.getLogger(__name__)


class RevisionService:
    """Handles ad image revisions using the local image_gen package."""

    def __init__(self, ad: "Ad"):
        self.ad = ad
        self.output_dir = Path(settings.MEDIA_ROOT) / "revisions" / str(ad.id)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        # Holds a downloaded copy of the ad's current image when storage is remote.
        self._tmp_dir = tempfile.TemporaryDirectory(prefix="revise_")
        # Settings loaded from database
        self._gemini_model: str | None = None
        self._primary_provider: str = "gemini"
        self._fallback_provider: str = "openai"

    def _materialize_current_image(self) -> Path:
        """Return a local Path for the ad's current image, downloading from remote storage if needed."""
        return self._materialize_image_field(self.ad.image, "current")

    def _materialize_image_field(self, image_field, basename: str) -> Path:
        """Download an ImageField to a temp file when storage is remote."""
        try:
            return Path(image_field.path)
        except NotImplementedError:
            suffix = Path(image_field.name).suffix or ".png"
            local_path = Path(self._tmp_dir.name) / f"{basename}{suffix}"
            with image_field.open("rb") as src, local_path.open("wb") as dst:
                for chunk in src.chunks():
                    dst.write(chunk)
            return local_path

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
        elif api_settings.primary_provider == "gemini":
            self._primary_provider = "gemini"
            self._fallback_provider = "openai"
        elif api_settings.primary_provider == "gemini_only":
            self._primary_provider = "gemini"
            self._fallback_provider = "none"
        elif api_settings.primary_provider == "openai_only":
            self._primary_provider = "openai"
            self._fallback_provider = "none"

    def _build_context(self) -> RevisionContext:
        """Build RevisionContext: reference assets come from the Generator's M2M
        fields (durable), prompt/aspect/etc from the stored generation_metadata.
        """
        meta = self.ad.generation_metadata or {}
        generator = self.ad.generator

        # Materialize product references (downloaded from S3 to /tmp if remote)
        reference_images: list[Path] = []
        for asset in generator.product_references.all():
            if asset.image:
                reference_images.append(
                    self._materialize_image_field(asset.image, f"ref_{asset.pk}")
                )

        # Pick the same style reference variant the original generation used
        style_reference: Path | None = None
        style_assets = list(generator.style_references.all())
        if style_assets:
            idx = meta.get("style_variant", 0) or 0
            chosen = style_assets[idx] if 0 <= idx < len(style_assets) else style_assets[0]
            if chosen.image:
                style_reference = self._materialize_image_field(
                    chosen.image, f"style_{chosen.pk}"
                )

        return RevisionContext(
            reference_images=reference_images,
            logo_images=[],
            style_reference=style_reference,
            prompt_text=meta.get("prompt_text", ""),
            aspect_ratio=meta.get("aspect_ratio", "1:1"),
            product_name=meta.get("product_name", ""),
        )

    def run(self, instructions: str) -> Path:
        """Run revision synchronously and return path to revised image."""
        logger.info("Starting revision for Ad %s: %s", self.ad.id, instructions[:100])

        # Load API keys
        self._load_api_settings()

        # Build context from stored metadata
        context = self._build_context()
        logger.info("Revision context: %s", context.summary())

        # Get current image path (downloads from S3 if remote)
        if not self.ad.image:
            raise ValueError("Ad has no image to revise")
        image_path = self._materialize_current_image()

        # Run async revision
        revised_path = asyncio.run(self._revise_async(image_path, instructions, context))

        logger.info("Revision complete: %s", revised_path)
        return revised_path

    async def _revise_async(
        self, image_path: Path, instructions: str, context: RevisionContext
    ) -> Path:
        """Run the actual revision asynchronously."""
        generator = ImageGenerator(
            output_dir=self.output_dir,
            checkpoint_dir=self.output_dir / "checkpoints",
            gemini_model=self._gemini_model,
            primary_provider=self._primary_provider,
            fallback_provider=self._fallback_provider,
        )

        revised_path = await generator.revise_image(
            image_path=image_path,
            instructions=instructions,
            context=context,
        )

        return revised_path
