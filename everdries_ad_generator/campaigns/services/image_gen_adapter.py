"""
Adapter to convert Django Generator model to GenerationPrompt objects.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from everdries_ad_generator.campaigns.models import Asset, Generator


@dataclass
class GenerationPrompt:
    """A single fully-resolved prompt ready to send to the image model."""

    prompt_text: str
    reference_images: list[Path] = field(default_factory=list)
    logo_images: list[Path] = field(default_factory=list)
    style_reference: Path | None = None
    product_name: str = ""
    image_prompt_name: str = ""
    aspect_ratio: str = ""
    style_variant: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_text": self.prompt_text,
            "reference_images": [str(p) for p in self.reference_images],
            "logo_images": [str(p) for p in self.logo_images],
            "style_reference": str(self.style_reference) if self.style_reference else None,
            "product_name": self.product_name,
            "image_prompt_name": self.image_prompt_name,
            "aspect_ratio": self.aspect_ratio,
            "style_variant": self.style_variant,
        }

# Dimension to aspect ratio mapping. 16:9 is intentionally absent —
# gemini-2.5-flash-image returns IMAGE_OTHER on every request when
# asked for 16:9 with reference photos. Any stale generator pointing
# at a removed dimension falls back to DEFAULT_ASPECT_RATIO (1:1),
# which generates successfully.
DIMENSION_TO_ASPECT = {
    "1080x1080": "1:1",
    "1080x1350": "4:5",
    "1080x1920": "9:16",
    "1200x1200": "1:1",
}

DEFAULT_ASPECT_RATIO = "1:1"


@dataclass
class GeneratorConfig:
    """Configuration extracted from Django Generator."""

    product_name: str
    product_context: str
    headlines: list[str]
    brief: str
    product_reference_paths: list[Path]
    style_reference_paths: list[Path]
    aspect_ratio: str
    persona_description: str = ""


class ImageGenAdapter:
    """Converts Django Generator to GenerationPrompt objects."""

    def __init__(self, generator: "Generator"):
        self.generator = generator
        self.campaign = generator.campaign
        # Holds downloaded copies of remote (S3) asset files for the lifetime
        # of this adapter; auto-cleaned when the adapter is garbage collected.
        self._tmp_dir = tempfile.TemporaryDirectory(prefix="image_gen_assets_")
        self.config = self._extract_config()

    def _materialize_asset(self, asset: "Asset") -> Path:
        """Return a local Path for an Asset, downloading from remote storage if needed."""
        try:
            # Local filesystem storage exposes .path directly.
            return Path(asset.image.path)
        except NotImplementedError:
            # Remote storage (e.g. S3): download to a temp file.
            suffix = Path(asset.image.name).suffix
            local_path = Path(self._tmp_dir.name) / f"{asset.pk}{suffix}"
            with asset.image.open("rb") as src, local_path.open("wb") as dst:
                for chunk in src.chunks():
                    dst.write(chunk)
            return local_path

    def _extract_config(self) -> GeneratorConfig:
        """Extract config from Generator and Campaign models."""
        return GeneratorConfig(
            product_name=self.campaign.name,
            product_context=self.campaign.description or "",
            headlines=[
                line.strip()
                for line in (self.generator.headlines or "").split("\n")
                if line.strip()
            ],
            brief=self.generator.brief or "",
            product_reference_paths=[
                self._materialize_asset(asset)
                for asset in self.generator.product_references.all()
                if asset.image
            ],
            style_reference_paths=[
                self._materialize_asset(asset)
                for asset in self.generator.style_references.all()
                if asset.image
            ],
            aspect_ratio=DIMENSION_TO_ASPECT.get(
                self.generator.dimensions or "", DEFAULT_ASPECT_RATIO
            ),
            persona_description=(
                self.generator.customer_persona.description
                if self.generator.customer_persona
                else ""
            ),
        )

    def build_prompts(self) -> list[GenerationPrompt]:
        """Build GenerationPrompt for each headline × style combination."""
        prompts: list[GenerationPrompt] = []
        cfg = self.config

        if not cfg.headlines:
            return prompts

        # Use [None] sentinels so we still emit at least one prompt per headline
        # when style or product references are absent.
        style_refs = cfg.style_reference_paths or [None]
        product_refs = cfg.product_reference_paths or [None]

        for headline in cfg.headlines:
            for style_idx, style_path in enumerate(style_refs):
                for prod_idx, prod_path in enumerate(product_refs):
                    prompt_text = self._format_prompt(
                        headline=headline,
                        has_style_ref=style_path is not None,
                    )

                    # Build a name that distinguishes each cell in the fan-out
                    # so Ad rows are identifiable in the UI.
                    name_parts = [headline[:40]]
                    if len(style_refs) > 1 and style_path:
                        name_parts.append(f"v{style_idx + 1}")
                    if len(product_refs) > 1 and prod_path:
                        name_parts.append(f"p{prod_idx + 1}")
                    name = " ".join(name_parts)

                    prompts.append(
                        GenerationPrompt(
                            prompt_text=prompt_text,
                            reference_images=[prod_path] if prod_path else [],
                            logo_images=[],
                            style_reference=style_path,
                            product_name=cfg.product_name,
                            image_prompt_name=name[:255],
                            aspect_ratio=cfg.aspect_ratio,
                            style_variant=style_idx,
                        )
                    )

        return prompts

    def _get_master_prompt(self) -> str:
        """Get master prompt from APISettings."""
        try:
            from everdries_ad_generator.campaigns.models import APISettings
            api_settings = APISettings.get_settings()
            return api_settings.master_prompt or APISettings.DEFAULT_MASTER_PROMPT
        except Exception:
            return ""

    def _format_prompt(self, headline: str, has_style_ref: bool = False) -> str:
        """Build the prompt text for image generation."""
        cfg = self.config
        parts = []

        # Intro
        parts.append(f"Create a social media advertisement image for {cfg.product_name}.")

        # Product context from campaign description
        if cfg.product_context:
            parts.append(f"\nPRODUCT CONTEXT:\n{cfg.product_context}")

        # Reference image instructions
        parts.append(
            "\nREFERENCE IMAGES (CRITICAL): I have provided labeled product "
            "reference photos. These are REAL product photos that MUST appear "
            "in the final ad AS-IS. Do NOT regenerate, redraw, or modify them. "
            "Place the actual photo into the ad design. Add text and graphics "
            "AROUND and ON TOP of the real photo."
        )

        # Style reference instructions
        if has_style_ref:
            parts.append(
                "\nSTYLE REFERENCE: Replicate the layout structure, typography, "
                "and composition from the style reference — but use ONLY the "
                "product/model photos from the separate reference images."
            )

        # The headline/creative direction
        parts.append(f"\nIMAGE DIRECTION:\n{headline}")

        # Brief/additional instructions
        if cfg.brief:
            parts.append(f"\n{cfg.brief}")

        # Aspect ratio
        parts.append(f"\nASPECT RATIO: {cfg.aspect_ratio}")

        # Brand tone
        if cfg.persona_description:
            parts.append(f"\nBRAND TONE: {cfg.persona_description}")

        # Master prompt (global brand guidelines)
        master_prompt = self._get_master_prompt()
        if master_prompt:
            parts.append(f"\nGLOBAL GUIDELINES:\n{master_prompt}")

        parts.append("\nGenerate a high-quality advertisement image.")

        return "\n".join(parts)

    def get_estimated_count(self) -> int:
        """Return estimated number of images to generate."""
        return (
            len(self.config.headlines)
            * max(1, len(self.config.style_reference_paths))
            * max(1, len(self.config.product_reference_paths))
        )
