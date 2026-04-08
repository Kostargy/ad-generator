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
# Gemini 3 image models suffer severe latency degradation (5–30 min stalls)
# when temperature is below 1.0. Keep this at 1.0.
TEMPERATURE = 1.0
MAX_RETRIES = 3
RETRY_DELAY = 5

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
            temperature=TEMPERATURE,
            image_config=types.ImageConfig(aspect_ratio=prompt.aspect_ratio),
        )

        @retry(
            stop=stop_after_attempt(MAX_RETRIES),
            wait=wait_exponential(multiplier=RETRY_DELAY, min=RETRY_DELAY, max=60),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        )
        def _call() -> Any:
            return client.models.generate_content(
                model=self.model_name,
                contents=contents,
                config=config,
            )

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

        if response.candidates:
            for part in response.candidates[0].content.parts:
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

        raise RuntimeError(
            f"No image data in API response for prompt {index} "
            f"({prompt.image_prompt_name}). "
            f"Response text: {getattr(response, 'text', 'N/A')}"
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
            temperature=TEMPERATURE,
            image_config=types.ImageConfig(**image_config_kwargs) if image_config_kwargs else None,
        )

        @retry(
            stop=stop_after_attempt(MAX_RETRIES),
            wait=wait_exponential(multiplier=RETRY_DELAY, min=RETRY_DELAY, max=60),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        )
        def _call() -> Any:
            return client.models.generate_content(
                model=self.model_name,
                contents=contents,
                config=gen_config,
            )

        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, _call)
        except Exception as exc:
            if _is_unavailable_error(exc):
                raise ProviderUnavailableError(str(exc)) from exc
            raise

    def parse_revision_response(self, response: Any, output_path: Path) -> Path:
        if response.candidates:
            for part in response.candidates[0].content.parts:
                if part.inline_data is not None:
                    result_img = Image.open(BytesIO(part.inline_data.data))
                    result_img.save(str(output_path), "PNG")
                    logger.info("Saved revision to %s", output_path)
                    return output_path

        raise RuntimeError(
            f"No image data in revision response. "
            f"Response text: {getattr(response, 'text', 'N/A')}"
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
