import threading
import queue
from typing import Optional, Dict, Callable
import logging
import os

from sqlalchemy.orm import Session

from .database import get_session
from .models import ExtractionRequest
from .extractor import extract_from_text, ExtractorFailure

logger = logging.getLogger("app.worker")


class ExtractionWorker:
    def __init__(self) -> None:
        self._q: "queue.Queue[str]" = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # regex | llm (default regex)
        self._backend: str = os.getenv("EXTRACTOR_BACKEND", "llm").strip().lower()
        # retry and timeout config
        self._max_retries: int = int(os.getenv("WORKER_MAX_RETRIES", "3"))
        self._task_timeout_seconds: float = float(os.getenv("WORKER_TASK_TIMEOUT_SECONDS", "60"))
        # in-memory attempts tracker (resets on process restart)
        self._attempts: Dict[str, int] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="extraction-worker", daemon=True)
        self._thread.start()
        logger.info("Extraction worker started (backend=%s)", self._backend)

    def stop(self) -> None:
        self._stop_event.set()
        # put a sentinel to unblock queue if waiting
        self._q.put_nowait("__STOP__")
        if self._thread:
            self._thread.join(timeout=2.0)
        logger.info("Extraction worker stopped")

    def enqueue(self, request_id: str) -> None:
        self._q.put_nowait(request_id)
        logger.info("Enqueued request_id=%s", request_id)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                request_id = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if request_id == "__STOP__":
                break
            logger.info("Dequeued request_id=%s for processing", request_id)
            self._process_request(request_id)
            self._q.task_done()

    def _process_request(self, request_id: str) -> None:
        with get_session() as session:
            req: Optional[ExtractionRequest] = session.get(ExtractionRequest, request_id)
            if req is None:
                logger.warning("Request not found in worker for request_id=%s", request_id)
                return
            if req.status != "PENDING":
                logger.info(
                    "Skipping processing for request_id=%s with non-pending status=%s",
                    request_id,
                    req.status,
                )
                return
            try:
                def run_extractor() -> Dict[str, Optional[object]]:
                    if self._backend == "llm":
                        try:
                            from .llm_extractor import extract_with_llm  # lazy import
                            logger.info("Using LLM extractor for request_id=%s", request_id)
                            llm_result = extract_with_llm(req.document_text)
                            # Also compute regex result to merge missing fields
                            regex_result = extract_from_text(req.document_text)
                            merged = {
                                "doc_type": llm_result.get("doc_type") or regex_result.get("doc_type"),
                                "invoice_number": llm_result.get("invoice_number") or regex_result.get("invoice_number"),
                                "invoice_date": llm_result.get("invoice_date") or regex_result.get("invoice_date"),
                                "total_amount": llm_result.get("total_amount") if llm_result.get("total_amount") is not None else regex_result.get("total_amount"),
                                "currency": llm_result.get("currency") or regex_result.get("currency"),
                            }
                            if all(merged.get(k) is None for k in ["doc_type", "invoice_number", "invoice_date", "total_amount", "currency"]):
                                logger.warning("LLM returned no usable fields; falling back to regex for request_id=%s", request_id)
                                return regex_result
                            return merged
                        except Exception as import_or_call_err:
                            logger.warning(
                                "LLM extractor unavailable or failed (%s); falling back to regex for request_id=%s",
                                str(import_or_call_err),
                                request_id,
                            )
                            return extract_from_text(req.document_text)
                    logger.info("Using regex extractor for request_id=%s", request_id)
                    return extract_from_text(req.document_text)

                def execute_with_timeout(func: Callable[[], Dict[str, Optional[object]]], timeout_seconds: float) -> Dict[str, Optional[object]]:
                    result_container: Dict[str, Dict[str, Optional[object]]] = {}
                    error_container: Dict[str, BaseException] = {}

                    def _target() -> None:
                        try:
                            result_container["value"] = func()
                        except BaseException as e:
                            error_container["error"] = e

                    t = threading.Thread(target=_target, name=f"extract-{request_id}", daemon=True)
                    t.start()
                    t.join(timeout_seconds)
                    if t.is_alive():
                        raise TimeoutError(f"Extraction timed out after {timeout_seconds} seconds")
                    if "error" in error_container:
                        raise error_container["error"]
                    return result_container.get("value", {})

                result = execute_with_timeout(run_extractor, self._task_timeout_seconds)
                req.doc_type = result["doc_type"]
                req.invoice_number = result["invoice_number"]
                req.invoice_date = result["invoice_date"]
                req.total_amount = result["total_amount"]
                req.currency = result["currency"]
                req.error_code = None
                req.error_message = None
                req.status = "COMPLETED"
                logger.info("Processing completed for request_id=%s status=COMPLETED", request_id)
            except (ExtractorFailure, TimeoutError, Exception) as ex:
                # Retry policy: up to self._max_retries attempts on any exception
                attempts = self._attempts.get(request_id, 0) + 1
                self._attempts[request_id] = attempts
                if attempts <= self._max_retries:
                    logger.warning(
                        "Attempt %s/%s failed for request_id=%s (%s). Retrying...",
                        attempts,
                        self._max_retries,
                        request_id,
                        ex.__class__.__name__,
                    )
                    # Re-enqueue without modifying DB state (still PENDING)
                    self.enqueue(request_id)
                    return
                # Exceeded retries → mark FAILED with appropriate error code/message
                req.status = "FAILED"
                req.doc_type = None
                req.invoice_number = None
                req.invoice_date = None
                req.total_amount = None
                req.currency = None
                if isinstance(ex, TimeoutError):
                    req.error_code = "EXTRACTOR_TIMEOUT"
                    req.error_message = str(ex)
                    logger.error(
                        "Final failure due to timeout for request_id=%s after %s attempts",
                        request_id,
                        attempts,
                    )
                elif isinstance(ex, ExtractorFailure):
                    req.error_code = ex.code
                    req.error_message = ex.message
                    logger.error(
                        "Final extractor failure for request_id=%s code=%s after %s attempts",
                        request_id,
                        ex.code,
                        attempts,
                    )
                else:
                    req.error_code = "EXTRACTOR_ERROR"
                    req.error_message = str(ex)
                    logger.exception(
                        "Final unexpected error for request_id=%s after %s attempts",
                        request_id,
                        attempts,
                    )
            session.add(req)


# Global singleton worker instance used by the application
worker = ExtractionWorker()


