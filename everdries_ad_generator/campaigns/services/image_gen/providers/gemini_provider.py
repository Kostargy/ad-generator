"""Gemini image generation provider with hardcoded config."""

from __future__ import annotations

import asyncio
import logging
import os
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .base import ImageProvider, ProviderUnavailableError

if TYPE_CHECKING:
    from everdries_ad_generator.campaigns.services.image_gen_adapter import GenerationPrompt

logger = logging.getLogger(__name__)

# Hardcoded config values
DEFAULT_IMAGE_MODEL = "gemini-3-pro-image-preview"
# Default temperature when APISettings lookup fails. Gemini 3 image models
# have historically shown severe latency degradation below 1.0 — keep the
# fallback at 1.0 so failures don't silently regress latency.
DEFAULT_TEMPERATURE = 1.0
MAX_RETRIES = 3
RETRY_DELAY = 5


def _get_temperature() -> float:
    """Load sampling temperature from APISettings, falling back to the default."""
    try:
        from everdries_ad_generator.campaigns.models import APISettings
        return float(APISettings.get_settings().image_temperature)
    except Exception:
        return DEFAULT_TEMPERATURE

# Error messages that indicate the model is unavailable
_UNAVAILABLE_SIGNALS = (
    "503",
    "overloaded",
    "model is overloaded",
    "model_overloaded",
    "quota_exceeded",
    "resource_exhausted",
    "service_unavailable",
)


def _is_unavailable_error(exc: Exception) -> bool:
    """Return True if exc looks like a transient provider outage."""
    msg = str(exc).lower()
    return any(signal in msg for signal in _UNAVAILABLE_SIGNALS)


# finish_reasons that are transient model hiccups — safe to retry.
_RETRYABLE_FINISH_REASONS = {
    "IMAGE_OTHER",
    "OTHER",
    "FINISH_REASON_UNSPECIFIED",
    "MAX_TOKENS",
}


class _EmptyImageResponseError(Exception):
    """Raised when Gemini returns 200 OK but no image data — used for retry."""


def _response_has_image(response: Any) -> bool:
    candidate = response.candidates[0] if getattr(response, "candidates", None) else None
    content = getattr(candidate, "content", None) if candidate else None
    parts = getattr(content, "parts", None) if content else None
    if not parts:
        return False
    return any(getattr(p, "inline_data", None) is not None for p in parts)


def _finish_reason_name(response: Any) -> str:
    candidate = response.candidates[0] if getattr(response, "candidates", None) else None
    fr = getattr(candidate, "finish_reason", None) if candidate else None
    if fr is None:
        return ""
    return getattr(fr, "name", str(fr))


