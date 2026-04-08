"""
Image critic service for Django.

Sends generated images to Gemini Vision for quality assessment.
Returns structured issues that can trigger auto-revisions.

7 Quality Checks:
1. Fabricated offers/claims — text not in the original prompt
2. Wrong product/model — wrong product shown
3. Price accuracy — price doesn't match prompt
4. Logo fidelity — logo doesn't match provided image
5. Background integration — jarring contrast between photo and ad background
6. Visual hierarchy — poor eye flow or competing elements
7. Too much text — image is text-heavy / cramped
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

logger = logging.getLogger(__name__)


@dataclass
class CritiqueIssue:
    """A single issue found during critique."""

    check_name: str
    severity: str  # "high", "medium", "low"
    description: str
    revision_instruction: str


@dataclass
class CritiqueResult:
    """Full critique result for a single image."""

    image_path: Path
    issues: list[CritiqueIssue] = field(default_factory=list)
    overall_score: float = 0.0
    passed: bool = True
    summary: str = ""
    needs_revision: bool = False
    revision_instructions: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "image_path": str(self.image_path),
            "overall_score": round(self.overall_score, 2),
            "passed": self.passed,
            "needs_revision": self.needs_revision,
            "summary": self.summary,
            "issues": [
                {
                    "check": i.check_name,
                    "severity": i.severity,
                    "description": i.description,
                }
                for i in self.issues
            ],
            "revision_instructions": self.revision_instructions,
        }


CRITIQUE_PROMPT = """\
You are an expert ad creative director reviewing an AI-generated advertisement \
image for Everdries (leakproof underwear brand, target customer: women 65+).

The ad was generated with the following prompt context:
- Product: {product_name}
- Headline intent: {prompt_name}
- Price that SHOULD appear: {expected_price}
- Aspect ratio: {aspect_ratio}

Analyze this image and check for these specific issues. For each check, \
respond with "pass" or "fail" and a brief explanation.

CHECKS:

1. FABRICATED_OFFERS: Does the ad contain ANY text about offers, bundles, \
free gifts, bonuses, or claims that are NOT part of a standard product ad? \
The ONLY price/offer allowed is "{expected_price}". Any other offer text \
(free gifts, bonus items, mystery gifts, bundle details beyond the price) = FAIL.

2. WRONG_PRODUCT: Does the image show the correct product ({product_name})? \
If it shows a clearly different product type (e.g., brief instead of \
shapewear, or a product from a different brand) = FAIL.

3. PRICE_ACCURACY: If a price is shown, does it match "{expected_price}" \
exactly? Wrong numbers, missing price, or different price format = FAIL. \
If no price is shown at all, that is acceptable = PASS.

4. LOGO_FIDELITY: Does the brand name/logo look like a proper rendered logo, \
or does it look like generic typed text? If the "Everdries" text uses a \
plain default font that doesn't look like a designed logo = FAIL.

5. BACKGROUND_INTEGRATION: Is there a jarring, harsh color boundary between \
the model photo area and the ad background/design area? Seamless blending \
or intentional clean edges = PASS. Obvious awkward cutout or clashing \
backgrounds = FAIL.

6. VISUAL_HIERARCHY: Does the ad have clear visual hierarchy — headline \
visible first, then product/model, then price/CTA? Or is everything \
competing for attention with no clear focal point? Good hierarchy = PASS.

7. TEXT_OVERLOAD: Is the ad crammed with too much text? If text covers more \
than ~40% of the image area or feels visually overwhelming/cramped = FAIL. \
Clean, breathing layout with selective text = PASS.

