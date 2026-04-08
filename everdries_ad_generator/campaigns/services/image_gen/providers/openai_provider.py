"""OpenAI image generation provider with hardcoded config."""

from __future__ import annotations

import asyncio
import base64
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
MODEL_NAME = "gpt-image-1"
QUALITY = "high"
MAX_RETRIES = 3
RETRY_DELAY = 5

# Aspect ratio to OpenAI size mapping
_SIZE_MAP: dict[str, str] = {
    "1:1": "1024x1024",
    "9:16": "1024x1536",
    "4:5": "1024x1536",
    "16:9": "1536x1024",
    "5:4": "1536x1024",
}

# Max reference images for gpt-image-1 images.edit()
_MAX_REFERENCE_IMAGES = 10

_UNAVAILABLE_SIGNALS = (
    "503",
    "overloaded",
    "quota_exceeded",
    "rate_limit",
    "service_unavailable",
    "server_error",
    "capacity",
)


def _is_unavailable_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(signal in msg for signal in _UNAVAILABLE_SIGNALS)


def _image_to_png_bytes(img: Image.Image) -> bytes:
    """Convert a PIL Image to PNG bytes (OpenAI requires PNG)."""
    buf = BytesIO()
    if img.mode == "RGBA":
        img.save(buf, "PNG")
    else:
        img.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


class OpenAIProvider(ImageProvider):
    """OpenAI gpt-image-1 via images.edit()."""

    name = "openai"

    def __init__(self) -> None:
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            from openai import OpenAI

            api_key = os.environ.get("OPENAI_API_KEY", "")
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY not set in environment.")
            self._client = OpenAI(api_key=api_key)
        return self._client

    def build_contents(self, prompt: "GenerationPrompt") -> dict[str, Any]:
        """Build OpenAI-compatible payload."""
        images: list[bytes] = []

        # Reference images
        for ref_path in prompt.reference_images:
            try:
                img = Image.open(ref_path)
                images.append(_image_to_png_bytes(img))
            except Exception as e:
                logger.warning("Could not load reference image %s: %s", ref_path, e)

        # Logo
        for logo_path in prompt.logo_images:
            try:
                img = Image.open(logo_path)
                images.append(_image_to_png_bytes(img))
            except Exception as e:
                logger.warning("Could not load logo image %s: %s", logo_path, e)

        # Style reference
        if prompt.style_reference:
            try:
                img = Image.open(prompt.style_reference)
                images.append(_image_to_png_bytes(img))
            except Exception as e:
                logger.warning("Could not load style reference %s: %s", prompt.style_reference, e)

        # Trim to max allowed
        if len(images) > _MAX_REFERENCE_IMAGES:
            logger.warning(
                "Trimming %d reference images to %d for OpenAI",
                len(images),
                _MAX_REFERENCE_IMAGES,
            )
            images = images[:_MAX_REFERENCE_IMAGES]

        size = _SIZE_MAP.get(prompt.aspect_ratio, "1024x1024")

        return {
            "prompt": prompt.prompt_text,
            "images": images,
            "size": size,
        }

    async def call_api(self, prompt: "GenerationPrompt", contents: Any) -> Any:
        client = self._get_client()

        text_prompt = contents["prompt"]
        images = contents["images"]
        size = contents["size"]

        @retry(
            stop=stop_after_attempt(MAX_RETRIES),
            wait=wait_exponential(multiplier=RETRY_DELAY, min=RETRY_DELAY, max=60),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        )
        def _call() -> Any:
            if images:
                image_files = []
                for i, img_bytes in enumerate(images):
                    buf = BytesIO(img_bytes)
                    buf.name = f"image_{i}.png"
                    image_files.append(buf)
                return client.images.edit(
                    model=MODEL_NAME,
                    image=image_files,
                    prompt=text_prompt,
                    size=size,
                    quality=QUALITY,
                )
            else:
                return client.images.generate(
                    model=MODEL_NAME,
                    prompt=text_prompt,
                    size=size,
                    quality=QUALITY,
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
        image_path = output_dir / f"{run_prefix}gen_{index:04d}.png"

        if response.data and len(response.data) > 0:
            item = response.data[0]
            if item.b64_json:
                img_bytes = base64.b64decode(item.b64_json)
                image = Image.open(BytesIO(img_bytes))
                image.save(str(image_path), "PNG")
                logger.info(
                    "Saved image %s via OpenAI (%s, %s)",
                    image_path.name,
                    prompt.product_name,
                    prompt.image_prompt_name,
                )
                return image_path

        raise RuntimeError(
            f"No image data in OpenAI response for prompt {index} "
            f"({prompt.image_prompt_name})."
        )

    async def call_revision_api(
        self,
        contents: list[Any],
        aspect_ratio: str | None,
    ) -> Any:
        client = self._get_client()

        text_parts: list[str] = []
        images: list[bytes] = []

        for item in contents:
            if isinstance(item, str):
                text_parts.append(item)
            elif isinstance(item, Image.Image):
                images.append(_image_to_png_bytes(item))

        prompt_text = "\n".join(text_parts)
        size = _SIZE_MAP.get(aspect_ratio or "", "1024x1024")

        if len(images) > _MAX_REFERENCE_IMAGES:
            images = images[:_MAX_REFERENCE_IMAGES]

        @retry(
            stop=stop_after_attempt(MAX_RETRIES),
            wait=wait_exponential(multiplier=RETRY_DELAY, min=RETRY_DELAY, max=60),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        )
        def _call() -> Any:
            if images:
                image_files = []
                for i, img_bytes in enumerate(images):
                    buf = BytesIO(img_bytes)
                    buf.name = f"image_{i}.png"
                    image_files.append(buf)
                return client.images.edit(
                    model=MODEL_NAME,
                    image=image_files,
                    prompt=prompt_text,
                    size=size,
                    quality=QUALITY,
                )
            else:
                return client.images.generate(
                    model=MODEL_NAME,
                    prompt=prompt_text,
                    size=size,
                    quality=QUALITY,
                )

        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, _call)
        except Exception as exc:
            if _is_unavailable_error(exc):
                raise ProviderUnavailableError(str(exc)) from exc
            raise

    def parse_revision_response(self, response: Any, output_path: Path) -> Path:
        if response.data and len(response.data) > 0:
            item = response.data[0]
            if item.b64_json:
                img_bytes = base64.b64decode(item.b64_json)
                image = Image.open(BytesIO(img_bytes))
                image.save(str(output_path), "PNG")
                logger.info("Saved revision to %s via OpenAI", output_path)
                return output_path

        raise RuntimeError("No image data in OpenAI revision response.")

    def log_payload_size(self, contents: Any, index: int, ref_count: int) -> None:
        total_bytes = 0
        image_count = 0

        if isinstance(contents, dict):
            total_bytes += len(contents.get("prompt", "").encode("utf-8"))
            for img_bytes in contents.get("images", []):
                total_bytes += len(img_bytes)
                image_count += 1

        total_mb = total_bytes / (1024 * 1024)
        if total_mb > 10:
            logger.warning(
                "Large OpenAI payload for image %d: ~%.1f MB (%d images)",
                index + 1,
                total_mb,
                image_count,
            )
        else:
            logger.info(
                "OpenAI payload for image %d: ~%.1f MB (%d images)",
                index + 1,
                total_mb,
                image_count,
            )
