## Idempotent Extraction API

A small FastAPI service that accepts documents, extracts structured fields, and returns results idempotently. Persistence is backed by SQLite so data survives server restarts. Processing is asynchronous via an in-process queue and worker thread.

## Setup Instructions

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --reload
```

Open the interactive docs at `http://127.0.0.1:8000/docs`.

### Optional: Enable free local LLM (Ollama + llama3)

```bash
# macOS install
brew install ollama

# start server (in foreground)
ollama serve

# pull a small, free model
ollama pull llama3

# run the API using the LLM backend
export EXTRACTOR_BACKEND=llm
export LLM_PROVIDER=ollama
export OLLAMA_MODEL=llama3
export OLLAMA_BASE_URL=http://localhost:11434
uvicorn app.main:app --reload
```

If you prefer regex-only (default), skip setting `EXTRACTOR_BACKEND` or set it to `regex`.

### Configuration (environment variables)

- EXTRACTOR_BACKEND: `regex` (default) or `llm`
- APP_LOG_LEVEL: `INFO` (default), `DEBUG`, etc.
- GET_POLL_ATTEMPTS: short server-side polling attempts for GET PENDING (default: `3`)
- GET_POLL_DELAY_SECONDS: delay between GET polls (default: `1.0`)
- WORKER_MAX_RETRIES: max retries on any exception (default: `3`)
- WORKER_TASK_TIMEOUT_SECONDS: per-task timeout (default: `60`)
- LLM_PROVIDER: `ollama` (default) or `openai`
- OLLAMA_BASE_URL: Ollama server base URL (default: `http://localhost:11434`)
- OLLAMA_MODEL: model name (default: `llama3`)
- OPENAI_API_KEY: required if using `LLM_PROVIDER=openai`
- OPENAI_MODEL: OpenAI model (default: `gpt-4o-mini`)

## Architecture Overview

- **FastAPI app (`app/main.py`)**: Hosts endpoints and configures logging/CORS. Starts and stops the worker on app lifecycle.
- **Worker (`app/worker.py`)**: In-process thread with an internal `queue.Queue`. Pulls `request_id`s and performs extraction. Retries and timeouts are enforced here.
- **Database (`app/database.py`, `app/models.py`)**: SQLAlchemy ORM with SQLite stored at `data/extractions.db`. `ExtractionRequest` is the single table.
- **Extractors**:
  - Regex/heuristics (`app/extractor.py`): Deterministic, low-latency.
  - LLM (`app/llm_extractor.py`): Optional. Uses LangChain with Ollama (local llama3) or OpenAI. When LLM is enabled, results are merged with regex per-field for robustness.
- **Idempotency**: Unique constraint on `idempotency_key` ensures POST is idempotent even under concurrency.
- **Logging**: Console logging with configurable level via `APP_LOG_LEVEL`.

## Data Model

- `ExtractionRequest`
  - `id` (string PK, e.g., `req_abcd1234`)
  - `idempotency_key` (unique)
  - `status` (`PENDING` | `COMPLETED` | `FAILED`)
  - `document_text` (original input)
  - Result fields: `doc_type`, `invoice_number`, `invoice_date`, `total_amount`, `currency`
  - Error fields: `error_code`, `error_message`
  - Timestamps: `created_at`, `updated_at`

## API Specification

- POST `/extract`
  - Body:
    ```json
    {
      "idempotency_key": "abc-123",
      "document_text": "INVOICE ... TOTAL: $2,180.00 USD ..."
    }
    ```
  - Response:
    ```json
    { "request_id": "req_xxx", "status": "PENDING" }
    ```
  - Idempotency: Re-submitting the same `idempotency_key` always returns the same `request_id` and current `status` without enqueueing duplicate work.

- GET `/extract/{request_id}`
  - Response (COMPLETED):
    ```json
    {
      "request_id": "req_xxx",
      "status": "COMPLETED",
      "result": {
        "doc_type": "invoice",
        "invoice_number": "ACME-2024-5678",
        "invoice_date": "2024-12-15",
        "total_amount": 2180.0,
        "currency": "USD"
      },
      "error": null
    }
    ```
  - Response (FAILED):
    ```json
    {
      "request_id": "req_xxx",
      "status": "FAILED",
      "result": null,
      "error": { "code": "EXTRACTOR_TIMEOUT", "message": "Extraction process timed out after 30 seconds" }
    }
    ```
  - Response (PENDING):
    ```json
    { "request_id": "req_xxx", "status": "PENDING", "result": null, "error": null }
    ```
  - Pending server-side polling: If status is `PENDING`, the server will short-poll the DB for a few attempts (config via `GET_POLL_ATTEMPTS`, `GET_POLL_DELAY_SECONDS`) before returning `PENDING`.
  - 404 when `request_id` is not found.

