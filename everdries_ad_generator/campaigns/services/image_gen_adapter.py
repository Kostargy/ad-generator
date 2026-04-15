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


GLOBAL_GENERATION_RULES = """
GLOBAL GENERATION RULES — these apply to every image you generate. Follow them strictly; they override any conflicting instruction elsewhere in the prompt.

REFERENCE IMAGES (CRITICAL):
I have provided labeled product reference photos. These are REAL product photos that MUST appear in the final ad AS-IS. Do NOT regenerate, redraw, reinterpret, or create new versions of the products shown. Place the actual provided photo into the ad design. Add text, graphics, and design elements AROUND and ON TOP of the real photo. Do NOT invent new colors or modify the colors of the products. The exact colors in the reference photos are the ONLY colors that should appear on the product.

When multiple reference images are attached, treat them as follows:
- MODEL PHOTO (a person wearing or holding the product): use this exact photo as-is as the hero of the ad. Do NOT redraw the model, the pose, the clothing, or the background of this photo — composite design elements around and on top of it.
- FLATLAY / GHOST-MANNEQUIN PHOTO (product shown alone, possibly in multiple colors): use as an accurate reference for product detail, color, and texture. Do NOT redraw. You may include a flatlay as a secondary design element (e.g. a small color strip or sidebar), but never as the hero unless no model photo is provided.

STYLE REFERENCE (when one is attached):
Treat the style reference as a TEMPLATE. Replicate its layout structure, text placement, typography style, color palette, composition, AND any supporting text elements it contains — CTAs, prices, badges, taglines, disclaimers, supporting copy — transcribed EXACTLY as they appear in the style reference, in the same position and style. Two things are replaced, and only these two:
1. The style reference's main headline is replaced with the HEADLINE specified elsewhere in this prompt.
2. The style reference's sub-copy / descriptive body text is replaced with the SUPPLEMENTARY COPY specified elsewhere in this prompt (if any).
The product and model must come from the separate reference photos, NOT from the style reference — do not copy the person or product shown in the style reference. Everything else about the style reference is preserved verbatim. Do NOT invent new CTAs, prices, or badges that don't exist in the style reference, and do NOT omit ones that do.

TEXT CONTENT RULES (STRICT):
- The HEADLINE (and SUPPLEMENTARY COPY, if provided) specified elsewhere in this prompt must be rendered exactly as written. Spell every word exactly — do not paraphrase, do not auto-correct, do not add punctuation that isn't there. Render exactly ONE headline, never multiple or alternate versions.
- If a STYLE REFERENCE is attached: additional text elements (CTAs, prices, badges, taglines, disclaimers) come from the style reference and must be transcribed exactly as they appear there. Do NOT invent copy beyond what the style reference shows, and do NOT drop copy the style reference shows.
- If NO style reference is attached: the ONLY text in the image is the HEADLINE (and SUPPLEMENTARY COPY, if provided). Do NOT add any other text — no prices, no CTAs ("Shop Now", "Buy", "Order Today"), no extra taglines, no promo badges, no brand name, no website, no hashtags, no disclaimers, no subtitles, no legal text.

BRAND LOGO (when one is attached):
Reproduce the provided logo image exactly as supplied. Do NOT re-type, re-draw, or approximate the logo's wordmark — place the actual logo pixels into the ad. If no logo image is attached, do NOT include any logo or brand mark in the image.

BRAND:
Do NOT reference Trustpilot, reviews, or star ratings. Do NOT use "premium", "luxury", or similar upmarket language. Do NOT include any logo, brand mark, or wordmark unless a logo image has been explicitly attached as a reference.

BACKGROUND INTEGRATION:
Blend the ad background seamlessly with the model photo's existing background. Avoid jarring color contrasts at the edges of the composited model photo. The finished ad should look like one cohesive image, not a cutout pasted onto an unrelated backdrop.

QUALITY CHECK BEFORE RETURNING:
1. Is every letter of the headline spelled exactly as written in this prompt?
2. Have you added any text beyond what the TEXT CONTENT RULES allow? If yes, remove it.
3. Have you redrawn, recolored, or reinterpreted any product? If yes, replace it with the original reference photo.
4. Does the style reference's person or product appear anywhere in the final image? If yes, replace it with the reference photos. (Style reference text elements like CTAs/prices/badges SHOULD be preserved — only the person and product are replaced.)
5. Is the model photo's background blended with the ad background, or is there a visible cutout seam? Fix any seams.

Generate a high-quality advertisement image.
""".strip()


@dataclass
class GeneratorConfig:
    """Configuration extracted from Django Generator."""

    product_name: str
    product_context: str
    headlines: list[str]
    supplementary_copy: list[str]
    brief: str
    model_reference_paths: list[Path]
    flat_lay_reference_paths: list[Path]
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
            model_reference_paths=[
                self._materialize_asset(asset)
                for asset in self.generator.model_references.all()
                if asset.image
            ],
            flat_lay_reference_paths=[
                self._materialize_asset(asset)
                for asset in self.generator.flat_lay_references.all()
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
        # when style or model references are absent.
        style_refs = cfg.style_reference_paths or [None]
        model_refs = cfg.model_reference_paths or [None]
        # Flat-lays don't add a fan-out dimension — every prompt cell gets the
        # full set, so the model can use them as detail/color references.
        flat_lay_refs = cfg.flat_lay_reference_paths

        for headline in cfg.headlines:
            for style_idx, style_path in enumerate(style_refs):
                for model_idx, model_path in enumerate(model_refs):
                    prompt_text = self._format_prompt(headline=headline)

                    # Build a name that distinguishes each cell in the fan-out
                    # so Ad rows are identifiable in the UI.
                    name_parts = [headline[:40]]
                    if len(style_refs) > 1 and style_path:
                        name_parts.append(f"v{style_idx + 1}")
                    if len(model_refs) > 1 and model_path:
                        name_parts.append(f"m{model_idx + 1}")
                    name = " ".join(name_parts)

                    # Reference images = the single model image for this cell
                    # (if any) followed by every flat-lay shot the user uploaded.
                    cell_refs: list[Path] = []
                    if model_path:
                        cell_refs.append(model_path)
                    cell_refs.extend(flat_lay_refs)

                    prompts.append(
                        GenerationPrompt(
                            prompt_text=prompt_text,
                            reference_images=cell_refs,
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

    def _format_prompt(self, headline: str) -> str:
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

        # Master prompt (optional, admin-configurable)
        master_prompt = self._get_master_prompt()
        if master_prompt:
            parts.append(f"\nGLOBAL GUIDELINES:\n{master_prompt}")

        parts.append(f"\n{GLOBAL_GENERATION_RULES}")

        return "\n".join(parts)

    def get_estimated_count(self) -> int:
        """Return estimated number of images to generate."""
        return (
            len(self.config.headlines)
            * max(1, len(self.config.style_reference_paths))
            * max(1, len(self.config.model_reference_paths))
        )
