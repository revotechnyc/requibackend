"""
Extract compliance gaps & framework scores from Intelligence turns (PDF spec §3).

Runs after chat completes — failures are logged only; never breaks the chat stream.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Optional

from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.permissions import FeatureGate
from app.db.models import (
    ComplianceFramework,
    ComplianceGap,
    ComplianceScoreSnapshot,
    Organization,
    PlanType,
)
from app.services.compliance_gap_helpers import (
    GapSourceContext,
    apply_gap_source_context,
    build_gap_source_context,
)
from app.services.compliance_service import (
    FRAMEWORK_CATALOG,
    calculate_ai_score,
    calculate_risk_level,
    ensure_default_frameworks,
    framework_limit_for_plan,
)

logger = logging.getLogger(__name__)

COMPLIANCE_KEYWORDS = re.compile(
    r"\b(hipaa|fwa|compliance|audit|gap|risk|baa|business\s+associate|phi|"
    r"stark|kickback|fraud|waste|abuse|cms|oig|regulatory|safeguard|"
    r"breach|training|documentation|hipaa\s+security|45\s+cfr)\b",
    re.I,
)

EXTRACTION_PROMPT = """You are a healthcare compliance analyst. Read the user's question and the assistant's answer.
Identify concrete compliance GAPS (missing policies, missing BAAs, overdue training, weak controls, etc.)
and estimate per-framework scores (0-100).

Return ONLY valid JSON (no markdown):
{
  "framework_scores": {
    "hipaa": <number 0-100>,
    "fwa": <number 0-100>
  },
  "overall_ai_score": <number 0-100>,
  "risk_level": "low" | "medium" | "high" | "critical",
  "gaps_found": [
    {
      "title": "<short title>",
      "framework_slug": "hipaa" | "fwa" | "stark_law" | "anti_kickback" | "gdpr" | "reporting" | "documentation" | "training",
      "severity": "critical" | "high" | "medium" | "low",
      "description": "<one sentence>",
      "category": "<display name e.g. HIPAA>"
    }
  ],
  "recommendations": ["<action item>"]
}

