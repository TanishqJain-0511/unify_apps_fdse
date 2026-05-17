"""
main.py — Orchestrator for the enterprise customer onboarding pipeline.

Flow
----
  S3 (raw files)
    → S3Ingestor          : list + download all objects
                            download failures → onboarding_failures.json
    → LLMParser           : concurrent OpenAI calls → CustomerOnboardingRecord per file
                            rate-limit errors retried with backoff (×5); exhausted → onboarding_failures.json
    → ValidationRetryAgent: if name/email missing, targeted LLM retry (×2)
                            still missing → skipped
    → CRMWriter           : POST to legacy CRM API (sequential)
                            429 / 5xx / network errors retried with backoff (×5)
    → CorrectionAgent     : if CRM returns 4xx, LLM fixes the record and retries (×4)
    → Failure file        : all unrecoverable failures written to onboarding_failures.json
"""

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone

import openai

from agents.correction_agent import CorrectionAgent
from agents.crm_writer import CRMClientError, CRMWriter
from agents.llm_parser import LLMParser
from agents.s3_ingestor import S3Ingestor
from agents.validation_retry_agent import ValidationRetryAgent

logger = logging.getLogger(__name__)

FAILURES_PATH = "onboarding_failures.json"


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

async def run() -> dict[str, int]:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    logger.info("=== Customer Onboarding Pipeline started ===")

    ingestor = S3Ingestor()
    parser = LLMParser()
    writer = CRMWriter()
    corrector = CorrectionAgent()
    validator_retrier = ValidationRetryAgent()

    stats = {"processed": 0, "succeeded": 0, "failed": 0, "skipped": 0}
    failures: list[dict] = []

    # ── Step 1: Ingest all S3 objects ─────────────────────────────────────
    s3_objects, failed_downloads = await ingestor.fetch_all()
    for key, error in failed_downloads:
        failures.append(_failure_entry(key, "s3_download", error))
        stats["processed"] += 1
        stats["failed"] += 1

    # ── Step 2: Parse all concurrently ────────────────────────────────────
    parse_results = await asyncio.gather(
        *[parser.parse(obj.content, obj.key) for obj in s3_objects],
        return_exceptions=True,
    )

    # ── Step 3: Validate ──────────────────────────────────────────────────
    valid_records = []

    for obj, result in zip(s3_objects, parse_results):
        stats["processed"] += 1

        if isinstance(result, openai.RateLimitError):
            logger.error("OpenAI rate-limit for %s: %s", obj.key, result)
            failures.append(_failure_entry(obj.key, "llm_parse", str(result)))
            stats["failed"] += 1

        elif isinstance(result, Exception):
            logger.error("Parse error for %s: %s", obj.key, result)
            failures.append(_failure_entry(obj.key, "llm_parse", str(result)))
            stats["failed"] += 1

        elif result is None:
            # Targeted retry: ask LLM specifically to find name + email (up to 2 attempts)
            retried = None
            for attempt in range(1, 3):
                try:
                    retried = await validator_retrier.recover_missing_fields(
                        obj.content, obj.key, previous_attempt_failed=(attempt > 1)
                    )
                except Exception as exc:
                    logger.error("ValidationRetryAgent failed for %s (attempt %d): %s", obj.key, attempt, exc)
                    break
                if retried:
                    logger.info("Validation retry succeeded for %s (attempt %d)", obj.key, attempt)
                    break
            if retried:
                valid_records.append((obj.key, retried))
            else:
                logger.warning("No extractable customer data in %s — skipping.", obj.key)
                stats["skipped"] += 1

        else:
            valid_records.append((obj.key, result))

    # ── Step 4: CRM writes (sequential — legacy API) ──────────────────────
    for source_key, record in valid_records:
        try:
            writer.create_customer(record)
            logger.info("Onboarded: %s (%s)", record.name, record.email)
            stats["succeeded"] += 1

        except CRMClientError as exc:
            current_record, current_error = record, str(exc)
            for attempt in range(1, 5):
                logger.warning("CRM rejected record for %s — attempting correction (attempt %d): %s", record.email, attempt, current_error)
                try:
                    corrected = await corrector.correct(current_record, current_error)
                except Exception as agent_exc:
                    logger.error("CorrectionAgent failed for %s (attempt %d): %s", record.email, attempt, agent_exc)
                    failures.append(_failure_entry(source_key, "crm_correction_agent", str(agent_exc), current_record))
                    stats["failed"] += 1
                    break
                if not corrected:
                    logger.error("CorrectionAgent could not fix record for %s", record.email)
                    failures.append(_failure_entry(source_key, "crm_client_error", current_error, current_record))
                    stats["failed"] += 1
                    break
                try:
                    writer.create_customer(corrected)
                    logger.info("Onboarded (after correction attempt %d): %s (%s)", attempt, corrected.name, corrected.email)
                    stats["succeeded"] += 1
                    break
                except CRMClientError as new_exc:
                    current_record, current_error = corrected, str(new_exc)
                except Exception as retry_exc:
                    logger.error("CRM write failed after correction attempt %d for %s: %s", attempt, record.email, retry_exc)
                    failures.append(_failure_entry(source_key, "crm_correction_retry", str(retry_exc), corrected))
                    stats["failed"] += 1
                    break
            else:
                # All 4 correction attempts were rejected by CRM
                failures.append(_failure_entry(source_key, "crm_client_error", current_error, current_record))
                stats["failed"] += 1

        except Exception as exc:
            logger.error("CRM write failed for %s after all retries: %s", record.email, exc)
            failures.append(_failure_entry(source_key, "crm_write", str(exc), record))
            stats["failed"] += 1

    # ── Write failure file ────────────────────────────────────────────────
    if failures:
        failures_doc = {
            "run_at": datetime.now(timezone.utc).isoformat(),
            "total": len(failures),
            "items": failures,
        }
        with open(FAILURES_PATH, "w", encoding="utf-8") as fh:
            json.dump(failures_doc, fh, indent=2, default=str)
        logger.warning(
            "Onboarding failures: %d item(s) written to %s",
            len(failures),
            FAILURES_PATH,
        )

    logger.info(
        "=== Pipeline complete: processed=%d succeeded=%d failed=%d skipped=%d ===",
        stats["processed"],
        stats["succeeded"],
        stats["failed"],
        stats["skipped"],
    )
    return stats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _failure_entry(key: str, stage: str, error: str, record=None) -> dict:
    entry: dict = {"source_key": key, "stage": stage, "error": error}
    if record is not None:
        entry["record"] = record.model_dump()
    return entry


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    result = asyncio.run(run())
    if result["failed"] > 0:
        sys.exit(1)
