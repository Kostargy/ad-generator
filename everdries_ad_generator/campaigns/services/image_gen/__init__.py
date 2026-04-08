"""
Inlined image generation package.

Provides ImageGenerator for batch image generation with Gemini/OpenAI providers.
"""

from .generator import GeneratedImage, ImageGenerator, RevisionContext

__all__ = [
    "GeneratedImage",
    "ImageGenerator",
    "RevisionContext",
]
