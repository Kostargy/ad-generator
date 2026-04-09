"""
Headline generation service.

Uses Gemini/OpenAI to generate ad headlines based on product and campaign data.
Supports fallback between providers based on settings.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

logger = logging.getLogger(__name__)


class HeadlineGenerator:
    """Generates ad headlines using Gemini or OpenAI with fallback support."""

    SYSTEM_PROMPT = """# Ad Headline Generator

You are an expert advertising copywriter for {product_name}.

## Target Persona
{persona_description}

## Product Context
{product_context}

## Campaign Brief
{brief}

## Brand Guidelines
{master_prompt}

## Instructions
- Generate exactly {number_of_headlines} headline variations
- Keep headlines short: 3-8 words ideal, 12 words max
- Lead with benefit or outcome, not feature
- Each headline should work standalone on an ad image
- Include variety: benefit-first, lifestyle, urgency, social proof angles

Output ONLY the headlines, one per line. No numbering, no bullet points, no explanations."""

    def __init__(self) -> None:
        self._gemini_client: Any | None = None
        self._openai_client: Any | None = None
        self._master_prompt: str = ""
        self._primary_provider: str = "gemini"
        self._gemini_api_key: str = ""
        self._openai_api_key: str = ""
        self.max_retries = 3
        self.retry_delay_seconds = 2.0

    def _load_settings(self) -> None:
        """Load API keys and settings from database."""
        try:
            from everdries_ad_generator.campaigns.models import APISettings
            api_settings = APISettings.get_settings()

            self._gemini_api_key = api_settings.gemini_api_key or os.environ.get("GEMINI_API_KEY", "")
            self._openai_api_key = api_settings.openai_api_key or os.environ.get("OPENAI_API_KEY", "")
            self._master_prompt = api_settings.master_prompt or APISettings.DEFAULT_MASTER_PROMPT
            self._primary_provider = api_settings.primary_provider or "gemini"
            self.max_retries = api_settings.critic_max_retries  # Reuse retry setting
        except Exception as e:
            logger.warning("Could not load API settings: %s", e)
            self._gemini_api_key = os.environ.get("GEMINI_API_KEY", "")
            self._openai_api_key = os.environ.get("OPENAI_API_KEY", "")

    def _get_gemini_client(self) -> Any | None:
        """Get or create Gemini client."""
        if self._gemini_client is None and self._gemini_api_key:
            try:
                from google import genai
                self._gemini_client = genai.Client(api_key=self._gemini_api_key)
            except Exception as e:
                logger.error("Failed to initialize Gemini client: %s", e)
        return self._gemini_client

    def _get_openai_client(self) -> Any | None:
        """Get or create OpenAI client."""
        if self._openai_client is None and self._openai_api_key:
            try:
                from openai import OpenAI
                self._openai_client = OpenAI(api_key=self._openai_api_key)
            except Exception as e:
                logger.error("Failed to initialize OpenAI client: %s", e)
        return self._openai_client

    def _generate_with_gemini(self, prompt: str) -> str:
        """Generate headlines using Gemini with retry logic."""
        from google.genai import types

        client = self._get_gemini_client()
        if client is None:
            raise RuntimeError("Gemini client not available")

        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=[prompt],
                    config=types.GenerateContentConfig(
                        temperature=0.9,
                    ),
                )
                return response.text.strip()
            except Exception as e:
                last_error = e
                error_str = str(e)

                # Retry on rate limit or transient errors
                if ("429" in error_str or "503" in error_str or "overloaded" in error_str.lower()) and attempt < self.max_retries:
                    wait = self.retry_delay_seconds * (2 ** attempt)
                    logger.warning(
                        "Gemini rate limited/unavailable, retrying in %ds (attempt %d/%d): %s",
                        wait, attempt + 1, self.max_retries, e
                    )
                    time.sleep(wait)
                else:
                    break

        raise RuntimeError(f"Gemini generation failed after {self.max_retries + 1} attempts: {last_error}")

    def _generate_with_openai(self, prompt: str) -> str:
        """Generate headlines using OpenAI."""
        client = self._get_openai_client()
        if client is None:
            raise RuntimeError("OpenAI client not available")

        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are an expert advertising copywriter."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.9,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            raise RuntimeError(f"OpenAI generation failed: {e}")

    @staticmethod
    def _sanitize_output(raw: str, count: int) -> str:
        """Clean model output into one headline per line.

        Strips numbering, bullets, surrounding quotes, and any preamble/blank
        lines so each line is exactly one headline ready to feed the image
        generator (which builds one ad per line).
        """
        lines = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            # Drop leading numbering ("1.", "1)", "1:") or bullets ("-", "*", "•").
            line = re.sub(r"^\s*(?:[-*•]|\d+[.)\]:])\s*", "", line)
            # Drop "Headline 1:" / "Option 2 -" style prefixes.
            line = re.sub(
                r"^\s*(?:headline|option|variation|variant)\s*\d*\s*[:\-–—]\s*",
                "",
                line,
                flags=re.IGNORECASE,
            )
            # Strip a single pair of wrapping quotes.
            if len(line) >= 2 and line[0] in {'"', "'", "“", "‘"} and line[-1] in {'"', "'", "”", "’"}:
                line = line[1:-1].strip()
            # Skip preamble lines like "Here are 5 headlines:".
            if not line or line.endswith(":"):
                continue
            lines.append(line)
        return "\n".join(lines[:count])

    def generate(
        self,
        product_name: str,
        product_context: str,
        persona_description: str,
        brief: str,
        count: int = 5,
    ) -> str:
        """Generate headlines using configured provider with fallback.

        Args:
            product_name: Name of the product/campaign.
            product_context: Product description and context.
            persona_description: Target customer persona.
            brief: Campaign brief/direction.
            count: Number of headlines to generate.

        Returns:
            String with headlines, one per line.
        """
        # Load settings
        self._load_settings()

        # Check if any API key is available
        if not self._gemini_api_key and not self._openai_api_key:
            logger.error("No API keys configured for headline generation")
            return "Error: No API keys configured. Please add Gemini or OpenAI API key in Settings."

        # Build prompt
        prompt = self.SYSTEM_PROMPT.format(
            product_name=product_name or "the product",
            product_context=product_context or "No additional context provided.",
            persona_description=persona_description or "General audience.",
            brief=brief or "Generate general advertising headlines.",
            master_prompt=self._master_prompt or "No specific brand guidelines.",
            number_of_headlines=count,
        )

        # Determine provider order based on settings
        if self._primary_provider in ("openai", "openai_only"):
            providers = [("openai", self._generate_with_openai)]
            if self._primary_provider == "openai" and self._gemini_api_key:
                providers.append(("gemini", self._generate_with_gemini))
        else:
            # Default: Gemini first
            providers = [("gemini", self._generate_with_gemini)]
            if self._primary_provider == "gemini" and self._openai_api_key:
                providers.append(("openai", self._generate_with_openai))

        # Filter to only providers with API keys
        providers = [
            (name, func) for name, func in providers
            if (name == "gemini" and self._gemini_api_key) or (name == "openai" and self._openai_api_key)
        ]

        if not providers:
            return "Error: No valid API keys for configured providers."

        # Try each provider
        errors = []
        for provider_name, generate_func in providers:
            try:
                logger.info("Generating headlines with %s", provider_name)
                result = generate_func(prompt)
                logger.info("Headlines generated successfully with %s", provider_name)
                return self._sanitize_output(result, count)
            except Exception as e:
                error_msg = f"{provider_name}: {e}"
                errors.append(error_msg)
                logger.warning("Headline generation failed with %s: %s", provider_name, e)

                # If there's a fallback, continue to next provider
                if len(providers) > 1:
                    logger.info("Falling back to next provider...")
                continue

        # All providers failed
        error_summary = "; ".join(errors)
        logger.error("All providers failed for headline generation: %s", error_summary)
        return f"Error: All providers failed. {error_summary}"
