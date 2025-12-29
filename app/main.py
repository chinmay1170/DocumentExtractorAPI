import os
import time
import logging
import uuid
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import Base, engine, get_session
from .models import ExtractionRequest
from .schemas import (
    ExtractRequestBody,
    ExtractPostResponse,
    ExtractGetResponse,
    ExtractionResult,
    ExtractionError,
)
from .worker import worker

_log_level = os.getenv("APP_LOG_LEVEL", "INFO").upper()
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=_log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
else:
    logging.getLogger().setLevel(_log_level)

logger = logging.getLogger("app.main")
logger.setLevel(_log_level)

app = FastAPI(
    title="Idempotent Extraction API",
    version="1.0.0",
    description="Accepts documents, extracts structured data, and returns results idempotently.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _generate_request_id() -> str:
    return "req_" + uuid.uuid4().hex[:12]


@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)
    logger.info("Application startup: database tables ensured, starting worker")
    worker.start()


@app.on_event("shutdown")
def on_shutdown() -> None:
    logger.info("Application shutdown: stopping worker")
    worker.stop()


@app.post("/extract", response_model=ExtractPostResponse, tags=["Extraction"])
def submit_extraction(body: ExtractRequestBody) -> ExtractPostResponse:
    # Idempotency: if the idempotency_key exists, return existing id and status
    with get_session() as session:
        logger.info("Submit extraction received for idempotency_key=%s", body.idempotency_key)
        existing = (
            session.query(ExtractionRequest)
            .filter(ExtractionRequest.idempotency_key == body.idempotency_key)
            .one_or_none()
        )
        if existing:
            logger.info(
                "Idempotent hit: returning existing request_id=%s status=%s for idempotency_key=%s",
                existing.id,
                existing.status,
                body.idempotency_key,
            )
            return ExtractPostResponse(request_id=existing.id, status=existing.status)

        req_id = _generate_request_id()
        new_req = ExtractionRequest(
            id=req_id,
            idempotency_key=body.idempotency_key,
            status="PENDING",
            document_text=body.document_text,
        )
        session.add(new_req)
        try:
            session.flush()
            logger.info(
                "Created new extraction request_id=%s for idempotency_key=%s",
                req_id,
                body.idempotency_key,
            )
        except IntegrityError:
            # In rare race conditions, return the winner's row
            session.rollback()
            existing_again = (
                session.query(ExtractionRequest)
                .filter(ExtractionRequest.idempotency_key == body.idempotency_key)
                .one_or_none()
            )
            if existing_again:
                logger.info(
                    "Race detected; returning winner request_id=%s status=%s for idempotency_key=%s",
                    existing_again.id,
                    existing_again.status,
                    body.idempotency_key,
                )
                return ExtractPostResponse(request_id=existing_again.id, status=existing_again.status)
            raise

    # enqueue after commit
    worker.enqueue(req_id)
    logger.info("Enqueued request_id=%s for processing", req_id)
    return ExtractPostResponse(request_id=req_id, status="PENDING")


@app.get("/extract/{request_id}", response_model=ExtractGetResponse, tags=["Extraction"])
def get_extraction(request_id: str) -> ExtractGetResponse:
    with get_session() as session:
        logger.info("Fetch extraction status for request_id=%s", request_id)
        req = session.get(ExtractionRequest, request_id)
        if req is None:
            logger.info("Request not found for request_id=%s", request_id)
            raise HTTPException(status_code=404, detail="Request not found")
        if req.status == "COMPLETED":
            logger.info("Request_id=%s status=COMPLETED returning result", request_id)
            result = ExtractionResult(
                doc_type=req.doc_type or "unknown",
                invoice_number=req.invoice_number,
                invoice_date=req.invoice_date,
                total_amount=req.total_amount,
                currency=req.currency,
            )
            return ExtractGetResponse(request_id=req.id, status=req.status, result=result, error=None)
        if req.status == "FAILED":
            logger.info(
                "Request_id=%s status=FAILED code=%s", request_id, req.error_code or "UNKNOWN_ERROR"
            )
            error = ExtractionError(code=req.error_code or "UNKNOWN_ERROR", message=req.error_message or "")
            return ExtractGetResponse(request_id=req.id, status=req.status, result=None, error=error)
    
        max_attempts = int(os.getenv("GET_POLL_ATTEMPTS", "3"))
        delay_seconds = float(os.getenv("GET_POLL_DELAY_SECONDS", "1.0"))
        if req.status == "PENDING" and max_attempts > 0:
            logger.info(
                "Request_id=%s status=PENDING; polling up to %s attempts every %ss",
                request_id,
                max_attempts,
                delay_seconds,
            )
        for _ in range(max_attempts):
            time.sleep(delay_seconds)
            session.refresh(req)
            if req.status == "COMPLETED":
                logger.info("Request_id=%s became COMPLETED during polling", request_id)
                result = ExtractionResult(
                    doc_type=req.doc_type or "unknown",
                    invoice_number=req.invoice_number,
                    invoice_date=req.invoice_date,
                    total_amount=req.total_amount,
                    currency=req.currency,
                )
                return ExtractGetResponse(request_id=req.id, status=req.status, result=result, error=None)
            if req.status == "FAILED":
                logger.info(
                    "Request_id=%s became FAILED during polling code=%s",
                    request_id,
                    req.error_code or "UNKNOWN_ERROR",
                )
                error = ExtractionError(code=req.error_code or "UNKNOWN_ERROR", message=req.error_message or "")
                return ExtractGetResponse(request_id=req.id, status=req.status, result=None, error=error)
        if req.status == "PENDING":
            logger.info(
                "Request_id=%s still PENDING after %s attempts; returning PENDING",
                request_id,
                max_attempts,
            )
        return ExtractGetResponse(request_id=req.id, status=req.status, result=None, error=None)


@app.get("/", tags=["Meta"])
def root() -> dict:
    return {
        "service": "Idempotent Extraction API",
        "endpoints": ["/extract [POST]", "/extract/{request_id} [GET]"],
        "persistence": "sqlite",
        "processing": "asynchronous with worker queue",
    }


