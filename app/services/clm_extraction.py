"""AI metadata extraction for CLM contract uploads."""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Optional

from openai import AsyncOpenAI

from app.core.config import settings

CLM_EXTRACTION_PROMPT = """You are a healthcare contract analyst. Extract structured metadata from the contract text.

OUTPUT JSON ONLY:
{
  "vendor_name": "string — counterparty / vendor legal or trade name",
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
- vendor_name is required if any party is identifiable
- Use null when dates are not explicit
- Max 10 obligations; only explicit contractual duties
- No markdown outside JSON
"""


def _parse_date(value: Any) -> Optional[date]:
    if not value or not isinstance(value, str):
        return None
    value = value.strip()[:10]
    try:
        parts = value.split("-")
        if len(parts) == 3:
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, TypeError):
        return None
    return None


def _heuristic_extract(text: str, filename: str) -> dict[str, Any]:
    """Offline / fallback extraction when OpenAI is unavailable."""
    vendor = re.sub(r"\.(pdf|docx?|txt)$", "", filename, flags=re.I)
    vendor = re.sub(r"[_-]+", " ", vendor).strip() or "Unknown Vendor"
    return {
        "vendor_name": vendor[:255],
        "effective_date": None,
        "expiration_date": None,
        "renewal_clause": None,
        "risk_score": 50,
        "obligations": [],
    }


async def extract_clm_metadata(document_text: str, filename: str) -> dict[str, Any]:
    snippet = (document_text or "").strip()
    if not snippet:
        return _heuristic_extract("", filename)

    if not settings.openai_api_key:
        return _heuristic_extract(snippet, filename)

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    model = settings.openai_model or "gpt-4o-mini"
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
    except Exception:
        return _heuristic_extract(snippet, filename)

    vendor_name = str(data.get("vendor_name") or "").strip() or _heuristic_extract(snippet, filename)["vendor_name"]
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

    risk = data.get("risk_score")
    try:
        risk_score = max(0, min(100, int(risk)))
    except (TypeError, ValueError):
        risk_score = 50

    return {
        "vendor_name": vendor_name[:255],
        "effective_date": _parse_date(data.get("effective_date")),
        "expiration_date": _parse_date(data.get("expiration_date")),
        "renewal_clause": (str(data.get("renewal_clause")).strip()[:4000] or None)
        if data.get("renewal_clause")
        else None,
        "risk_score": risk_score,
        "obligations": obligations[:10],
    }
