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

BACKGROUND INTEGRATION (HIGHEST PRIORITY — overrides the STYLE REFERENCE):
The ENTIRE ad canvas, from edge to edge, uses the model photo's backdrop as its background. Extend / outpaint the model photo's existing backdrop so it fills the full canvas — the model photo is not a framed element sitting inside the ad, it IS the ad's background. All text, graphics, and design elements are composited DIRECTLY ON TOP of this extended backdrop.

Hard prohibitions (these override anything implied by the STYLE REFERENCE):
- NO white panel, off-white panel, cream panel, colored sidebar, or split-screen layout. The canvas has ONE continuous background, not two.
- NO vertical or horizontal divider, seam, border, frame, gradient edge, or hard color transition between the area containing the model and the area containing the text.
- NO rectangular block, card, pill, or shape of a different color placed behind the headline or supplementary copy. The headline sits directly on the extended backdrop — if contrast is needed for legibility, use a soft drop shadow, outline, or subtle darkening of the backdrop immediately behind the letters, NEVER a separate colored panel.
- NO secondary background color anywhere in the composition. Sample the dominant color of the model photo's backdrop and use that exact color for every pixel of negative space.

Interaction with the STYLE REFERENCE (below): the style reference determines WHERE text and graphical elements are placed (layout positions, typography, proportions, spacing) and WHICH supporting elements exist (CTAs, badges, prices — see STYLE REFERENCE rules). It does NOT determine background colors, panel colors, or split-screen structure. If the style reference shows a white right-hand panel with text on it, you place the text in that same position but on the extended model-photo backdrop — NOT on a white panel.

STYLE REFERENCE (when one is attached):
Treat the style reference as a TEMPLATE. Replicate its layout structure, text placement, typography style, color palette, composition, AND any supporting text elements it contains — CTAs, prices, badges, taglines, disclaimers, supporting copy — transcribed EXACTLY as they appear in the style reference, in the same position and style. Replacements are made ONLY where the style reference has an equivalent slot:
1. HEADLINE slot. If the style reference has a main headline slot, DELETE the style reference's existing headline text and write the HEADLINE specified elsewhere in this prompt in its place. The style reference's original headline text is PLACEHOLDER ONLY — do NOT transcribe it, do NOT paraphrase it, do NOT preserve any of it in the final ad. If the style reference has no headline slot, do not add one.
2. SUB-COPY slot. If the style reference has a sub-copy / descriptive body slot AND SUPPLEMENTARY COPY is specified elsewhere in this prompt, DELETE the style reference's existing sub-copy text and write the SUPPLEMENTARY COPY in its place. The style reference's original sub-copy text is PLACEHOLDER ONLY — do NOT transcribe it, do NOT paraphrase it, do NOT preserve any of it in the final ad. When writing the SUPPLEMENTARY COPY, MATCH THE SHAPE of the style reference's sub-copy slot: count the number of lines the style reference's sub-copy holds and render only that many lines from the SUPPLEMENTARY COPY, in order, starting from the first line. Example: if the style reference's sub-copy is ONE line and SUPPLEMENTARY COPY contains four lines, render only the FIRST line and discard the other three. If the style reference has NO sub-copy slot, do NOT add one — omit the SUPPLEMENTARY COPY entirely, even if it is specified elsewhere in this prompt. If the style reference has a sub-copy slot but no SUPPLEMENTARY COPY is specified, leave that slot empty.
The product and model must come from the separate reference photos, NOT from the style reference — do not copy the person or product shown in the style reference. Everything else about the style reference is preserved verbatim. Do NOT invent new CTAs, prices, or badges that don't exist in the style reference, and do NOT omit ones that do. Do NOT add text slots (headlines, sub-copy, CTAs, badges) that the style reference does not already have.

TEXT CONTENT RULES (STRICT):
- The HEADLINE (and SUPPLEMENTARY COPY, if provided) specified elsewhere in this prompt must be rendered exactly as written wherever it is rendered. Spell every word exactly — do not paraphrase, do not auto-correct, do not add punctuation that isn't there. Render exactly ONE headline, never multiple or alternate versions.
- EACH SUPPORTING ELEMENT APPEARS EXACTLY ONCE. No element — no CTA, no "Shop Now" button, no "Buy Now" button, no price, no badge, no tagline, no disclaimer, no brand mark — may be drawn, rendered, or composited more than once in the final image. If the style reference has one "BUY NOW" button, the final ad has exactly one "BUY NOW" button in the same position. If it has one price, the final ad has exactly one price. Never duplicate, never mirror, never add a second copy in another position.
- If a STYLE REFERENCE is attached: whether HEADLINE and SUPPLEMENTARY COPY appear in the image is determined by whether the style reference has corresponding slots (see STYLE REFERENCE rules above). Additional text elements (CTAs, prices, badges, taglines, disclaimers) come from the style reference and must be transcribed exactly as they appear there — once each, in the same position the style reference shows them. Do NOT invent copy beyond what the style reference shows, and do NOT drop copy the style reference shows.
- If NO style reference is attached: the ONLY text in the image is the HEADLINE (and SUPPLEMENTARY COPY, if provided). Do NOT add any other text — no prices, no CTAs ("Shop Now", "Buy", "Order Today"), no extra taglines, no promo badges, no brand name, no website, no hashtags, no disclaimers, no subtitles, no legal text.

BRAND LOGO (when one is attached):
Reproduce the provided logo image exactly as supplied. Do NOT re-type, re-draw, or approximate the logo's wordmark — place the actual logo pixels into the ad. If no logo image is attached, do NOT include any logo or brand mark in the image.

