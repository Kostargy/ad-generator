"""
Adapter to convert Django Generator model to image-gen GenerationPrompt objects.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

# Add image-gen to path
IMAGE_GEN_PATH = Path(__file__).resolve().parents[4] / "image-gen"
if str(IMAGE_GEN_PATH) not in sys.path:
    sys.path.insert(0, str(IMAGE_GEN_PATH))

from image_gen.prompt_builder import GenerationPrompt

if TYPE_CHECKING:
    from everdries_ad_generator.campaigns.models import Generator

# Dimension to aspect ratio mapping
DIMENSION_TO_ASPECT = {
    "1080x1080": "1:1",
    "1080x1350": "4:5",
    "1080x1920": "9:16",
    "1200x628": "1.91:1",
    "1920x1080": "16:9",
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
        self.config = self._extract_config()

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
                Path(asset.image.path)
                for asset in self.generator.product_references.all()
                if asset.image
            ],
            style_reference_paths=[
                Path(asset.image.path)
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

        # Use [None] if no style refs to still generate one per headline
        style_refs = cfg.style_reference_paths or [None]

        for headline in cfg.headlines:
            for style_idx, style_path in enumerate(style_refs):
                prompt_text = self._format_prompt(
                    headline=headline,
                    has_style_ref=style_path is not None,
                )

                # Label with variant number if multiple styles
                if len(style_refs) > 1 and style_path:
                    name = f"{headline[:50]} (v{style_idx + 1})"
                else:
                    name = headline[:50]

                prompts.append(
                    GenerationPrompt(
                        prompt_text=prompt_text,
                        reference_images=list(cfg.product_reference_paths),
                        logo_images=[],
                        style_reference=style_path,
                        product_name=cfg.product_name,
                        image_prompt_name=name,
                        aspect_ratio=cfg.aspect_ratio,
                        style_variant=style_idx,
                    )
                )

        return prompts

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
        else:
            parts.append(
                "\nBRAND: Warm, relatable, empowering tone. "
                "No Trustpilot. No 'premium' messaging. No logo."
            )

        parts.append("\nGenerate a high-quality advertisement image.")

        return "\n".join(parts)

    def get_estimated_count(self) -> int:
        """Return estimated number of images to generate."""
        return len(self.config.headlines) * max(1, len(self.config.style_reference_paths))
