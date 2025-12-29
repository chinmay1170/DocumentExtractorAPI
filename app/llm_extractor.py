import json
import os
from typing import Dict, Optional

# Optional LangChain imports (lazy; module is safe to import without LangChain installed)
_lc_import_error: Optional[Exception] = None
try:
    from pydantic import BaseModel, Field
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.output_parsers import JsonOutputParser
    from langchain_openai import ChatOpenAI  # pip install langchain-openai
    try:
        # Prefer the dedicated package if available
        from langchain_ollama import ChatOllama  # pip install langchain-ollama
    except Exception:
        # Fallback to community (older) if present
        from langchain_community.chat_models import ChatOllama  # type: ignore
except Exception as _e:  # pragma: no cover - import-time guard
    _lc_import_error = _e
    BaseModel = object  # type: ignore
    ChatPromptTemplate = object  # type: ignore
    JsonOutputParser = object  # type: ignore
    ChatOpenAI = object  # type: ignore
    ChatOllama = object  # type: ignore


class LLMInvoiceFields(BaseModel):  # type: ignore[misc, valid-type]
    doc_type: Optional[str] = Field(
        None,
        description="The detected document type. One of: invoice, receipt, unknown.",
    )
    invoice_number: Optional[str] = Field(
        None, description="The invoice or transaction number if present."
    )
    invoice_date: Optional[str] = Field(
        None,
        description="The date in ISO format YYYY-MM-DD when possible.",
    )
    total_amount: Optional[float] = Field(
        None, description="The total payable amount as a float."
    )
    currency: Optional[str] = Field(
        None, description="Three-letter currency code (e.g., USD, EUR, GBP)."
    )


def _best_effort_parse_json(text: str) -> Optional[Dict[str, object]]:
    """
    Try to parse JSON out of arbitrary text:
      - direct json
      - fenced ```json ... ```
      - substring between first '{' and last '}'
    """
    if not isinstance(text, str):
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    if "```" in text:
        try:
            fence_start = text.find("```")
            fence_end = text.rfind("```")
            if fence_end > fence_start:
                fenced = text[fence_start + 3 : fence_end].strip()
                if "\n" in fenced and not fenced.lstrip().startswith("{"):
                    first_newline = fenced.find("\n")
                    fenced = fenced[first_newline + 1 :]
                return json.loads(fenced)
        except Exception:
            pass
    try:
        l = text.find("{")
        r = text.rfind("}")
        if l != -1 and r != -1 and r > l:
            return json.loads(text[l : r + 1])
    except Exception:
        pass
    return None

def _build_prompt() -> JsonOutputParser:
    """
    Create a structured output parser and prompt template instructing the model
    to produce strict JSON matching LLMInvoiceFields.
    """
    parser = JsonOutputParser(pydantic_object=LLMInvoiceFields)  # type: ignore[arg-type]
    format_instructions = parser.get_format_instructions()
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "You are an extraction assistant. Extract structured fields from the provided document text.",
            ),
            (
                "user",
                "Document text:\n\n{document_text}\n\n"
                "Return ONLY valid JSON following these instructions, with no extra text before or after:\n{format_instructions}",
            ),
        ]
    ).partial(format_instructions=format_instructions)
    return parser, prompt


def _get_llm():
    """
    Create a Chat model based on environment configuration.
    Supported:
      - OpenAI via langchain-openai (LLM_PROVIDER=openai, OPENAI_API_KEY, OPENAI_MODEL)
      - Ollama via langchain-ollama or community (LLM_PROVIDER=ollama, OLLAMA_MODEL, OLLAMA_BASE_URL)
    """
    provider = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set")
        model_name = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        temperature = float(os.getenv("OPENAI_TEMPERATURE", "0"))
        return ChatOpenAI(model=model_name, api_key=api_key, temperature=temperature)  # type: ignore[call-arg]
    if provider == "ollama":
        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        model_name = os.getenv("OLLAMA_MODEL", "llama3")
        temperature = float(os.getenv("OLLAMA_TEMPERATURE", "0"))
        return ChatOllama(model=model_name, base_url=base_url, temperature=temperature, format="json")  # type: ignore[call-arg]
    raise RuntimeError(f"Unsupported LLM_PROVIDER '{provider}'")


def extract_with_llm(document_text: str) -> Dict[str, Optional[object]]:
    """
    Use a LangChain chat model to parse document_text into the target schema.

    Returns dict with keys:
      - doc_type, invoice_number, invoice_date, total_amount, currency

    Environment configuration:
      - LLM_PROVIDER=openai|ollama
      - OPENAI_API_KEY, OPENAI_MODEL (for openai)
      - OLLAMA_BASE_URL, OLLAMA_MODEL (for ollama)
    """
    if _lc_import_error is not None:
        raise RuntimeError(
            "LangChain-based extraction requested but dependencies are missing. "
            "Install: pip install langchain langchain-core langchain-openai langchain-ollama pydantic\n"
            f"Underlying import error: {_lc_import_error}"
        )

    parser, prompt = _build_prompt()
    llm = _get_llm()
    chain = prompt | llm | parser  # type: ignore[operator]

    try:
        result: LLMInvoiceFields = chain.invoke({"document_text": document_text})  # type: ignore[assignment]
        # Convert to the API's expected dict
        return {
            "doc_type": getattr(result, "doc_type", None),
            "invoice_number": getattr(result, "invoice_number", None),
            "invoice_date": getattr(result, "invoice_date", None),
            "total_amount": getattr(result, "total_amount", None),
            "currency": getattr(result, "currency", None),
        }
    except Exception as ex:
        # Attempt lenient JSON parse if the model returned raw text not parsed by the JsonOutputParser
        try:
            raw = (prompt | llm).invoke({"document_text": document_text})  # type: ignore[operator]
            content = getattr(raw, "content", None)
            if isinstance(content, str):
                parsed = _best_effort_parse_json(content)
                if not isinstance(parsed, dict):
                    raise ValueError("Unable to parse JSON from model output")
                # Ensure only expected keys are returned
                return {
                    "doc_type": parsed.get("doc_type"),
                    "invoice_number": parsed.get("invoice_number"),
                    "invoice_date": parsed.get("invoice_date"),
                    "total_amount": parsed.get("total_amount"),
                    "currency": parsed.get("currency"),
                }
        except Exception:
            pass
        # Final fallback: return empty fields; caller may merge with regex extractor
        return {
            "doc_type": None,
            "invoice_number": None,
            "invoice_date": None,
            "total_amount": None,
            "currency": None,
        }


