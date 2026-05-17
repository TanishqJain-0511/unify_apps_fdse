"""LLMParser — uses OpenAI gpt-4o-mini to extract structured customer data from raw text."""

import asyncio
import logging
import random
from typing import Optional

import openai
from openai import AsyncOpenAI

from agents.prompts import SYSTEM_PROMPT
from agents.schemas import CustomerOnboardingRecord

logger = logging.getLogger(__name__)

OPENAI_API_KEY = "YOUR_OPENAI_API_KEY"
OPENAI_MODEL = "gpt-4o-mini"

MAX_RETRIES = 5
BASE_DELAY = 1.0
MAX_DELAY = 60.0


class LLMParser:
    def __init__(self) -> None:
        self.client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        self._sem = asyncio.Semaphore(10)

    async def parse(self, content: str, source_file: str) -> Optional[CustomerOnboardingRecord]:
        """
        Send raw file content to OpenAI and return a CustomerOnboardingRecord.

        Returns None if required fields (name/email) are absent or the model refuses.
        Retries up to MAX_RETRIES times on rate-limit errors with exponential backoff.
        Raises openai.APIError on unrecoverable API failures.
        """
        user_message = (
            f"File: {source_file}\n\n"
            f"--- BEGIN FILE CONTENT ---\n{content}\n--- END FILE CONTENT ---"
        )

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]

        response = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with self._sem:
                    response = await self.client.beta.chat.completions.parse(
                        model=OPENAI_MODEL,
                        messages=messages,
                        response_format=CustomerOnboardingRecord,
                    )
                break  # success — exit retry loop

            except openai.RateLimitError as exc:
                if attempt == MAX_RETRIES:
                    raise

                retry_after = _parse_retry_after(exc)
                if retry_after is None:
                    retry_after = min(BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 1), MAX_DELAY)

                logger.warning(
                    "OpenAI rate-limit for %s (attempt %d/%d). Retrying in %.1fs.",
                    source_file, attempt, MAX_RETRIES, retry_after,
                )
                await asyncio.sleep(retry_after)

        assert response is not None  # guaranteed: loop only exits via break or raise
        record = response.choices[0].message.parsed
        if record is None:
            logger.warning("Model refused to extract data from %s", source_file)
            return None

        if not record.name or not record.email:
            logger.warning("Missing required fields (name/email) in %s", source_file)
            return None

        record.source_file = source_file
        return record


def _parse_retry_after(exc: openai.RateLimitError) -> Optional[float]:
    """Extract Retry-After seconds from the response headers, if present."""
    try:
        value = exc.response.headers.get("retry-after")
        return float(value) if value is not None else None
    except Exception:
        return None
