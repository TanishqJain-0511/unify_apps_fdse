from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, EmailStr, Field


class CustomerOnboardingRecord(BaseModel):
    # Document classification
    document_type: Literal[
        "onboarding_email",
        "contract",
        "kyc_document",
        "meeting_notes",
        "spreadsheet_record",
        "mixed_enterprise_document",
        "unknown",
    ]

    # Core identity
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    company: Optional[str] = None
    phone: Optional[str] = None

    # Business / CRM
    plan_type: Optional[str] = None
    subscription_type: Optional[str] = None
    activation_date: Optional[str] = None
    effective_date: Optional[str] = None

    # KYC / Address
    dob: Optional[str] = None
    address: Optional[str] = None

    # Financial
    contract_value: Optional[float] = None
    currency: Optional[str] = None

    # Confidence
    confidence_score: Optional[float] = None

    # Fallback
    custom_fields: Dict[str, Any] = {}

    # Pipeline metadata — excluded from OpenAI schema, set after extraction
    source_file: Optional[str] = Field(default=None, exclude=True)
