from datetime import datetime
from sqlalchemy import Column, String, Text, Float, DateTime, Index, UniqueConstraint

from .database import Base


class ExtractionRequest(Base):
    __tablename__ = "extraction_requests"

    # Use application-level request ids (e.g., "req_xxx") for clarity in API
    id = Column(String(64), primary_key=True, index=True)

    idempotency_key = Column(String(255), unique=True, nullable=False, index=True)
    status = Column(String(32), nullable=False, index=True)  # PENDING | COMPLETED | FAILED

    document_text = Column(Text, nullable=False)

    # Result fields (nullable until COMPLETED)
    doc_type = Column(String(32), nullable=True)  # invoice | receipt | unknown
    invoice_number = Column(String(128), nullable=True)
    invoice_date = Column(String(32), nullable=True)  # YYYY-MM-DD
    total_amount = Column(Float, nullable=True)
    currency = Column(String(8), nullable=True)  # 3-letter code

    # Error fields (nullable; set when FAILED)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_extraction_idempotency_key"),
        Index("ix_status_created_at", "status", "created_at"),
    )


