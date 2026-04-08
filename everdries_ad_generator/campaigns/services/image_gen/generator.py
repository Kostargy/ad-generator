"""
Image generator with hardcoded config.

Batch image generation with Gemini (primary) and OpenAI (fallback).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from PIL import Image

from .providers.base import (
    ImageGenerationAbortedError,
    ImageProvider,
    ProviderUnavailableError,
)
from .providers.gemini_provider import GeminiProvider
from .providers.openai_provider import OpenAIProvider

if TYPE_CHECKING:
    from everdries_ad_generator.campaigns.services.image_gen_adapter import GenerationPrompt

logger = logging.getLogger(__name__)

# Hardcoded config values
RATE_LIMIT_RPM = 60
RATE_LIMIT_RPD = 1500
BATCH_SIZE = 10
RETRY_DELAY = 5


@dataclass
class RevisionContext:
    """Original generation context needed for high-quality revisions."""

    reference_images: list[Path] = field(default_factory=list)
    logo_images: list[Path] = field(default_factory=list)
    style_reference: Path | None = None
    prompt_text: str = ""
    aspect_ratio: str = ""
    product_name: str = ""

    def summary(self) -> str:
        """One-line summary for display."""
        parts = []
        if self.reference_images:
            parts.append(f"{len(self.reference_images)} ref image(s)")
        if self.logo_images:
            parts.append("logo")
        if self.style_reference:
            parts.append("style ref")
        if self.prompt_text:
            parts.append("prompt")
        return ", ".join(parts) if parts else "no context"


@dataclass
class GeneratedImage:
    """Represents a single generated image and its metadata."""

    image_path: Path
    prompt: "GenerationPrompt"
    generation_id: str = ""
    timestamp: float = field(default_factory=time.time)
    raw_response: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "image_path": str(self.image_path),
            "generation_id": self.generation_id,
            "timestamp": self.timestamp,
            "prompt": self.prompt.to_dict(),
        }


class RateLimiter:
    """Simple async token-bucket rate limiter."""

    def __init__(self, rpm: int = RATE_LIMIT_RPM, rpd: int = RATE_LIMIT_RPD) -> None:
        self.rpm = rpm
        self.rpd = rpd
        self._minute_tokens = rpm
        self._day_tokens = rpd
        self._last_refill = time.monotonic()
        self._day_start = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a request slot is available."""
        async with self._lock:
            now = time.monotonic()

            # Refill minute tokens
            elapsed = now - self._last_refill
            if elapsed >= 60.0:
                self._minute_tokens = self.rpm
                self._last_refill = now

            # Refill daily tokens
            if now - self._day_start >= 86400.0:
                self._day_tokens = self.rpd
                self._day_start = now

            # Wait if no minute tokens
            if self._minute_tokens <= 0:
                wait = 60.0 - (now - self._last_refill)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._minute_tokens = self.rpm
                self._last_refill = time.monotonic()

            # Hard stop if daily limit hit
            if self._day_tokens <= 0:
                raise RuntimeError(
                    "Daily API rate limit reached. Resume tomorrow or increase rpd."
                )

            self._minute_tokens -= 1
            self._day_tokens -= 1


class CheckpointManager:
    """Tracks which prompts have been completed so runs can resume."""

    def __init__(self, checkpoint_dir: Path) -> None:
        self.checkpoint_dir = checkpoint_dir
        self.checkpoint_file = checkpoint_dir / "checkpoint.json"
        self._completed: dict[int, str] = {}
        self._load()

    def _load(self) -> None:
        if self.checkpoint_file.exists():
            with open(self.checkpoint_file, encoding="utf-8") as f:
                data = json.load(f)
                self._completed = {int(k): v for k, v in data.items()}

    def save(self) -> None:
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        with open(self.checkpoint_file, "w", encoding="utf-8") as f:
            json.dump(self._completed, f, indent=2)

    def is_completed(self, index: int) -> bool:
        return index in self._completed

    def mark_completed(self, index: int, output_path: str) -> None:
        self._completed[index] = output_path
        self.save()

    def get_completed_indices(self) -> set[int]:
        return set(self._completed.keys())

    def clear(self) -> None:
        self._completed.clear()
        if self.checkpoint_file.exists():
            self.checkpoint_file.unlink()