BRAND:
Do NOT reference Trustpilot, reviews, or star ratings. Do NOT use "premium", "luxury", or similar upmarket language. Do NOT include any logo, brand mark, or wordmark unless a logo image has been explicitly attached as a reference.

QUALITY CHECK BEFORE RETURNING:
1. Is every letter of the headline spelled exactly as written in this prompt?
2. Have you added any text beyond what the TEXT CONTENT RULES allow? If yes, remove it.
3. Have you redrawn, recolored, or reinterpreted any product? If yes, replace it with the original reference photo.
4. Does the style reference's person or product appear anywhere in the final image? If yes, replace it with the reference photos. (Style reference text elements like CTAs/prices/badges SHOULD be preserved — only the person and product are replaced.)
5. Does the model photo's backdrop extend edge-to-edge across the ENTIRE canvas, with the text composited directly on top of it? If there is ANY white/off-white/cream panel, colored sidebar, split-screen, vertical or horizontal seam, or rectangular shape of a different color behind the text — remove it and extend the model photo's backdrop to cover that area. The canvas must have ONE continuous background.
6. Does the image contain SUPPLEMENTARY COPY that the style reference did not have a slot for? If so, remove it — supplementary copy is only rendered when the style reference already has a sub-copy slot.
7. Does any supporting element appear more than once? Count the CTAs/buttons (e.g. "BUY NOW", "SHOP NOW"), prices, badges, taglines, and disclaimers. If any of them appear twice or more, delete the duplicates — each supporting element must appear exactly once, in the position shown in the style reference.
8. Did you transcribe any of the style reference's ORIGINAL headline or sub-copy text into the final ad? Compare the headline and sub-copy in your output to the headline and sub-copy in the style reference image. If any of the style reference's original headline or sub-copy words appear in the output, DELETE them and replace with the supplied HEADLINE and SUPPLEMENTARY COPY. The style reference's original headline and sub-copy text is PLACEHOLDER ONLY — it MUST NOT appear in the output. (CTAs, buttons, prices, and badges from the style reference are different — those SHOULD be transcribed exactly. Only the headline and sub-copy slots get replaced.)

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
                    prompt_text = self._format_prompt(
                        headline=headline,
                        has_style_ref=style_path is not None,
                    )

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

        # The headline — must be transcribed exactly, not paraphrased.
        # Emphasise that this is THE single piece of copy, not one of many.
        parts.append(
            f'\nHEADLINE TEXT (this is the ONE AND ONLY headline for this ad — '
            f'transcribe letter-perfect, do NOT paraphrase or auto-correct, '
            f'do NOT add any second headline or alternate version):\n"{headline}"'
        )

        # Optional supplementary copy — feature callouts / benefit lines.
        # Placement rules differ depending on whether a STYLE REFERENCE is
        # attached: with a style ref, the copy is slot-gated (only rendered
        # if the style ref already has a sub-copy area); without one, it
        # renders freely around or beneath the headline.
        supp = cfg.supplementary_copy
        has_supp = bool(supp)
        if has_supp:
            if len(supp) == 1:
                supp_payload = f'"{supp[0]}"'
            else:
                supp_payload = "\n".join(f'- "{line}"' for line in supp)

            if has_style_ref:
                supp_block = (
                    "\nSUPPLEMENTARY COPY (REPLACEMENT for the style "
                    "reference's sub-copy slot — read carefully):\n"
                    "STEP 1: If the style reference has NO sub-copy / "
                    "descriptive body slot, OMIT these lines entirely — do "
                    "NOT render them anywhere in the image, do NOT create a "
                    "new sub-copy area, do NOT add them as bullets, "
                    "callouts, badges, or captions, and do NOT place them "
                    "around or beneath the headline. Stop here.\n"
                    "STEP 2: If the style reference HAS a sub-copy slot, "
                    "DELETE the style reference's existing sub-copy text "
                    "from that slot. The style reference's original "
                    "sub-copy text is PLACEHOLDER ONLY — do NOT transcribe "
                    "it, do NOT paraphrase it, do NOT preserve any of it "
                    "in the final ad. None of the original sub-copy words "
                    "from the style reference may appear in the output.\n"
                    "STEP 3: Write the lines below into that now-empty "
                    "sub-copy slot, transcribed exactly as written. MATCH "
                    "THE SHAPE of the slot: count how many lines the style "
                    "reference's sub-copy slot holds and render ONLY that "
                    "many lines from the list below, starting from the "
                    "first line and taking them in order. If the style "
                    "reference's sub-copy is a single line, render ONLY "
                    "the first line below and discard the rest. If it is "
                    "two lines, render only the first two, and so on. "
                    "Never render more lines than the style reference's "
                    "sub-copy slot holds.\n"
                    f"{supp_payload}"
                )
            else:
                if len(supp) == 1:
                    supp_block = (
                        "\nSUPPLEMENTARY COPY (render exactly as written, in "
                        "smaller supporting type around or beneath the "
                        "headline — do NOT paraphrase, do NOT add other "
                        "lines):\n"
                        f"{supp_payload}"
                    )
                else:
                    supp_block = (
                        "\nSUPPLEMENTARY COPY (render each of the following "
                        "lines exactly as written, as small supporting "
                        "callouts/bullets around or beneath the headline — "
                        "do NOT paraphrase, do NOT merge them, do NOT add "
                        "other lines):\n"
                        f"{supp_payload}"
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
