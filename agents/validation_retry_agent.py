"""ValidationRetryAgent — targeted second-pass extraction for missing name/email."""

import logging
from typing import Optional

from openai import AsyncOpenAI

from agents.llm_parser import OPENAI_API_KEY, OPENAI_MODEL
from agents.schemas import CustomerOnboardingRecord

logger = logging.getLogger(__name__)

VALIDATION_RETRY_SYSTEM_PROMPT = (
    "You are a contact-information extraction specialist. "
    "A previous extraction attempt failed to find a customer name and/or email address in the document provided. "
    "Re-read the document with specific attention to any person's name and email address. "
    "Look carefully for implicit references such as email signatures, 'From:' or 'CC:' headers, "
    "'Contact:' lines, sign-offs, footers, and any other unconventional locations. "
    "Do not invent or fabricate data that is not present in the document. "
    "If a name or email is genuinely absent, return null for that field. "
    "Return the full record in the same schema."
)


class ValidationRetryAgent:
    def __init__(self) -> None:
        self.client = AsyncOpenAI(api_key=OPENAI_API_KEY)

    async def recover_missing_fields(
        self,
        content: str,
        source_file: str,
        previous_attempt_failed: bool = False,
    ) -> Optional[CustomerOnboardingRecord]:
        """
        Re-attempt extraction with a focused prompt targeting name and email.

        Set previous_attempt_failed=True on the second call to give the model
        additional signal that a prior targeted attempt also failed.

        Returns a CustomerOnboardingRecord (with source_file set) if name and
        email are found, or None if the document genuinely lacks them.
        """
        prefix = (
            "Note: A previous targeted extraction attempt also failed to find name/email. "
            "Look even more carefully, including partial names, initials, or email-like patterns.\n\n"
            if previous_attempt_failed else ""
        )
        user_message = (
            f"{prefix}File: {source_file}\n\n"
            f"--- BEGIN FILE CONTENT ---\n{content}\n--- END FILE CONTENT ---"
        )

        response = await self.client.beta.chat.completions.parse(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": VALIDATION_RETRY_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            response_format=CustomerOnboardingRecord,
        )

        record = response.choices[0].message.parsed
        if record is None:
            logger.warning("ValidationRetryAgent: model refused to extract data from %s", source_file)
            return None

        if not record.name or not record.email:
            logger.warning("ValidationRetryAgent: still missing name/email in %s", source_file)
            return None

        record.source_file = source_file
        return record
