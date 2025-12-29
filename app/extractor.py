import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional, Tuple


@dataclass
class ExtractorFailure(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

SUPPORTED_CURRENCY_CODES = {
    "USD",
    "EUR",
    "GBP",
    "AUD",
    "CAD",
    "CHF",
    "CNY",
    "INR",
    "JPY",
    "NZD",
}


def _normalize_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _detect_doc_type(text: str) -> str:
    upper = text.upper()
    if "INVOICE" in upper:
        return "invoice"
    if "RECEIPT" in upper:
        return "receipt"
    return "unknown"


def _extract_invoice_number(text: str) -> Optional[str]:
    patterns = [
        r"Invoice\s*Number[:#]?\s*([A-Za-z0-9\-_/]+)",
        r"Invoice\s*#[:\s]*([A-Za-z0-9\-_/]+)",
        r"Invoice[:\s]+([A-Za-z0-9\-_/]+)",
        r"Transaction\s*#[:\s]*([A-Za-z0-9\-_/]+)",
        r"Transaction\s*Number[:#]?\s*([A-Za-z0-9\-_/]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def _extract_date_iso(text: str) -> Optional[str]:
    # Direct ISO 8601 date
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # Month name formats like "December 15, 2024" or "Dec 15, 2024"
    m2 = re.search(r"\b([A-Za-z]{3,9})\s+(\d{1,2}),\s*(\d{4})\b", text)
    if m2:
        month_name = m2.group(1).lower()
        month_num = MONTHS.get(month_name)
        if month_num:
            day = int(m2.group(2))
            year = int(m2.group(3))
            try:
                dt = datetime(year, month_num, day)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                return None
    return None


def _extract_currency_and_amount(text: str) -> Tuple[Optional[str], Optional[float]]:
    """
    Heuristic:
    - Prefer totals lines: TOTAL, Grand Total, Total Paid
    - Currency:
        - If explicit 3-letter code (e.g., USD, EUR) anywhere on the candidate line → use it
        - Else if symbol is '€' → EUR
        - Else if symbol is '$' → USD (defaulting for '$' to USD)
    """
    normalized_lines = _normalize_text(text).split("\n")

    total_pattern = re.compile(r"\b(TOTAL|Grand\s+Total|Total\s+Paid)\b", re.IGNORECASE)
    symbol_pattern = re.compile(r"[\$€£]")
    code_pattern = re.compile(r"\b(USD|EUR|GBP|AUD|CAD|CHF|CNY|INR|JPY|NZD)\b")

    totals = [line for line in normalized_lines if total_pattern.search(line)]
    totals_with_currency = [
        line for line in totals if symbol_pattern.search(line) or code_pattern.search(line)
    ]

    # Build candidates in priority:
    # 1) totals that include a currency hint
    # 2) any totals
    # 3) any line with a currency symbol
    # 4) any line with a currency code
    candidates: list[str] = []
    for group in [totals_with_currency or totals,
                  [l for l in normalized_lines if symbol_pattern.search(l)],
                  [l for l in normalized_lines if code_pattern.search(l)]]:
        for line in group:
            if line not in candidates:
                candidates.append(line)

    # If the document has no currency hints at all, only then fall back to any amount-like number lines
    has_any_currency_hint = any(symbol_pattern.search(l) or code_pattern.search(l) for l in normalized_lines)

    # Prefer amounts directly attached to a currency symbol to avoid matching IDs/dates
    symbol_amount_regex = re.compile(
        r"([\$€£])\s*([0-9]{1,3}(?:[,.\s][0-9]{3})*(?:[.,][0-9]{2})|[0-9]+(?:[.,][0-9]{2})?)"
    )
    # Fallback: any amount-looking number (may incorrectly match IDs/dates; used only if no symbol match)
    amount_regex = re.compile(
        r"([\$€£])?\s*([0-9]{1,3}(?:[,.\s][0-9]{3})*(?:[.,][0-9]{2})|[0-9]+(?:[.,][0-9]{2})?)"
    )

    if not has_any_currency_hint:
        for line in normalized_lines:
            if amount_regex.search(line) and line not in candidates:
                candidates.append(line)

    parsed_candidates: list[Tuple[float, Optional[str]]] = []

    for cand in candidates:
        # Determine any explicit currency code on the line (validated against whitelist)
        code_match = code_pattern.search(cand)
        line_code: Optional[str] = None
        if code_match and code_match.group(1) in SUPPORTED_CURRENCY_CODES:
            line_code = code_match.group(1)

        # Collect all symbol-anchored amounts on this line
        for m in symbol_amount_regex.finditer(cand):
            symbol = m.group(1)
            raw_number = m.group(2)
            normalized = raw_number.replace(" ", "").replace(",", "")
            if normalized.count(".") > 1 and "," not in normalized:
                pass
            if "," in raw_number and "." in raw_number:
                if raw_number.rfind(",") > raw_number.rfind("."):
                    normalized = raw_number.replace(".", "").replace(",", ".")
            try:
                amount_val = float(normalized)
            except Exception:
                continue
            currency_for_amount: Optional[str] = line_code
            if currency_for_amount is None:
                if symbol == "€":
                    currency_for_amount = "EUR"
                elif symbol == "$":
                    currency_for_amount = "USD"
                elif symbol == "£":
                    currency_for_amount = "GBP"
            parsed_candidates.append((amount_val, currency_for_amount))

        # If none symbol-anchored found, consider generic amounts where either the match has a symbol
        # or we have a line-level code to attribute currency
        if not symbol_amount_regex.search(cand):
            for m in amount_regex.finditer(cand):
                symbol_opt = m.group(1)
                raw_number = m.group(2)
                normalized = raw_number.replace(" ", "").replace(",", "")
                if normalized.count(".") > 1 and "," not in normalized:
                    pass
                if "," in raw_number and "." in raw_number:
                    if raw_number.rfind(",") > raw_number.rfind("."):
                        normalized = raw_number.replace(".", "").replace(",", ".")
                try:
                    amount_val = float(normalized)
                except Exception:
                    continue
                currency_for_amount: Optional[str] = None
                if symbol_opt:
                    if symbol_opt == "€":
                        currency_for_amount = "EUR"
                    elif symbol_opt == "$":
                        currency_for_amount = "USD"
                    elif symbol_opt == "£":
                        currency_for_amount = "GBP"
                elif line_code:
                    currency_for_amount = line_code
                parsed_candidates.append((amount_val, currency_for_amount))

    if not parsed_candidates:
        return None, None

    # Choose the highest amount across all candidates
    best_amount, best_currency = max(parsed_candidates, key=lambda x: x[0])
    return best_currency, best_amount


def extract_from_text(document_text: str) -> Dict[str, Optional[object]]:
    if "<<TRIGGER_EXTRACTOR_FAILURE>>" in document_text:
        # Simulated failure path per spec/test-cases
        raise ExtractorFailure(
            code="EXTRACTOR_TIMEOUT",
            message="Extraction process timed out after 30 seconds",
        )

    doc_type = _detect_doc_type(document_text)
    invoice_number = _extract_invoice_number(document_text)
    invoice_date = _extract_date_iso(document_text)
    currency, total_amount = _extract_currency_and_amount(document_text)

    return {
        "doc_type": doc_type,
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "total_amount": total_amount,
        "currency": currency,
    }


