"""
Headline and supplementary-copy generation service.

Uses Anthropic Claude to generate ad headlines and supporting feature/benefit
copy based on product and campaign data. The specific Claude model is
configured in APISettings.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

logger = logging.getLogger(__name__)


UNIVERSAL_COPY_RULES = """VOICE & TONE
- Third person or imperative only — never first person ("I", "my", "me").
- Warm, relatable, empowering — never clinical, patronizing, or medical.
- Say "leaks", not "incontinence" or "bladder".

BANNED WORDS / CLAIMS
- Never use: "premium", "luxury", "Trustpilot", star ratings, review counts.
- Never use clinical or diagnosis language.
- Do NOT overclaim absorbency — overclaiming is the top driver of 1-star reviews.

STRUCTURE
- Headlines: 4–8 words ideal, 12 words hard max.
- Every headline communicates a benefit or outcome, not a feature.
- Each headline must work standalone on a single ad image.
- One headline per ad — never output multiple variants glued together.
- Supplementary copy lines: 2–5 words, short, factual, scannable.
- Features ≠ headlines: features are factual/informational, headlines carry the emotion.

CONTENT SOURCING
- Draw from real customer language, situations, and numbers. Do not invent claims.
- If the brief or product context gives you specific numbers, scenarios, or customer phrases, prefer those over generic copy.

