"""CRMWriter — POSTs customer records to the legacy CRM REST API with full resilience.

Retry strategy
--------------
  429 Rate-Limited  → honour Retry-After header, then retry (counts as one attempt)
  5xx Server Error  → exponential backoff with jitter, up to MAX_RETRIES attempts
  4xx Client Error  → raise immediately (no retry — request is broken)
  Network Error     → exponential backoff with jitter
"""

import logging
import random
import time
from typing import Any

import httpx

from agents.schemas import CustomerOnboardingRecord

logger = logging.getLogger(__name__)

CRM_BASE_URL = "https://crm.example.com"
CRM_API_KEY = "YOUR_CRM_API_KEY"
CRM_TIMEOUT = 30
MAX_RETRIES = 5
BASE_DELAY = 1.0
MAX_DELAY = 60.0


class CRMClientError(Exception):
    """Non-retryable 4xx error from the CRM API."""


class CRMServerError(Exception):
    """Retryable 5xx error from the CRM API."""


class CRMWriter:
    def __init__(self) -> None:
        self._base_url = CRM_BASE_URL.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {CRM_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_customer(self, record: CustomerOnboardingRecord) -> dict[str, Any]:
        """
        Write a customer record to the CRM.

        Retries on rate-limits and server errors.
        Raises on non-retryable errors or exhausted retries.
        """
        payload = self._build_payload(record)
        last_exc: Exception = RuntimeError("No attempts made")

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                result = self._post(payload)
                logger.info(
                    "CRM contact created for %s (attempt %d/%d)",
                    record.email,
                    attempt,
                    MAX_RETRIES,
                )
                return result

            except _RateLimitError as exc:
                logger.warning(
                    "CRM rate-limited (attempt %d/%d). Waiting %ds.",
                    attempt,
                    MAX_RETRIES,
                    exc.retry_after,
                )
                time.sleep(exc.retry_after)
                last_exc = exc

            except CRMClientError:
                raise  # 4xx — no retry

            except (CRMServerError, httpx.RequestError) as exc:
                delay = self._backoff(attempt)
                logger.warning(
                    "CRM error on attempt %d/%d (%s). Retrying in %.1fs.",
                    attempt,
                    MAX_RETRIES,
                    exc,
                    delay,
                )
                time.sleep(delay)
                last_exc = exc

        raise RuntimeError(
            f"CRM write failed after {MAX_RETRIES} attempts for {record.email}"
        ) from last_exc

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_payload(self, record: CustomerOnboardingRecord) -> dict[str, Any]:
        return {
            "contact": {
                "full_name": record.name,
                "email": record.email,
                "phone": record.phone,
                "company_name": record.company,
                "address": record.address,
                "plan_type": record.plan_type,
                "subscription_type": record.subscription_type,
                "activation_date": record.activation_date,
                "effective_date": record.effective_date,
                "dob": record.dob,
                "contract_value": record.contract_value,
                "currency": record.currency,
                "document_type": record.document_type,
                "custom_fields": record.custom_fields,
                "source": f"automated_onboarding:{record.source_file}",
            }
        }

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        with httpx.Client(timeout=CRM_TIMEOUT) as client:
            response = client.post(
                f"{self._base_url}/api/v1/contacts",
                json=payload,
                headers=self._headers,
            )

        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", "60"))
            raise _RateLimitError(retry_after)

        if 400 <= response.status_code < 500:
            raise CRMClientError(
                f"CRM returned {response.status_code}: {response.text[:300]}"
            )

        if response.status_code >= 500:
            raise CRMServerError(
                f"CRM returned {response.status_code}: {response.text[:300]}"
            )

        return response.json()

    def _backoff(self, attempt: int) -> float:
        delay = BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 1)
        return min(delay, MAX_DELAY)


class _RateLimitError(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__(f"Rate limited — retry after {retry_after}s")
