"""AI quality review for task resolutions before approver sign-off."""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Optional

from openai import AsyncOpenAI

from app.core.config import settings
from app.db.models import User, WorkspaceTask

logger = logging.getLogger(__name__)

APPROVAL_REVIEW_PROMPT = """You are a senior healthcare compliance auditor reviewing whether a task
resolution adequately addresses the original task requirements.

Evaluate the assignee's resolution against the task description, attached documents context,
and the structured resolution output. Determine if the work is complete, accurate, and
compliance-ready for final approval.

Return ONLY valid JSON (no markdown fences):
{
  "recommendation": "approve" | "reject",
  "confidence": "high" | "medium" | "low",
  "summary": "2-4 sentence executive summary for the approver",
  "resolution_quality": "adequate" | "partial" | "inadequate",
  "strengths": ["what was done well"],
  "gaps": ["remaining issues or missing items"],
  "findings_review": [
    {
      "title": "finding title",
      "status": "addressed" | "partially_addressed" | "not_addressed",
      "detail": "brief explanation"
    }
  ],
  "rationale": "why approve or reject — decisive guidance for the approver",
  "suggested_approver_action": "approve" | "reject"
}

Be strict on healthcare compliance gaps. Recommend reject if critical findings are not addressed
or the resolution is vague, incomplete, or contradicts requirements."""


