"""AI metadata extraction for CLM contract uploads."""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any, Optional

from openai import AsyncOpenAI

from app.core.config import settings

logger = logging.getLogger(__name__)

CLM_EXTRACTION_PROMPT = """You are a healthcare contract analyst. Extract structured metadata from the contract text.

OUTPUT JSON ONLY:
{
  "vendor_name": "string — counterparty / vendor legal or trade name (NOT the filename)",
  "effective_date": "YYYY-MM-DD or null",
  "expiration_date": "YYYY-MM-DD or null",
  "renewal_clause": "short summary of renewal/termination terms or null",
  "risk_score": 0-100 integer (higher = more risk for the covered entity),
  "obligations": [
    {
      "title": "string",
      "description": "string",
      "obligation_type": "reporting | policy | audit | certification | insurance | renewal | other",
      "due_date": "YYYY-MM-DD or null",
      "severity": "critical | high | medium | low"
    }
  ]
}

Rules:
- vendor_name must be the counterparty legal/trade name, never a file stem like "01_MedSupply_BAA"
- Prefer explicit labels such as "Vendor", "Business Associate", "Parties", "Vendor Name"
- Use null when dates are not explicit
- Max 10 obligations; only explicit contractual duties
- No markdown outside JSON
"""


def _parse_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    value = value.strip()
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _filename_vendor_fallback(filename: str) -> str:
    vendor = re.sub(r"\.(pdf|docx?|txt|md|csv|html?)$", "", filename or "", flags=re.I)
    vendor = re.sub(r"^[\d]+[_\-\s]+", "", vendor)
    vendor = re.sub(r"[_-]+", " ", vendor).strip()
    return vendor[:255] or "Unknown Vendor"


def _heuristic_extract(text: str, filename: str) -> dict[str, Any]:
    """Structured fallback when OpenAI is unavailable or fails."""
    body = text or ""
    vendor = None
    for pattern in (
        r"(?im)^\s*Vendor\s*(?:Name)?\s*[:\-]\s*(.+)$",
        r"(?im)^\s*Vendor\s*/\s*Counterparty\s*[:\-]\s*(.+)$",
        r"(?im)^\s*Business Associate\s*[:\-]\s*[\"']?([^\"'\n(]+)",
        r"(?im)and\s+([A-Z][\w .,&-]{2,80}?)\s*\(\s*\"?Business Associate\"?\s*\)",
        r"(?im)and\s+([A-Z][\w .,&-]{2,80}?)\s*\(\s*\"?Vendor\"?\s*\)",
        r"(?im)^\s*Parties\s*:\s*.+?\band\s+([A-Z][\w .,&-]{2,80})",
    ):
        match = re.search(pattern, body)
        if match:
            vendor = re.sub(r"\s+", " ", match.group(1)).strip(" .;,\"'")
            if vendor:
                break
    if not vendor:
        vendor = _filename_vendor_fallback(filename)

    effective = None
    expiration = None
    for pattern in (
        r"(?im)^\s*Effective Date\s*[:\-]\s*([0-9]{4}-[0-9]{2}-[0-9]{2})",
        r"(?im)Effective Date\s*:\s*([0-9]{4}-[0-9]{2}-[0-9]{2})",
    ):
        match = re.search(pattern, body)
        if match:
            effective = _parse_date(match.group(1))
            break
    for pattern in (
        r"(?im)^\s*Expiration Date\s*[:\-]\s*([0-9]{4}-[0-9]{2}-[0-9]{2})",
        r"(?im)Expiration Date\s*:\s*([0-9]{4}-[0-9]{2}-[0-9]{2})",
        r"(?im)Expires\s*:\s*([0-9]{4}-[0-9]{2}-[0-9]{2})",
    ):
        match = re.search(pattern, body)
        if match:
            expiration = _parse_date(match.group(1))
            break

    renewal = None
    renewal_match = re.search(
        r"(?is)((?:Auto-)?[Rr]enewal[^\n.]{0,200})",
        body,
    )
    if renewal_match:
        renewal = re.sub(r"\s+", " ", renewal_match.group(1)).strip()[:4000]

    obligations: list[dict[str, Any]] = []
    for match in re.finditer(
        r"(?im)^\s*(?:\d+(?:\.\d+)*\.?|[A-Z]\.|[-*•])\s+(.+)$",
        body,
    ):
        line = match.group(1).strip()
        lower = line.lower()
        if not any(
            token in lower
            for token in (
                "shall",
                "must",
                "provide",
                "maintain",
                "report",
                "notify",
                "implement",
                "complete",
                "train",
                "submit",
            )
        ):
            continue
        if len(line) < 20:
            continue
        obligations.append(
            {
                "title": line[:120],
                "description": line[:2000],
                "obligation_type": "other",
                "due_date": None,
                "severity": "medium",
            }
        )
        if len(obligations) >= 8:
            break

    risk = 50
    if expiration and (expiration - date.today()).days <= 90:
        risk += 15
    if re.search(r"(?i)auto-?renew", body):
        risk += 10
    if not re.search(r"(?i)cyber|security|encrypt", body):
        risk += 10
    risk = max(0, min(100, risk))

    return {
        "vendor_name": vendor[:255],
        "effective_date": effective,
        "expiration_date": expiration,
        "renewal_clause": renewal,
        "risk_score": risk,
        "obligations": obligations,
    }


async def extract_clm_metadata(document_text: str, filename: str) -> dict[str, Any]:
    snippet = (document_text or "").strip()
    if not snippet:
        return _heuristic_extract("", filename)

    fallback = _heuristic_extract(snippet, filename)
    if not settings.openai_api_key:
        return fallback

    # Structured extraction should use the same reliable extraction model as
    # compliance (gpt-5.5 rejects non-default temperature on chat.completions).
    model = settings.compliance_extraction_model or "gpt-4o-mini"
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": CLM_EXTRACTION_PROMPT},
                {
                    "role": "user",
                    "content": f"Filename: {filename}\n\nContract text:\n{snippet[:12000]}",
                },
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or "{}"
        data = json.loads(raw)
    except Exception as exc:
        logger.warning("clm_ai_extraction_failed model=%s error=%s", model, exc)
        return fallback

    vendor_name = str(data.get("vendor_name") or "").strip()
    if not vendor_name or vendor_name.lower() in {
        _filename_vendor_fallback(filename).lower(),
        filename.lower(),
    }:
        vendor_name = fallback["vendor_name"]

    obligations = []
    for item in data.get("obligations") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        obligations.append(
            {
                "title": title[:500],
                "description": str(item.get("description") or "")[:2000] or None,
                "obligation_type": str(item.get("obligation_type") or "other")[:64],
                "due_date": _parse_date(item.get("due_date")),
                "severity": str(item.get("severity") or "medium")[:20],
            }
        )
    if not obligations:
        obligations = fallback["obligations"]

    risk = data.get("risk_score")
    try:
        risk_score = max(0, min(100, int(risk)))
    except (TypeError, ValueError):
        risk_score = fallback["risk_score"]

    return {
        "vendor_name": vendor_name[:255],
        "effective_date": _parse_date(data.get("effective_date"))
        or fallback["effective_date"],
        "expiration_date": _parse_date(data.get("expiration_date"))
        or fallback["expiration_date"],
        "renewal_clause": (
            (str(data.get("renewal_clause")).strip()[:4000] or None)
            if data.get("renewal_clause")
            else fallback["renewal_clause"]
        ),
        "risk_score": risk_score,
        "obligations": obligations[:10],
    }
