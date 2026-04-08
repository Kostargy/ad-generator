"""Abstract base class for image generation providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from everdries_ad_generator.campaigns.services.image_gen_adapter import GenerationPrompt


class ProviderUnavailableError(Exception):
    """Raised when a provider is temporarily unavailable (503, overloaded, quota).

    This triggers fallback to the next provider rather than a hard failure.
    """


class ImageGenerationAbortedError(Exception):
    """Raised when generation should abort the entire batch immediately.

    Used for non-recoverable model responses (e.g. IMAGE_OTHER from Gemini)
    where retrying or moving on to the next image is wasteful — every image
    in the batch will hit the same failure.
    """


class ImageProvider(ABC):
    """Interface that all image generation providers must implement."""

    name: str

    @abstractmethod
    def build_contents(self, prompt: "GenerationPrompt") -> Any:
        """Convert prompt + reference images to provider-specific format."""

    @abstractmethod
    async def call_api(self, prompt: "GenerationPrompt", contents: Any) -> Any:
        """Make the API call with retry logic.

        Raises:
            ProviderUnavailableError: When the provider is overloaded/unavailable.
        """

    @abstractmethod
    def parse_response(
        self,
        response: Any,
        index: int,
        prompt: "GenerationPrompt",
        output_dir: Path,
        run_prefix: str,
    ) -> Path:
        """Extract image from response and save to disk. Returns image path."""

    @abstractmethod
    async def call_revision_api(
        self,
        contents: list[Any],
        aspect_ratio: str | None,
    ) -> Any:
        """Make a revision API call.

        Raises:
            ProviderUnavailableError: When the provider is overloaded/unavailable.
        """

    @abstractmethod
    def parse_revision_response(self, response: Any, output_path: Path) -> Path:
        """Extract revised image from response and save to disk."""

    @abstractmethod
    def log_payload_size(
        self, contents: Any, index: int, ref_count: int
    ) -> None:
        """Estimate and log the total payload size for an API call."""