Return your analysis as JSON (no markdown fences):
{{
  "checks": {{
    "fabricated_offers": {{"result": "pass|fail", "detail": "..."}},
    "wrong_product": {{"result": "pass|fail", "detail": "..."}},
    "price_accuracy": {{"result": "pass|fail", "detail": "..."}},
    "logo_fidelity": {{"result": "pass|fail", "detail": "..."}},
    "background_integration": {{"result": "pass|fail", "detail": "..."}},
    "visual_hierarchy": {{"result": "pass|fail", "detail": "..."}},
    "text_overload": {{"result": "pass|fail", "detail": "..."}}
  }},
  "overall_score": <1-10>,
  "summary": "<1-2 sentence overall assessment>"
}}
"""

# Maps check names to revision instruction templates
REVISION_TEMPLATES = {
    "fabricated_offers": (
        "Remove any text about offers, bundles, free gifts, bonuses, "
        "or promotions that are not '{expected_price}'. "
        "Replace that area with clean design space or extend the background. "
        "Do not add any new offers or claims."
    ),
    "wrong_product": (
        "The image shows the wrong product. The ad should feature "
        "{product_name} specifically. Adjust the product shown to match "
        "the provided reference photos of {product_name}."
    ),
    "price_accuracy": (
        "The price shown is incorrect. Change it to exactly "
        "'{expected_price}'. Do not alter anything else."
    ),
    "logo_fidelity": (
        "The Everdries logo/brand name does not match the official logo. "
        "Use the exact logo image provided — do not type or approximate "
        "the logo with a generic font."
    ),
    "background_integration": (
        "There is a jarring color contrast between the model photo "
        "background and the ad design background. Blend the ad background "
        "to seamlessly match the photo's background color, or cleanly "
        "cut out the model onto the design background."
    ),
    "visual_hierarchy": (
        "The visual hierarchy is unclear — elements are competing for "
        "attention. Make the headline the most prominent element, then "
        "the model/product, then the price. Reduce visual clutter."
    ),
    "text_overload": (
        "The ad has too much text and feels cramped. Remove or reduce "
        "secondary text elements. Keep only the headline, price, and "
        "at most 1-2 short feature callouts. Let the design breathe."
    ),
}


class ImageCritic:
    """Evaluates generated images and triggers revisions for issues.

    Uses Gemini Vision to analyze each image against 7 quality checks.
    Issues found generate specific revision instructions.
    """

    def __init__(self, model_name: str = "gemini-2.5-flash") -> None:
        self.model_name = model_name
        self.max_retries = 3
        self.retry_delay_seconds = 2.0
        self._client: Any | None = None

    def _get_client(self) -> Any:
        """Lazily initialize the Gemini client."""
        if self._client is None:
            from google import genai

            # Get API key and model from Django settings or environment
            api_key = os.environ.get("GEMINI_API_KEY", "")
            try:
                from everdries_ad_generator.campaigns.models import APISettings
                api_settings = APISettings.get_settings()
                if not api_key:
                    api_key = api_settings.gemini_api_key
                # Use critic_model and max_retries from settings
                self.model_name = api_settings.critic_model or "gemini-2.5-flash"
                self.max_retries = api_settings.critic_max_retries
            except Exception:
                pass

            if not api_key:
                logger.warning("GEMINI_API_KEY not set — critique disabled")
                return None
            self._client = genai.Client(api_key=api_key)
        return self._client

    def critique(
        self,
        image_path: Path,
        product_name: str = "",
        prompt_name: str = "",
        expected_price: str = "5 for $69.95",
        aspect_ratio: str = "",
    ) -> CritiqueResult:
        """Critique a single image.

        Args:
            image_path: Path to the generated image.
            product_name: Expected product name.
            prompt_name: The image prompt name / headline.
            expected_price: The price that should appear.
            aspect_ratio: The intended aspect ratio.

        Returns:
            CritiqueResult with issues and revision instructions.
        """
        from google.genai import types

        if not image_path.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")

        # Build the prompt
        prompt_text = CRITIQUE_PROMPT.format(
            product_name=product_name,
            prompt_name=prompt_name,
            expected_price=expected_price,
            aspect_ratio=aspect_ratio,
        )

        # Load the image
        img = Image.open(image_path)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")

        # Call Gemini Vision with retry for rate limits
        client = self._get_client()
        if client is None:
            logger.info("Skipping critique for %s — no API key configured", image_path.name)
            return CritiqueResult(
                image_path=image_path,
                overall_score=5.0,
                passed=True,
                summary="Critique skipped — API key not configured",
            )

        for attempt in range(self.max_retries + 1):
            try:
                response = client.models.generate_content(
                    model=self.model_name,
                    contents=[prompt_text, img],
                    config=types.GenerateContentConfig(
                        temperature=0.1,
                    ),
                )
                break
            except Exception as e:
                if "429" in str(e) and attempt < self.max_retries:
                    wait = self.retry_delay_seconds * (2 ** attempt)
                    logger.warning(
                        "Rate limited on critique for %s, "
                        "retrying in %ds (attempt %d/%d)",
                        image_path.name, wait,
                        attempt + 1, self.max_retries,
                    )
                    time.sleep(wait)
                else:
                    logger.error("Critique API error for %s: %s", image_path.name, e)
                    raise

        # Parse the response
        return self._parse_response(
            image_path, response, product_name, expected_price
        )

    def _parse_response(
        self,
        image_path: Path,
        response: Any,
        product_name: str,
        expected_price: str,
    ) -> CritiqueResult:
        """Parse Gemini's JSON response into a CritiqueResult."""
        result = CritiqueResult(image_path=image_path)

        try:
            text = response.text.strip()
            # Strip markdown fences if present
            if text.startswith("```"):
                text = text.split("\n", 1)[1]
                text = text.rsplit("```", 1)[0]
            data = json.loads(text)
        except (json.JSONDecodeError, AttributeError) as e:
            logger.warning(
                "Could not parse critique response for %s: %s",
                image_path, e,
            )
            result.summary = "Critique parse error — skipping"
            result.overall_score = 5.0
            return result

        result.overall_score = data.get("overall_score", 5.0)
        result.summary = data.get("summary", "")

        checks = data.get("checks", {})
        revision_parts: list[str] = []

        for check_name, check_data in checks.items():
            if check_data.get("result", "pass").lower() == "fail":
                detail = check_data.get("detail", "")
                severity = (
                    "high" if check_name in (
                        "fabricated_offers", "wrong_product", "price_accuracy"
                    ) else "medium"
                )
                result.issues.append(
                    CritiqueIssue(
                        check_name=check_name,
                        severity=severity,
                        description=detail,
                        revision_instruction=REVISION_TEMPLATES.get(
                            check_name, ""
                        ).format(
                            product_name=product_name,
                            expected_price=expected_price,
                        ),
                    )
                )
                # Build combined revision instructions
                template = REVISION_TEMPLATES.get(check_name, "")
                if template:
                    revision_parts.append(
                        template.format(
                            product_name=product_name,
                            expected_price=expected_price,
                        )
                    )

        if result.issues:
            result.needs_revision = True
            result.passed = False
            result.revision_instructions = " ".join(revision_parts)

        return result