Rules:
- Only include gaps that are clearly implied or stated in the conversation (max 5).
- If no gaps, return "gaps_found": [].
- framework_scores should include every framework you mention in gaps.
- Be conservative: do not invent violations without basis in the text."""


def _should_analyze(user_message: str, assistant_message: str, *, has_documents: bool) -> bool:
    if has_documents:
        return True
    combined = f"{user_message}\n{assistant_message}"
    return bool(COMPLIANCE_KEYWORDS.search(combined))


def _parse_json_from_text(text: str) -> Optional[dict]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _mock_analysis(user_message: str, assistant_message: str) -> dict:
    """Deterministic gaps for MOCK_CHAT_STREAM / offline testing."""
    msg = (user_message or "").lower()
    gaps: list[dict] = []
    scores = {"hipaa": 88.0, "fwa": 82.0}

    if "baa" in msg or "business associate" in msg:
        gaps.append(
            {
                "title": "Missing or incomplete Business Associate Agreement",
                "framework_slug": "hipaa",
                "severity": "high",
                "description": "Identified from compliance review question about BAAs.",
                "category": "HIPAA",
            }
        )
        scores["hipaa"] = 72.0
    if "risk assessment" in msg or "risk analysis" in msg:
        gaps.append(
            {
                "title": "HIPAA security risk assessment needs update",
                "framework_slug": "hipaa",
                "severity": "medium",
                "description": "Risk analysis documentation gap discussed in chat.",
                "category": "HIPAA",
            }
        )
        scores["hipaa"] = min(scores["hipaa"], 75.0)
    if "training" in msg and ("overdue" in msg or "annual" in msg or "workforce" in msg):
        gaps.append(
            {
                "title": "Workforce HIPAA training documentation gap",
                "framework_slug": "hipaa",
                "severity": "medium",
                "description": "Training compliance mentioned in user query.",
                "category": "Training",
            }
        )
    if "fwa" in msg or "fraud" in msg or "billing" in msg:
        gaps.append(
            {
                "title": "FWA billing compliance control review needed",
                "framework_slug": "fwa",
                "severity": "medium",
                "description": "FWA-related topic in conversation.",
                "category": "FWA",
            }
        )
        scores["fwa"] = 76.0

    if not gaps and COMPLIANCE_KEYWORDS.search(user_message or ""):
        gaps.append(
            {
                "title": "Follow-up compliance documentation recommended",
                "framework_slug": "hipaa",
                "severity": "low",
                "description": "General compliance topic — confirm evidence in policies.",
                "category": "HIPAA",
            }
        )

    overall = calculate_ai_score({k: float(v) for k, v in scores.items()})
    risk = calculate_risk_level(overall, 0.0)
    return {
        "framework_scores": scores,
        "overall_ai_score": overall,
        "risk_level": risk,
        "gaps_found": gaps[:5],
        "recommendations": [
            "Review open gaps on the Compliance dashboard.",
            "Upload supporting policies to Documents and re-run analysis.",
        ],
    }


async def _call_extraction_model(user_message: str, assistant_message: str) -> Optional[dict]:
    if not settings.openai_api_key:
        return None
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    user_block = (
        f"USER QUESTION:\n{user_message[:4000]}\n\n"
        f"ASSISTANT ANSWER:\n{assistant_message[:6000]}"
    )
    try:
        resp = await client.chat.completions.create(
            model=settings.compliance_extraction_model,
            messages=[
                {"role": "system", "content": EXTRACTION_PROMPT},
                {"role": "user", "content": user_block},
            ],
            temperature=0.2,
            max_tokens=1200,
        )
        raw = resp.choices[0].message.content or ""
        return _parse_json_from_text(raw)
    except Exception as exc:
        logger.warning("compliance_ai_extraction_failed: %s", exc)
        return None


async def _ensure_framework_slot(
    db: AsyncSession,
    org: Organization,
    slug: str,
) -> bool:
    """Return True if framework exists (active or reactivated) or was created."""
    slug = slug.strip().lower()
    if slug not in FRAMEWORK_CATALOG:
        return False

    result = await db.execute(
        select(ComplianceFramework).where(
            ComplianceFramework.organization_id == org.id,
            ComplianceFramework.slug == slug,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        if not existing.is_active:
            plan = org.subscription.plan_type if org.subscription else PlanType.STANDARD
            limit = framework_limit_for_plan(plan)
            count_result = await db.execute(
                select(ComplianceFramework).where(
                    ComplianceFramework.organization_id == org.id,
                    ComplianceFramework.is_active == True,
                )
            )
            if limit is not None and len(count_result.scalars().all()) >= limit:
                return False
            existing.is_active = True
            await db.flush()
        return True

    plan = org.subscription.plan_type if org.subscription else PlanType.STANDARD
    limit = framework_limit_for_plan(plan)
    count_result = await db.execute(
        select(ComplianceFramework).where(
            ComplianceFramework.organization_id == org.id,
            ComplianceFramework.is_active == True,
        )
    )
    active = len(count_result.scalars().all())
    if limit is not None and active >= limit:
        return False

    db.add(
        ComplianceFramework(
            organization_id=org.id,
            slug=slug,
            name=FRAMEWORK_CATALOG[slug],
            score=75.0,
            is_active=True,
        )
    )
    await db.flush()
    return True


async def persist_ai_compliance_analysis(
    db: AsyncSession,
    org: Organization,
    analysis: dict,
    *,
    source_type: str = "chat",
    source_context: Optional[GapSourceContext] = None,
) -> dict[str, Any]:
    """Write gaps + snapshot; update framework scores. Returns summary for logging/SSE."""
    await ensure_default_frameworks(db, org.id)

    framework_scores = analysis.get("framework_scores") or {}
    if isinstance(framework_scores, dict):
        framework_scores = {k: float(v) for k, v in framework_scores.items() if v is not None}
    else:
        framework_scores = {}

    overall = float(analysis.get("overall_ai_score") or calculate_ai_score(framework_scores))
    risk_level = str(analysis.get("risk_level") or calculate_risk_level(overall, 0.0))
    gaps_raw = analysis.get("gaps_found") or []
    recommendations = analysis.get("recommendations") or []

    created_gaps = 0
    skipped_gaps = 0

    for item in gaps_raw:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        slug = (item.get("framework_slug") or "hipaa").strip().lower()
        await _ensure_framework_slot(db, org, slug)

        dup = await db.execute(
            select(ComplianceGap).where(
                ComplianceGap.organization_id == org.id,
                ComplianceGap.framework_slug == slug,
                ComplianceGap.title == title,
                ComplianceGap.status == "open",
            )
        )
        if dup.scalar_one_or_none():
            skipped_gaps += 1
            continue

        severity = item.get("severity") or "medium"
        if severity not in ("critical", "high", "medium", "low"):
            severity = "medium"

        gap = ComplianceGap(
            organization_id=org.id,
            framework_slug=slug,
            title=title[:500],
            description=(item.get("description") or "")[:2000] or None,
            severity=severity,
            category=(item.get("category") or FRAMEWORK_CATALOG.get(slug, slug))[:100],
            status="open",
        )
        apply_gap_source_context(gap, source_context)
        db.add(gap)
        created_gaps += 1

    for slug, score in framework_scores.items():
        fw_result = await db.execute(
            select(ComplianceFramework).where(
                ComplianceFramework.organization_id == org.id,
                ComplianceFramework.slug == slug,
                ComplianceFramework.is_active == True,
            )
        )
        fw = fw_result.scalar_one_or_none()
        if fw:
            fw.score = round(min(100.0, max(0.0, float(score))), 1)

    gaps_for_snapshot = [
        {
            "title": g.get("title"),
            "severity": g.get("severity"),
            "framework_slug": g.get("framework_slug"),
        }
        for g in gaps_raw
        if isinstance(g, dict) and g.get("title")
    ]

    db.add(
        ComplianceScoreSnapshot(
            organization_id=org.id,
            framework_scores=framework_scores,
            overall_score=overall,
            risk_level=risk_level,
            gaps_found=gaps_for_snapshot,
            recommendations=recommendations if isinstance(recommendations, list) else [],
            source_type=source_type,
        )
    )
    await db.commit()

    return {
        "gaps_created": created_gaps,
        "gaps_skipped_duplicate": skipped_gaps,
        "overall_ai_score": overall,
        "risk_level": risk_level,
        "source_type": source_type,
    }


async def process_intelligence_compliance_update(
    db: AsyncSession,
    org: Organization,
    user_message: str,
    assistant_message: str,
    *,
    source_type: str = "chat",
    has_documents: bool = False,
    use_mock: bool = False,
    source_context: Optional[GapSourceContext] = None,
    conversation_id: Optional[uuid.UUID] = None,
    task_id: Optional[uuid.UUID] = None,
    workflow_id: Optional[uuid.UUID] = None,
    contract_id: Optional[uuid.UUID] = None,
    contract_name: Optional[str] = None,
    document_filename: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """
    Run compliance extraction after an Intelligence turn.
    Returns summary dict or None if skipped.
    """
    if not getattr(settings, "compliance_ai_extraction_enabled", True):
        return None

    plan = org.subscription.plan_type if org.subscription else PlanType.STANDARD
    if not FeatureGate.has_feature(plan, "compliance"):
        return None

    if not _should_analyze(user_message, assistant_message, has_documents=has_documents):
        return None

    if not (assistant_message or "").strip():
        return None

    if source_context is None:
        source_context = await build_gap_source_context(
            db,
            user_message=user_message,
            source_type=source_type,
            conversation_id=conversation_id,
            task_id=task_id,
            workflow_id=workflow_id,
            contract_id=contract_id,
            contract_name=contract_name,
            document_filename=document_filename,
        )

    if use_mock or settings.mock_chat_stream:
        analysis = _mock_analysis(user_message, assistant_message)
    else:
        analysis = await _call_extraction_model(user_message, assistant_message)
        if not analysis:
            analysis = _mock_analysis(user_message, assistant_message)

    try:
        return await persist_ai_compliance_analysis(
            db,
            org,
            analysis,
            source_type=source_type,
            source_context=source_context,
        )
    except Exception as exc:
        logger.exception("persist_ai_compliance_failed: %s", exc)
        await db.rollback()
        return None