class ImageGenerator:
    """Generates images from prompts using Gemini with OpenAI fallback."""

    def __init__(
        self,
        output_dir: Path,
        checkpoint_dir: Path | None = None,
        gemini_model: str | None = None,
        primary_provider: str = "gemini",
        fallback_provider: str = "openai",
    ) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.rate_limiter = RateLimiter()
        self.checkpoint = CheckpointManager(
            Path(checkpoint_dir) if checkpoint_dir else self.output_dir / "checkpoints"
        )

        # Run prefix for unique filenames
        eastern = ZoneInfo("America/New_York")
        run_start = datetime.now(eastern)
        self.run_prefix = run_start.strftime("run_%Y%m%d_%H%M%S_")
        day = run_start.day
        hour = run_start.hour % 12 or 12
        ampm = "AM" if run_start.hour < 12 else "PM"
        self._batch_label = f"{run_start.strftime('%b')} {day}, {hour}:{run_start.minute:02d} {ampm}"

        # Set up providers
        self.primary: ImageProvider = self._create_provider(primary_provider, gemini_model)
        self.fallback: ImageProvider | None = (
            self._create_provider(fallback_provider, gemini_model)
            if fallback_provider and fallback_provider != "none"
            else None
        )

    def _create_provider(self, name: str, gemini_model: str | None = None) -> ImageProvider:
        if name == "gemini":
            return GeminiProvider(model_name=gemini_model)
        if name == "openai":
            return OpenAIProvider()
        raise ValueError(f"Unknown provider: {name}")

    async def generate_batch(
        self,
        prompts: list["GenerationPrompt"],
        dry_run: bool = False,
    ) -> list[GeneratedImage]:
        """Generate images for a batch of prompts."""
        results: list[GeneratedImage] = []
        failed = 0

        for i, prompt in enumerate(prompts):
            if self.checkpoint.is_completed(i):
                continue

            if dry_run:
                result = self._make_dry_run_result(i, prompt)
            else:
                try:
                    result = await self._generate_single(i, prompt)
                except ImageGenerationAbortedError as e:
                    logger.error(
                        "Image %d/%d aborted batch: %s",
                        i + 1,
                        len(prompts),
                        e,
                    )
                    raise
                except Exception as e:
                    failed += 1
                    logger.error(
                        "Image %d/%d failed after retries: %s — skipping",
                        i + 1,
                        len(prompts),
                        e,
                    )
                    await asyncio.sleep(RETRY_DELAY)
                    continue

            self._save_metadata(i, prompt, result)
            results.append(result)
            self.checkpoint.mark_completed(i, str(result.image_path))

            # Pause between batches
            if (i + 1) % BATCH_SIZE == 0 and i + 1 < len(prompts):
                await asyncio.sleep(2.0)

        if failed:
            logger.warning("%d image(s) failed, %d succeeded", failed, len(results))

        return results

    def _save_metadata(
        self,
        index: int,
        prompt: "GenerationPrompt",
        result: GeneratedImage,
    ) -> None:
        """Save a JSON metadata sidecar next to each generated image."""
        meta_path = self.output_dir / f"{self.run_prefix}gen_{index:04d}_meta.json"

        meta = {
            "index": index,
            "image_path": str(result.image_path),
            "batch": self._batch_label,
            "product_name": prompt.product_name,
            "image_prompt_name": prompt.image_prompt_name,
            "aspect_ratio": prompt.aspect_ratio,
            "style_variant": prompt.style_variant,
            "style_reference": prompt.style_reference.name if prompt.style_reference else None,
            "reference_images": [p.name for p in prompt.reference_images],
            "logo_images": [p.name for p in prompt.logo_images],
            "prompt_text": prompt.prompt_text,
            "reference_image_paths": [str(p) for p in prompt.reference_images],
            "logo_image_paths": [str(p) for p in prompt.logo_images],
            "style_reference_path": str(prompt.style_reference) if prompt.style_reference else None,
        }
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

    async def _generate_single(
        self,
        index: int,
        prompt: "GenerationPrompt",
    ) -> GeneratedImage:
        """Generate a single image with rate limiting and fallback."""
        await self.rate_limiter.acquire()

        try:
            return await self._generate_with_provider(self.primary, index, prompt)
        except ProviderUnavailableError:
            if self.fallback:
                logger.warning(
                    "%s unavailable, falling back to %s",
                    self.primary.name,
                    self.fallback.name,
                )
                return await self._generate_with_provider(self.fallback, index, prompt)
            raise

    async def _generate_with_provider(
        self,
        provider: ImageProvider,
        index: int,
        prompt: "GenerationPrompt",
    ) -> GeneratedImage:
        """Generate a single image using a specific provider."""
        contents = provider.build_contents(prompt)
        provider.log_payload_size(contents, index, len(prompt.reference_images))
        response = await provider.call_api(prompt, contents)
        image_path = provider.parse_response(
            response,
            index,
            prompt,
            self.output_dir,
            self.run_prefix,
        )
        return GeneratedImage(
            image_path=image_path,
            prompt=prompt,
            generation_id=f"gen-{index:04d}",
        )

    def _make_dry_run_result(
        self,
        index: int,
        prompt: "GenerationPrompt",
    ) -> GeneratedImage:
        """Create a placeholder result for dry-run mode."""
        image_path = self.output_dir / f"{self.run_prefix}dryrun_{index:04d}.txt"
        ref_lines = "\n".join(f"  - {p}" for p in prompt.reference_images)
        image_path.write_text(
            f"[DRY RUN] Product: {prompt.product_name}\n"
            f"Reference images ({len(prompt.reference_images)}):\n"
            f"{ref_lines}\n\n"
            f"Prompt:\n{prompt.prompt_text}",
            encoding="utf-8",
        )
        return GeneratedImage(
            image_path=image_path,
            prompt=prompt,
            generation_id=f"dryrun-{index:04d}",
        )

    # --- Revision methods ---

    async def revise_image(
        self,
        image_path: Path,
        instructions: str,
        output_path: Path | None = None,
        context: RevisionContext | None = None,
    ) -> Path:
        """Revise an existing generated image."""
        if output_path is None:
            output_path = self._next_revision_path(image_path)

        contents, aspect_ratio = self._prepare_revision_contents(
            image_path,
            instructions,
            context,
        )

        try:
            return await self._revise_with_provider(
                self.primary,
                contents,
                aspect_ratio,
                output_path,
            )
        except ProviderUnavailableError:
            if self.fallback:
                logger.warning(
                    "%s unavailable for revision, falling back to %s",
                    self.primary.name,
                    self.fallback.name,
                )
                return await self._revise_with_provider(
                    self.fallback,
                    contents,
                    aspect_ratio,
                    output_path,
                )
            raise

    async def _revise_with_provider(
        self,
        provider: ImageProvider,
        contents: list[Any],
        aspect_ratio: str | None,
        output_path: Path,
    ) -> Path:
        response = await provider.call_revision_api(contents, aspect_ratio)
        return provider.parse_revision_response(response, output_path)

    def _prepare_revision_contents(
        self,
        image_path: Path,
        instructions: str,
        context: RevisionContext | None,
    ) -> tuple[list[Any], str | None]:
        """Build the contents list for a revision call."""
        source_img = Image.open(image_path)
        if source_img.mode not in ("RGB", "RGBA"):
            source_img = source_img.convert("RGB")

        if context is not None and (
            context.reference_images
            or context.logo_images
            or context.style_reference
            or context.prompt_text
        ):
            contents = self._build_revision_contents(instructions, source_img, context)
            aspect_ratio = context.aspect_ratio or None
            logger.info("Revision with context: %s", context.summary())
        else:
            edit_prompt = (
                "Edit this advertisement image. Keep everything "
                "that is not mentioned in the instructions exactly "
                "as it is. Only change what is specifically requested:\n\n"
                f"{instructions}"
            )
            contents = [edit_prompt, source_img]
            aspect_ratio = None

        return contents, aspect_ratio

    def _build_revision_contents(
        self,
        instructions: str,
        source_img: Image.Image,
        context: RevisionContext,
    ) -> list[Any]:
        """Build the full contents list for a context-aware revision."""
        prompt_text = self._build_revision_prompt(instructions, context)
        contents: list[Any] = [prompt_text]

        # Style reference
        if context.style_reference:
            try:
                img = Image.open(context.style_reference)
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                contents.append(img)
            except Exception as e:
                logger.warning("Could not load style reference %s: %s", context.style_reference, e)

        # Reference images
        for ref_path in context.reference_images:
            try:
                img = Image.open(ref_path)
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                contents.append(img)
            except Exception as e:
                logger.warning("Could not load reference image %s: %s", ref_path, e)

        # Logo
        for logo_path in context.logo_images:
            try:
                img = Image.open(logo_path)
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                contents.append(img)
            except Exception as e:
                logger.warning("Could not load logo image %s: %s", logo_path, e)

        # Source image last
        contents.append(source_img)

        return contents

    @staticmethod
    def _build_revision_prompt(instructions: str, context: RevisionContext) -> str:
        """Build a structured revision prompt with full context."""
        parts: list[str] = [
            "You are editing an existing advertisement image. "
            "Keep everything that is not mentioned in the edit "
            "instructions exactly as it is — same model, same "
            "text, same layout, same colors. Only change what "
            "is specifically requested.",
        ]

        if context.reference_images:
            parts.append(
                "\nREFERENCE PHOTOS: I have provided the original "
                "model/product reference photos. Keep them exactly "
                "as they appear — do NOT regenerate or alter them."
            )

        if context.logo_images:
            parts.append(
                "\nLOGO: Keep the logo exactly as provided — "
                "do not re-draw or re-type it."
            )

        if context.style_reference:
            parts.append(
                "\nSTYLE REFERENCE: Maintain the same layout style "
                "and design aesthetic."
            )

        parts.append(f"\nEDIT INSTRUCTIONS:\n{instructions}")

        # Auto-append shift directive for removal instructions
        removal_keywords = ["remove", "delete", "get rid of", "take out", "eliminate", "drop", "hide"]
        if any(kw in instructions.lower() for kw in removal_keywords):
            parts.append(
                "\nAfter removing the specified element(s), shift "
                "other elements slightly to fill the empty space."
            )

        if context.prompt_text:
            trimmed = context.prompt_text[:1500]
            if len(context.prompt_text) > 1500:
                trimmed += "..."
            parts.append(
                f"\nORIGINAL GENERATION CONTEXT (for reference):\n{trimmed}"
            )

        return "\n".join(parts)

    @staticmethod
    def _next_revision_path(original: Path) -> Path:
        """Find the next available revision filename."""
        stem = original.stem
        base = stem.split("_rev")[0]
        parent = original.parent
        rev = 1
        while True:
            candidate = parent / f"{base}_rev{rev}.png"
            if not candidate.exists():
                return candidate
            rev += 1