CAMPAIGN-AWARE
- Deal / DOM campaigns: include the price and urgency language.
- Evergreen: lead with lifestyle benefit.
- Seasonal: tie to the occasion (Mother's Day, summer travel, gifting, etc.)."""


SUB_COPY_RULES = """PURPOSE
- Supplementary copy lines are short supporting callouts that sit alongside the headline on an ad image.
- They exist to reinforce the headline with product facts, proof points, and practical details — not to repeat it.

LENGTH & FORMAT
- 2–5 words per line. Hard max: 5 words.
- One line per line. No punctuation beyond necessary apostrophes — no full stops at the end.
- Title Case or sentence case, consistent across the set.

CONTENT
- Factual, informational, scannable. Product attributes, proof points, practical details.
- NOT emotional — the headline already carries the emotion. These are the callouts that make the ad credible.
- Examples of the right shape: "Machine Washable", "Leakproof For 8 Hours", "Seamless Under Clothes", "Free US Shipping".
- Do NOT restate the headline's benefit in different words. Each line must add new information.
- Do NOT invent claims. Only use facts supported by the product context or campaign brief.

VOICE
- Third person or imperative. Never first person.
- Never use: "premium", "luxury", "Trustpilot", clinical or medical language.
- Do NOT overclaim absorbency.

VARIETY
- Across the set, mix attribute types: materials, protection, comfort, usage, convenience, price/offer.
- No two lines should say the same thing."""


class HeadlineGenerator:
    """Generates ad headlines using Anthropic Claude."""

    SYSTEM_PROMPT = """You are an expert advertising copywriter for {product_name}.

## Target Persona
{persona_description}

## Product Context
{product_context}

## Campaign Brief
{brief}

## Universal Copy Rules
{universal_rules}

The Universal Copy Rules above are non-negotiable and override any conflicting instruction in Brand Guidelines or Campaign Brief.

## Brand Guidelines
{master_prompt}

## Number of Headlines
{number_of_headlines}

## Output Format (STRICT)
Output ONLY the headlines, one per line, and NOTHING else.
- No preamble, no explanation, no closing remarks.
- No numbering, no bullets, no dashes.
- No headings, no labels (e.g. "Headline 1:"), no quotes around lines.
- No markdown formatting of any kind.
- Do NOT start the output with a title line such as "Advertising Headlines", "Headlines", "Here are the headlines", or any similar heading/intro. The very first line of your output must already be the first headline.
- Exactly one headline per line. No blank lines between them."""

    def __init__(self) -> None:
        self._anthropic_client: Any | None = None
        self._anthropic_api_key: str = ""
        self._anthropic_model: str = "claude-haiku-4-5-20251001"
        self._master_prompt: str = ""
        self.max_retries = 3
        self.retry_delay_seconds = 2.0

    def _load_settings(self) -> None:
        """Load API key, model, and master prompt from database."""
        try:
            from everdries_ad_generator.campaigns.models import APISettings
            api_settings = APISettings.get_settings()

            self._anthropic_api_key = (
                api_settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", "")
            )
            self._anthropic_model = (
                api_settings.headline_anthropic_model or "claude-haiku-4-5-20251001"
            )
            self._master_prompt = api_settings.master_prompt or APISettings.DEFAULT_MASTER_PROMPT
            self.max_retries = api_settings.critic_max_retries  # Reuse retry setting
        except Exception as e:
            logger.warning("Could not load API settings: %s", e)
            self._anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    def _get_anthropic_client(self) -> Any | None:
        """Get or create Anthropic client."""
        if self._anthropic_client is None and self._anthropic_api_key:
            try:
                import anthropic
                self._anthropic_client = anthropic.Anthropic(api_key=self._anthropic_api_key)
            except Exception as e:
                logger.error("Failed to initialize Anthropic client: %s", e)
        return self._anthropic_client

    def _generate_with_anthropic(self, prompt: str) -> str:
        """Generate text using Anthropic Claude with retry logic."""
        import anthropic

        client = self._get_anthropic_client()
        if client is None:
            raise RuntimeError("Anthropic client not available")

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                msg = client.messages.create(
                    model=self._anthropic_model,
                    max_tokens=1024,
                    messages=[{"role": "user", "content": prompt}],
                )
                # content is a list of content blocks; first block is text for our prompts.
                if not msg.content:
                    raise RuntimeError("Anthropic returned empty content")
                text = getattr(msg.content[0], "text", "") or ""
                return text.strip()
            except anthropic.RateLimitError as e:
                last_error = e
                transient = True
            except anthropic.APIConnectionError as e:
                last_error = e
                transient = True
            except anthropic.APIStatusError as e:
                last_error = e
                transient = getattr(e, "status_code", None) in {429, 500, 502, 503, 529}
            except Exception as e:
                last_error = e
                transient = False

            if transient and attempt < self.max_retries:
                wait = self.retry_delay_seconds * (2 ** attempt)
                logger.warning(
                    "Anthropic transient error, retrying in %ds (attempt %d/%d): %s",
                    wait, attempt + 1, self.max_retries, last_error,
                )
                time.sleep(wait)
                continue
            break

        raise RuntimeError(
            f"Anthropic generation failed after {self.max_retries + 1} attempts: {last_error}"
        )

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
            # Strip leading markdown heading markers ("# ", "## ", etc.).
            line = re.sub(r"^#+\s*", "", line)
            # Strip inline markdown: links, bold, italic, code, strikethrough.
            # Order matters: handle **bold** before *italic* so bold doesn't half-strip.
            line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
            line = re.sub(r"\*\*([^*]+)\*\*", r"\1", line)
            line = re.sub(r"__([^_]+)__", r"\1", line)
            line = re.sub(r"\*([^*]+)\*", r"\1", line)
            line = re.sub(r"_([^_]+)_", r"\1", line)
            line = re.sub(r"`([^`]+)`", r"\1", line)
            line = re.sub(r"~~([^~]+)~~", r"\1", line)
            # Strip a single pair of wrapping quotes.
            if len(line) >= 2 and line[0] in {'"', "'", "“", "‘"} and line[-1] in {'"', "'", "”", "’"}:
                line = line[1:-1].strip()
            # Skip preamble lines like "Here are 5 headlines:".
            if not line or line.endswith(":"):
                continue
            # Skip title-like first lines such as "Advertising Headlines",
            # "Headlines", "Supplementary Copy", "Features", etc.
            if not lines and re.match(
                r"^(advertising\s+)?(headlines?|supplementary\s+copy|features?|options?|variations?)\s*$",
                line,
                flags=re.IGNORECASE,
            ):
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
        """Generate headlines using Anthropic Claude.

        Args:
            product_name: Name of the product/campaign.
            product_context: Product description and context.
            persona_description: Target customer persona.
            brief: Campaign brief/direction.
            count: Number of headlines to generate.

        Returns:
            String with headlines, one per line, or an "Error: ..." string
            on failure.
        """
        self._load_settings()

        if not self._anthropic_api_key:
            logger.error("No Anthropic API key configured for headline generation")
            return "Error: No Anthropic API key configured. Please add it in Settings."

        prompt = self.SYSTEM_PROMPT.format(
            product_name=product_name or "the product",
            product_context=product_context or "No additional context provided.",
            persona_description=persona_description or "General audience.",
            brief=brief or "Generate general advertising headlines.",
            universal_rules=UNIVERSAL_COPY_RULES,
            master_prompt=self._master_prompt or "No specific brand guidelines.",
            number_of_headlines=count,
        )

        try:
            logger.info("Generating headlines with Anthropic %s", self._anthropic_model)
            result = self._generate_with_anthropic(prompt)
            logger.info("Headlines generated successfully")
            return self._sanitize_output(result, count)
        except Exception as e:
            logger.error("Headline generation failed: %s", e)
            return f"Error: {e}"


class SupplementaryCopyGenerator(HeadlineGenerator):
    """Generates short feature/benefit callout lines that sit alongside the headline.

    Reuses HeadlineGenerator's Anthropic client and sanitizer plumbing — only
    the system prompt and the public method signature differ.
    """

    SYSTEM_PROMPT = """You are an expert advertising copywriter for {product_name}.

## Target Persona
{persona_description}

## Product Context
{product_context}

## Campaign Brief
{brief}

## Universal Copy Rules
{universal_rules}

## Supplementary Copy Rules
{sub_copy_rules}

The Universal Copy Rules and Supplementary Copy Rules above are non-negotiable and override any conflicting instruction in Brand Guidelines or Campaign Brief.

## Brand Guidelines
{master_prompt}

## Existing Headlines (for tone reference — do NOT repeat these)
{headlines}

## Number of Lines
{number_of_lines}

## Output Format (STRICT)
Output ONLY the supplementary copy lines, one per line, and NOTHING else.
- No preamble, no explanation, no closing remarks.
- No numbering, no bullets, no dashes.
- No headings, no labels, no quotes around lines.
- No markdown formatting of any kind.
- Do NOT start the output with a title line such as "Supplementary Copy", "Features", "Here are the lines", or any similar heading/intro. The very first line of your output must already be the first supplementary copy line.
- Exactly one line per line. No blank lines between them."""

    def generate(  # type: ignore[override]
        self,
        product_name: str,
        product_context: str,
        persona_description: str,
        brief: str,
        headlines: str = "",
        count: int = 5,
    ) -> str:
        """Generate supplementary copy lines using Anthropic Claude."""
        self._load_settings()

        if not self._anthropic_api_key:
            logger.error("No Anthropic API key configured for supplementary copy generation")
            return "Error: No Anthropic API key configured. Please add it in Settings."

        prompt = self.SYSTEM_PROMPT.format(
            product_name=product_name or "the product",
            product_context=product_context or "No additional context provided.",
            persona_description=persona_description or "General audience.",
            brief=brief or "Generate general supporting copy.",
            universal_rules=UNIVERSAL_COPY_RULES,
            sub_copy_rules=SUB_COPY_RULES,
            master_prompt=self._master_prompt or "No specific brand guidelines.",
            headlines=headlines.strip() or "(none provided)",
            number_of_lines=count,
        )

        try:
            logger.info("Generating supplementary copy with Anthropic %s", self._anthropic_model)
            result = self._generate_with_anthropic(prompt)
            logger.info("Supplementary copy generated successfully")
            return self._sanitize_output(result, count)
        except Exception as e:
            logger.error("Supplementary copy generation failed: %s", e)
            return f"Error: {e}"
