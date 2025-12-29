from typing import Optional, Literal
from pydantic import BaseModel, Field


class ExtractRequestBody(BaseModel):
    idempotency_key: str = Field(..., min_length=1, max_length=255)
    document_text: str = Field(..., min_length=1)


StatusLiteral = Literal["PENDING", "COMPLETED", "FAILED"]
DocTypeLiteral = Literal["invoice", "receipt", "unknown"]


class ExtractPostResponse(BaseModel):
    request_id: str
    status: StatusLiteral


class ExtractionResult(BaseModel):
    doc_type: DocTypeLiteral
    invoice_number: Optional[str]
    invoice_date: Optional[str]
    total_amount: Optional[float]
    currency: Optional[str]


class ExtractionError(BaseModel):
    code: str
    message: str


class ExtractGetResponse(BaseModel):
    request_id: str
    status: StatusLiteral
    result: Optional[ExtractionResult]
    error: Optional[ExtractionError]


