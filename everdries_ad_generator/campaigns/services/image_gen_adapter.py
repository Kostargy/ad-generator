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
    supplementary_copy: list[str]
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
            supplementary_copy=[
                line.strip()
                for line in (self.generator.supplementary_copy or "").split("\n")
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

        # Product context from campaign description.
        # Framed as background info ONLY — must not be rendered as on-image text.
        if cfg.product_context:
            parts.append(
                "\nPRODUCT CONTEXT (background information for your understanding "
                "ONLY — do NOT transcribe, paraphrase, or render any of this text "
                "into the image):\n"
                f"{cfg.product_context}"
            )

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

        # The headline — must be transcribed exactly, not paraphrased.
        # Emphasise that this is THE single piece of copy, not one of many.
        parts.append(
            f'\nHEADLINE TEXT (this is the ONE AND ONLY headline for this ad — '
            f'transcribe letter-perfect, do NOT paraphrase or auto-correct, '
            f'do NOT add any second headline or alternate version):\n"{headline}"'
        )

        # Optional supplementary copy — feature callouts / benefit lines that
        # render in smaller supporting type alongside the headline. Handles
        # both shapes: a single tagline OR a multi-line bulleted list.
        supp = cfg.supplementary_copy
        has_supp = bool(supp)
        if has_supp:
            if len(supp) == 1:
                supp_block = (
                    f'\nSUPPLEMENTARY COPY (render exactly as written, in '
                    f'smaller supporting type around or beneath the headline '
                    f'— do NOT paraphrase, do NOT add other lines):\n'
                    f'"{supp[0]}"'
                )
            else:
                bullet_lines = "\n".join(f'- "{line}"' for line in supp)
                supp_block = (
                    "\nSUPPLEMENTARY COPY (render each of the following lines "
                    "exactly as written, as small supporting callouts/bullets "
                    "around or beneath the headline — do NOT paraphrase, do "
                    "NOT merge them, do NOT add other lines):\n"
                    f"{bullet_lines}"
                )
            parts.append(supp_block)

        # Text content rules — image must show ONLY the single headline above
        # plus any supplementary copy lines provided. Style references are for
        # layout/composition only, never a license to invent additional copy.
        if has_supp:
            allowed_text = (
                "the single HEADLINE TEXT above plus the SUPPLEMENTARY COPY "
                "lines above"
            )
        else:
            allowed_text = "the single HEADLINE TEXT above"

        if has_style_ref:
            parts.append(
                "\nTEXT CONTENT RULES (STRICT):\n"
                f"- The ONLY text that may appear in the image is "
                f"{allowed_text}. Spell every word exactly as written.\n"
                "- Use the style reference for LAYOUT, COMPOSITION, "
                "TYPOGRAPHY and COLOR ONLY. Do NOT copy any text from the "
                "style reference.\n"
                "- If the style reference contains slots for prices, badges, "
                "CTAs, taglines, brand marks, or any other copy beyond what "
                "is allowed above, leave those slots EMPTY or omit them "
                "entirely. Do NOT invent copy to fill them.\n"
                "- Do NOT render multiple headlines, alternate versions, or "
                "variations of the headline. Exactly one headline string "
                "appears in the final image."
            )
        else:
            parts.append(
                "\nTEXT CONTENT RULES (STRICT):\n"
                f"- The ONLY text that may appear in the image is "
                f"{allowed_text}. Spell every word exactly as written.\n"
                "- Do NOT add any other text: no prices, no CTAs "
                "('Shop Now', 'Buy', 'Order Today'), no extra taglines, no "
                "promo badges, no brand name, no website, no hashtags, no "
                "disclaimers, no subtitles beyond what is allowed above.\n"
                "- Do NOT render multiple headlines, alternate versions, or "
                "variations of the headline. Exactly one headline string "
                "appears in the final image."
            )

        # Brief/additional creative direction.
        # Framed as direction ONLY — must not be rendered as on-image text.
        if cfg.brief:
            parts.append(
                "\nCREATIVE BRIEF (creative direction for the visual treatment "
                "ONLY — do NOT transcribe, paraphrase, or render any of this "
                "text into the image):\n"
                f"{cfg.brief}"
            )

        # Aspect ratio
        parts.append(f"\nASPECT RATIO: {cfg.aspect_ratio}")

        # Brand tone
        if cfg.persona_description:
            parts.append(f"\nBRAND TONE: {cfg.persona_description}")

        # Master prompt (global brand guidelines)
        master_prompt = self._get_master_prompt()
        if master_prompt:
            parts.append(f"\nGLOBAL GUIDELINES:\n{master_prompt}")

        parts.append(
            "\nGenerate a high-quality advertisement image. Double-check "
            "that every letter of the HEADLINE TEXT is spelled correctly "
            "and that no extra text has been added beyond what the TEXT "
            "CONTENT RULES allow."
        )

        return "\n".join(parts)

    def get_estimated_count(self) -> int:
        """Return estimated number of images to generate."""
        return (
            len(self.config.headlines)
            * max(1, len(self.config.style_reference_paths))
            * max(1, len(self.config.product_reference_paths))
        )
