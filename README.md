# Enterprise Customer Onboarding Pipeline

An AI agent-driven pipeline that ingests unstructured customer data from AWS S3, parses it with GPT-4o-mini, and writes structured records to a legacy CRM REST API with full retry/resilience handling and agentic recovery loops.

---

## Architecture Diagram > https://www.canva.com/design/DAHJ7ImWqTI/nnYNNgD1BIWpFAK4N9vVHQ/edit?ui=e30

---

## Data Flow

```
1. S3Ingestor.fetch_all()
   └── paginate ListObjectsV2 → download all concurrently (aioboto3)
   └── download failures → returned as (key, error) list → onboarding_failures.json

2. LLMParser.parse(content, source_file)
   └── send raw content to gpt-4o-mini with structured extraction prompt
   └── response_format=CustomerOnboardingRecord (Pydantic, structured output)
   └── rate-limit errors → retry ×5 with exponential backoff + Retry-After header
   └── exhausted retries / API error → onboarding_failures.json

3. ValidationRetryAgent.recover_missing_fields(content, source_file)
   └── fires when name or email is missing after LLMParser
   └── targeted prompt: "look specifically for name/email in signatures, headers, footers"
   └── ×2 attempts; 2nd attempt tells model a prior targeted retry also failed
   └── found → valid_records | still missing → skipped

4. CRMWriter.create_customer(record)
   └── POST /api/v1/contacts
         • 429 → honour Retry-After header → retry (up to MAX_RETRIES=5)
         • 5xx / network → exponential backoff + jitter → retry (up to MAX_RETRIES=5)
         • 4xx → raise CRMClientError immediately → CorrectionAgent

5. CorrectionAgent.correct(record, crm_error)
   └── fires when CRM returns a 4xx rejection
   └── LLM receives the rejected record + CRM error message
   └── fixes only what's needed to resolve the error
   └── ×4 rounds; each round feeds the new CRM error back in
   └── fixed → CRMWriter retry | unfixable → onboarding_failures.json

6. Outcomes
   └── Success  → stats["succeeded"]++, logged
   └── Skipped  → stats["skipped"]++  (no name/email after all retries)
   └── Failed   → stats["failed"]++, appended to onboarding_failures.json
```

---

## Project Structure

```
.
├── main.py
│
└── agents/
    ├── schemas.py                 # CustomerOnboardingRecord (Pydantic)
    ├── prompts.py                 # SYSTEM_PROMPT with few-shot examples
    ├── s3_ingestor.py             # AWS S3 list + concurrent download
    ├── llm_parser.py              # GPT-4o-mini broad extraction + rate-limit retry
    ├── validation_retry_agent.py  # RECOVERY AGENT for Targeted name/email
    ├── correction_agent.py        # CORRECTION AGENT for CRM rejection
    └── crm_writer.py              # Legacy CRM API client with resilience
```

---

## Error Handling & Resilience

- **OpenAI rate limits** — `LLMParser` retries ×5 with exponential backoff, honouring `Retry-After` header if present
- **Missing name/email** — `ValidationRetryAgent` makes ×2 targeted re-attempts; 2nd attempt signals to the model that the first also failed
- **CRM 429** — `CRMWriter` sleeps for `Retry-After` duration, counts as one of 5 attempts
- **CRM 5xx / network** — exponential backoff + jitter, up to 5 attempts
- **CRM 4xx** — `CorrectionAgent` fixes the record using the CRM error message, up to 4 rounds feeding each new error back in

**Failure file** — `onboarding_failures.json`- `source_key` → S3 object key
- `stage` → `s3_download` / `llm_parse` / `crm_client_error` / `crm_correction_agent` / `crm_correction_retry` / `crm_write`
- `error` → error message
- `record` → `CustomerOnboardingRecord` at time of failure (if available)

---

## Design Decisions

**GPT-4o-mini with Structured Outputs** — uses `beta.chat.completions.parse()` with `response_format=CustomerOnboardingRecord`, constraining the model to valid schema-compliant JSON and eliminating parsing failures.

**Two Agentic Feedback Loops** — `ValidationRetryAgent` recovers missing name/email before the CRM write using a narrower prompt; `CorrectionAgent` fixes CRM-rejected records using the actual error message as signal.

**Concurrency Model** — S3 downloads and LLM parses run concurrently (`asyncio.gather`); LLM concurrency is capped at 10 via semaphore to avoid rate limits; CRM writes are sequential since an undocumented legacy API is safest to call one at a time.