def _parse_json_from_text(text: str) -> Optional[dict]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```json\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


def latest_resolution_entry_id(task: WorkspaceTask) -> Optional[str]:
    history = task.resolution_history or []
    if not history:
        return None
    return history[-1].get("id")


def valid_approval_review_for_task(
    task: WorkspaceTask,
    review_id: Optional[str] = None,
) -> Optional[dict]:
    reviews = task.approval_ai_reviews or []
    if not reviews:
        return None
    current_res_id = latest_resolution_entry_id(task)
    if not current_res_id:
        return None
    if review_id:
        for review in reversed(reviews):
            if review.get("id") == review_id and review.get("resolution_entry_id") == current_res_id:
                return review
        return None
    for review in reversed(reviews):
        if review.get("resolution_entry_id") == current_res_id:
            return review
    return None


def _build_review_context(
    task: WorkspaceTask,
    documents: list[dict],
    reviewer: User,
) -> str:
    resolution = task.resolution_result or {}
    resolution_json = json.dumps(resolution, indent=2, default=str)[:12000]
    doc_lines = "\n".join(
        f"- {d.get('title', 'Document')} (id: {d.get('id', '')})" for d in documents[:10]
    ) or "None"
    history = task.resolution_history or []
    latest_entry = history[-1] if history else {}
    return (
        f"TASK TITLE: {task.title}\n"
        f"TASK DESCRIPTION: {(task.description or '')[:4000]}\n"
        f"CATEGORY: {task.category or 'General'}\n"
        f"PRIORITY: {task.priority}\n"
        f"ATTACHED DOCUMENTS:\n{doc_lines}\n"
        f"RESOLUTION SUMMARY: {resolution.get('summary', latest_entry.get('summary', ''))}\n"
        f"RESOLUTION RISK LEVEL: {resolution.get('risk_level', latest_entry.get('risk_level', ''))}\n"
        f"RESOLUTION DELIVERABLE: {latest_entry.get('resolution_document_title', '')}\n"
        f"STRUCTURED RESOLUTION JSON:\n{resolution_json}\n"
        f"REVIEWER (AI audit requested by): {reviewer.first_name} {reviewer.last_name} ({reviewer.email})"
    )


def _normalize_report(raw: dict, *, model: str, source: str) -> dict:
    recommendation = str(raw.get("recommendation", "reject")).lower()
    if recommendation not in ("approve", "reject"):
        recommendation = "reject"
    suggested = str(raw.get("suggested_approver_action", recommendation)).lower()
    if suggested not in ("approve", "reject"):
        suggested = recommendation
    confidence = str(raw.get("confidence", "medium")).lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "medium"
    quality = str(raw.get("resolution_quality", "partial")).lower()
    if quality not in ("adequate", "partial", "inadequate"):
        quality = "partial"

    return {
        "recommendation": recommendation,
        "confidence": confidence,
        "summary": str(raw.get("summary") or "AI review completed."),
        "resolution_quality": quality,
        "strengths": [str(s) for s in (raw.get("strengths") or [])[:8]],
        "gaps": [str(g) for g in (raw.get("gaps") or [])[:8]],
        "findings_review": [
            {
                "title": str(f.get("title", "Finding")),
                "status": str(f.get("status", "not_addressed")),
                "detail": str(f.get("detail", "")),
            }
            for f in (raw.get("findings_review") or [])[:12]
            if isinstance(f, dict)
        ],
        "rationale": str(raw.get("rationale") or raw.get("summary") or ""),
        "suggested_approver_action": suggested,
        "model": model,
        "source": source,
    }


def _fallback_report(task: WorkspaceTask) -> dict:
    """Heuristic report when OpenAI is unavailable."""
    resolution = task.resolution_result or {}
    findings = resolution.get("findings") or []
    risk = str(resolution.get("risk_level", "")).lower()
    summary = str(resolution.get("summary") or "Resolution submitted for review.")
    open_findings = [
        f for f in findings
        if isinstance(f, dict) and str(f.get("severity", "")).lower() in ("high", "critical")
    ]
    recommend_reject = bool(open_findings) or risk in ("high", "critical")
    recommendation = "reject" if recommend_reject else "approve"
    gaps = [str(f.get("title", "Gap")) for f in open_findings[:5]]
    if not gaps and not summary.strip():
        gaps = ["Resolution lacks sufficient detail for automated verification"]
        recommendation = "reject"

    return _normalize_report(
        {
            "recommendation": recommendation,
            "confidence": "low",
            "summary": summary or "Automated review could not reach OpenAI; heuristic check applied.",
            "resolution_quality": "partial" if recommend_reject else "adequate",
            "strengths": ["Resolution structure present"] if resolution else [],
            "gaps": gaps,
            "findings_review": [
                {
                    "title": str(f.get("title", "Finding")),
                    "status": "partially_addressed",
                    "detail": str(f.get("detail", "")),
                }
                for f in findings[:8]
                if isinstance(f, dict)
            ],
            "rationale": (
                "High-severity findings remain or risk is elevated — recommend rejection until resolved."
                if recommend_reject
                else "No critical gaps detected in structured resolution — approve may be appropriate."
            ),
            "suggested_approver_action": recommendation,
        },
        model="heuristic",
        source="fallback",
    )


async def run_approval_ai_review(
    task: WorkspaceTask,
    reviewer: User,
    documents: list[dict],
) -> dict[str, Any]:
    """Run AI review and return a review record (not yet persisted)."""
    res_entry_id = latest_resolution_entry_id(task)
    if not res_entry_id:
        raise ValueError("Task has no saved resolution to review")
    if not task.resolution_result:
        raise ValueError("Task resolution result is required for AI approval review")

    context = _build_review_context(task, documents, reviewer)
    model = settings.compliance_extraction_model
    report: dict

    if settings.openai_api_key:
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": APPROVAL_REVIEW_PROMPT},
                    {"role": "user", "content": context},
                ],
                temperature=0.2,
                max_tokens=1800,
            )
            raw_text = resp.choices[0].message.content or ""
            parsed = _parse_json_from_text(raw_text)
            if parsed:
                report = _normalize_report(parsed, model=model, source="openai")
            else:
                logger.warning("approval_ai_review_parse_failed task=%s", task.id)
                report = _fallback_report(task)
        except Exception as exc:
            logger.warning("approval_ai_review_failed task=%s: %s", task.id, exc)
            report = _fallback_report(task)
    else:
        report = _fallback_report(task)

    now = datetime.utcnow()
    return {
        "id": f"ar_{uuid.uuid4().hex[:12]}",
        "resolution_entry_id": res_entry_id,
        "reviewed_by": str(reviewer.id),
        "reviewed_by_name": f"{reviewer.first_name or ''} {reviewer.last_name or ''}".strip()
        or reviewer.email,
        "created_at": now.isoformat(),
        "report": report,
    }
