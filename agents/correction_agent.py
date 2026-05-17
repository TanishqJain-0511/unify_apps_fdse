"""CorrectionAgent — LLM-driven recovery for CRM payload rejections."""

import logging
from typing import Optional

from openai import AsyncOpenAI

from agents.llm_parser import OPENAI_API_KEY, OPENAI_MODEL
from agents.schemas import CustomerOnboardingRecord

logger = logging.getLogger(__name__)

CORRECTION_SYSTEM_PROMPT = (
    "You are a data correction assistant. You will receive a customer record "
    "that was rejected by a CRM system, along with the error message returned. "
    "Fix only what is necessary to resolve the error. "
    "Do not invent or fabricate data that was not present in the original record. "
    "Return the corrected record in the same schema."
)


class CorrectionAgent:
    def __init__(self) -> None:
        self.client = AsyncOpenAI(api_key=OPENAI_API_KEY)

    async def correct(
        self,
        record: CustomerOnboardingRecord,
        crm_error: str,
    ) -> Optional[CustomerOnboardingRecord]:
        """
        Attempt to correct a CRM-rejected record using the LLM.

        Returns a corrected CustomerOnboardingRecord, or None if the model
        cannot fix the record.
        """
        record_json = record.model_dump_json(indent=2)
        user_message = (
            f"The following customer record was rejected by the CRM:\n\n"
            f"{record_json}\n\n"
            f"CRM error: {crm_error}\n\n"
            f"Return a corrected version of this record that resolves the error."
        )

        response = await self.client.beta.chat.completions.parse(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": CORRECTION_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            response_format=CustomerOnboardingRecord,
        )

        corrected = response.choices[0].message.parsed
        if corrected is None:
            logger.warning("CorrectionAgent: model could not fix record for %s", record.email)
            return None

        # Preserve pipeline metadata from the original record
        corrected.source_file = record.source_file
        logger.info("CorrectionAgent: produced corrected record for %s", record.email)
        return corrected