### Extractor behavior (regex + LLM)

- Regex/heuristics:
  - `doc_type`: keywords (INVOICE/RECEIPT).
  - `invoice_number`: common label variants (`Invoice Number`, `Invoice #`, `Invoice:`, `Transaction #`).
  - `invoice_date`: ISO `YYYY-MM-DD` or month-name formats (normalized to ISO).
  - `currency/total_amount`:
    - Prioritize lines mentioning totals (TOTAL, Grand Total, Total Paid).
    - Prefer symbol-anchored amounts (`$120.00`, `€1.234,56`, `£999.99`).
    - Recognize 3-letter currency codes; map symbols: `$→USD`, `€→EUR`, `£→GBP`.
    - When multiple amounts appear, pick the highest.
  - Failure trigger: if the text contains `<<TRIGGER_EXTRACTOR_FAILURE>>`, it raises a failure (used by tests).
- LLM extractor (optional):
  - Uses LangChain with Ollama (llama3) by default or OpenAI if configured.
  - The prompt enforces strict JSON. A best-effort JSON recovery is used if the model returns extra text.
  - Results are merged per-field with regex (LLM value if present, else regex), ensuring robustness.

## Failure & Retry Policy

- Worker retries any exception (including timeouts) up to `WORKER_MAX_RETRIES` (default 3). While retrying, the DB row remains `PENDING`.
- Per-message timeout: each extraction attempt is limited by `WORKER_TASK_TIMEOUT_SECONDS` (default 60). On timeout, a retry is scheduled.
- After max retries are exhausted, the row is marked `FAILED` with:
  - `error_code=EXTRACTOR_TIMEOUT` for timeouts
  - `error_code` from the extractor if it raised a known failure
  - `error_code=EXTRACTOR_ERROR` for unexpected exceptions

## Example cURL

```bash
# Submit for extraction (asynchronous)
curl -s -X POST http://127.0.0.1:8000/extract \
  -H "Content-Type: application/json" \
  -d '{"idempotency_key":"test-multiline-001","document_text":"INVOICE\n\nInvoice Number: ACME-2024-5678\nDate: December 15, 2024\n\nSubtotal: $2,000.00\nTax: $180.00\nTOTAL: $2,180.00 USD"}'

# Check status
curl -s http://127.0.0.1:8000/extract/req_abcdef123456
```

## Design Rationale

- **Strict idempotency**: Unique key ensures exactly-once logical create behavior for `POST /extract`.
- **Async processing**: Keeps request latency low and isolates extraction failures.
- **Retry + timeout**: Balances resilience with bounded latency. Retries are limited and timeouts prevent stuck tasks.
- **Regex + LLM merge**: Deterministic baseline with optional semantic booster; per-field merge yields better coverage than either alone.
- **SQLite**: Simplicity and portability for local assessment.

## Trade-offs

- In-process queue is not horizontally scalable; a real system would use a durable queue (e.g., Redis, SQS) and worker processes.
- SQLite has limited concurrency; a production DB (e.g., Postgres) would be preferable.
- LLM extraction is non-deterministic and slower; we default to regex for performance and predictability.
- Per-process retry counts are in-memory; a DB-backed attempt counter would persist across restarts.

### Development Workflow (optional)

- Typical loop:
  - Run locally with `uvicorn ... --reload`
  - Submit docs via cURL or Swagger UI
  - Observe logs (`APP_LOG_LEVEL=INFO|DEBUG`)
- Coding assistant tooling was used to:
  - scaffold API and worker
  - iteratively refine regex heuristics and LLM integration
  - implement retries, timeouts, and logging

### References

- Assessment PDF: [`Coding Assessment - Idempotent Extraction API.pdf`](file:///Users/chinmaykrishna/Documents/Personal/Chinmay/TestProject/Coding%20Assessment%20-%20Idempotent%20Extraction%20API.pdf)