class GeminiProvider(ImageProvider):
    """Gemini native image generation."""

    name = "gemini"

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or DEFAULT_IMAGE_MODEL
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            from google import genai

            api_key = os.environ.get("GEMINI_API_KEY", "")
            if not api_key:
                raise RuntimeError("GEMINI_API_KEY not set in environment.")
            self._client = genai.Client(api_key=api_key)
        return self._client

    def build_contents(self, prompt: "GenerationPrompt") -> list[Any]:
        """Build the contents list for the API call."""
        contents: list[Any] = [prompt.prompt_text]

        # Product reference images
        for ref_path in prompt.reference_images:
            try:
                img = Image.open(ref_path)
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                label = _reference_label(ref_path)
                contents.append(label)
                contents.append(img)
            except Exception as e:
                logger.warning("Could not load reference image %s: %s", ref_path, e)

        # Logo
        for logo_path in prompt.logo_images:
            try:
                img = Image.open(logo_path)
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                contents.append(
                    "BRAND LOGO — reproduce this exactly, do NOT re-type or approximate:"
                )
                contents.append(img)
            except Exception as e:
                logger.warning("Could not load logo image %s: %s", logo_path, e)

        # Style reference
        if prompt.style_reference:
            try:
                img = Image.open(prompt.style_reference)
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                contents.append(
                    "LAYOUT REFERENCE — follow this ad's layout structure, "
                    "text placement, and typography style. Do NOT use any "
                    "person, product, colors, or text content from this image:"
                )
                contents.append(img)
            except Exception as e:
                logger.warning("Could not load style reference %s: %s", prompt.style_reference, e)

        return contents

    async def call_api(self, prompt: "GenerationPrompt", contents: Any) -> Any:
        """Call the Gemini API with retry logic."""
        from google.genai import types

        client = self._get_client()

        config = types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            temperature=_get_temperature(),
            image_config=types.ImageConfig(aspect_ratio=prompt.aspect_ratio),
        )

        @retry(
            stop=stop_after_attempt(MAX_RETRIES),
            wait=wait_exponential(multiplier=RETRY_DELAY, min=RETRY_DELAY, max=60),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        )
        def _call() -> Any:
            response = client.models.generate_content(
                model=self.model_name,
                contents=contents,
                config=config,
            )
            if not _response_has_image(response):
                fr = _finish_reason_name(response)
                if fr in _RETRYABLE_FINISH_REASONS:
                    logger.warning(
                        "Empty image response (finish_reason=%s), retrying", fr
                    )
                    raise _EmptyImageResponseError(f"finish_reason={fr}")
                # Non-retryable (e.g. SAFETY) — return as-is so parse_response
                # surfaces a clean error to the caller.
            return response

        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, _call)
        except Exception as exc:
            if _is_unavailable_error(exc):
                raise ProviderUnavailableError(str(exc)) from exc
            raise

    def parse_response(
        self,
        response: Any,
        index: int,
        prompt: "GenerationPrompt",
        output_dir: Path,
        run_prefix: str,
    ) -> Path:
        """Extract the image from the API response and save to disk."""
        image_path = output_dir / f"{run_prefix}gen_{index:04d}.png"

        candidate = response.candidates[0] if response.candidates else None
        content = getattr(candidate, "content", None) if candidate else None
        if content and getattr(content, "parts", None):
            for part in content.parts:
                if part.inline_data is not None:
                    image = Image.open(BytesIO(part.inline_data.data))
                    image.save(str(image_path), "PNG")
                    logger.info(
                        "Saved image %s (%s, %s)",
                        image_path.name,
                        prompt.product_name,
                        prompt.image_prompt_name,
                    )
                    return image_path

        finish_reason = getattr(candidate, "finish_reason", None) if candidate else None
        prompt_feedback = getattr(response, "prompt_feedback", None)
        raise RuntimeError(
            f"No image data in API response for prompt {index} "
            f"({prompt.image_prompt_name}). "
            f"finish_reason={finish_reason!r}, "
            f"prompt_feedback={prompt_feedback!r}, "
            f"text={getattr(response, 'text', 'N/A')!r}"
        )

    async def call_revision_api(
        self,
        contents: list[Any],
        aspect_ratio: str | None,
    ) -> Any:
        from google.genai import types

        client = self._get_client()

        image_config_kwargs: dict[str, Any] = {}
        if aspect_ratio:
            image_config_kwargs["aspect_ratio"] = aspect_ratio

        gen_config = types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            temperature=_get_temperature(),
            image_config=types.ImageConfig(**image_config_kwargs) if image_config_kwargs else None,
        )

        @retry(
            stop=stop_after_attempt(MAX_RETRIES),
            wait=wait_exponential(multiplier=RETRY_DELAY, min=RETRY_DELAY, max=60),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        )
        def _call() -> Any:
            response = client.models.generate_content(
                model=self.model_name,
                contents=contents,
                config=gen_config,
            )
            if not _response_has_image(response):
                fr = _finish_reason_name(response)
                if fr in _RETRYABLE_FINISH_REASONS:
                    logger.warning(
                        "Empty revision response (finish_reason=%s), retrying", fr
                    )
                    raise _EmptyImageResponseError(f"finish_reason={fr}")
            return response

        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, _call)
        except Exception as exc:
            if _is_unavailable_error(exc):
                raise ProviderUnavailableError(str(exc)) from exc
            raise

    def parse_revision_response(self, response: Any, output_path: Path) -> Path:
        candidate = response.candidates[0] if response.candidates else None
        content = getattr(candidate, "content", None) if candidate else None
        if content and getattr(content, "parts", None):
            for part in content.parts:
                if part.inline_data is not None:
                    result_img = Image.open(BytesIO(part.inline_data.data))
                    result_img.save(str(output_path), "PNG")
                    logger.info("Saved revision to %s", output_path)
                    return output_path

        finish_reason = getattr(candidate, "finish_reason", None) if candidate else None
        prompt_feedback = getattr(response, "prompt_feedback", None)
        raise RuntimeError(
            f"No image data in revision response. "
            f"finish_reason={finish_reason!r}, "
            f"prompt_feedback={prompt_feedback!r}, "
            f"text={getattr(response, 'text', 'N/A')!r}"
        )

    def log_payload_size(self, contents: Any, index: int, ref_count: int) -> None:
        total_bytes = 0
        image_count = 0
        for item in contents:
            if isinstance(item, str):
                total_bytes += len(item.encode("utf-8"))
            elif isinstance(item, Image.Image):
                buf = BytesIO()
                try:
                    img = item if item.mode == "RGB" else item.convert("RGB")
                    img.save(buf, "JPEG", quality=85)
                    total_bytes += buf.tell()
                except Exception:
                    w, h = item.size
                    total_bytes += w * h // 4
                image_count += 1

        total_mb = total_bytes / (1024 * 1024)
        if total_mb > 10:
            logger.warning(
                "Large payload for image %d: ~%.1f MB (%d images)",
                index + 1,
                total_mb,
                image_count,
            )
        else:
            logger.info(
                "Payload for image %d: ~%.1f MB (%d images)",
                index + 1,
                total_mb,
                image_count,
            )


def _reference_label(ref_path: Path) -> str:
    """Return a descriptive label for a reference image based on filename."""
    name = ref_path.name.lower()
    if name.startswith("5color"):
        return "FLATLAY PHOTO (all 5 colors in one image) — use as-is, do NOT redraw:"
    if name.startswith("single_flatlay"):
        return "FLATLAY PHOTO (single color) — use as-is, do NOT redraw:"
    if name.startswith("single_ghost") or name.startswith("5color_ghost"):
        return "GHOST MANNEQUIN PHOTO — use as-is, do NOT redraw:"
    return "MODEL PHOTO — use this exact photo as-is in the ad, do NOT redraw:"
