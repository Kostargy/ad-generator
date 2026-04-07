"""
Revision service that handles ad image revisions based on user instructions.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings

# Add image-gen to path
IMAGE_GEN_PATH = Path(__file__).resolve().parents[4] / "image-gen"
if str(IMAGE_GEN_PATH) not in sys.path:
    sys.path.insert(0, str(IMAGE_GEN_PATH))

from image_gen.config import load_config
from image_gen.generator import ImageGenerator, RevisionContext

if TYPE_CHECKING:
    from everdries_ad_generator.campaigns.models import Ad

logger = logging.getLogger(__name__)


class RevisionService:
    """Handles ad image revisions using the image-gen library."""

    def __init__(self, ad: "Ad"):
        self.ad = ad
        self.config = load_config(IMAGE_GEN_PATH / "config.yaml")
        self.output_dir = Path(settings.MEDIA_ROOT) / "revisions" / str(ad.id)
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
        elif api_settings.primary_provider == "gemini":
            self.config.primary_provider = "gemini"
            self.config.fallback_provider = "openai"
        elif api_settings.primary_provider == "gemini_only":
            self.config.primary_provider = "gemini"
            self.config.fallback_provider = "none"
        elif api_settings.primary_provider == "openai_only":
            self.config.primary_provider = "openai"
            self.config.fallback_provider = "none"

    def _build_context(self) -> RevisionContext:
        """Build RevisionContext from ad.generation_metadata."""
        meta = self.ad.generation_metadata or {}

        return RevisionContext(
            reference_images=[Path(p) for p in meta.get("reference_image_paths", []) if p],
            logo_images=[],
            style_reference=Path(meta["style_reference_path"]) if meta.get("style_reference_path") else None,
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

        # Get current image path
        if not self.ad.image:
            raise ValueError("Ad has no image to revise")
        image_path = Path(self.ad.image.path)

        # Run async revision
        revised_path = asyncio.run(self._revise_async(image_path, instructions, context))

        logger.info("Revision complete: %s", revised_path)
        return revised_path

    async def _revise_async(
        self, image_path: Path, instructions: str, context: RevisionContext
    ) -> Path:
        """Run the actual revision asynchronously."""
        generator = ImageGenerator(
            config=self.config.gemini,
            output_dir=self.output_dir,
            checkpoint_dir=self.output_dir / "checkpoints",
            app_config=self.config,
        )

        revised_path = await generator.revise_image(
            image_path=image_path,
            instructions=instructions,
            context=context,
        )

        return revised_path
