"""
Compliance dashboard aggregation — v3.5 spec (Dashboard Logic Technical Spec).

Overall = AI*0.50 + Task*0.30 + Doc*0.20
AI score = weighted framework scores
Risk level from AI score + overdue task ratio
Audit readiness = min(doc, ai) * maturity factor
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ComplianceFramework,
    ComplianceGap,
    ComplianceScoreSnapshot,
    Document,
    DocumentChunk,
    Organization,
    PlanType,
    WorkspaceTask,
    WorkspaceTaskStatus,
)

FRAMEWORK_WEIGHTS: dict[str, float] = {
    "hipaa": 0.35,
    "fwa": 0.20,
    "stark_law": 0.15,
    "anti_kickback": 0.15,
    "gdpr": 0.15,
}

FRAMEWORK_CATALOG: dict[str, str] = {
    "hipaa": "HIPAA",
    "fwa": "FWA",
    "stark_law": "Stark Law",
    "anti_kickback": "Anti-Kickback",
    "gdpr": "GDPR",
    "reporting": "Reporting",
    "documentation": "Documentation",
    "training": "Training",
}

DEFAULT_STARTER_SLUGS = ("hipaa", "fwa")

# Baseline scores before open-gap penalties (must not use already-penalized fw.score).
DEFAULT_FRAMEWORK_BASELINES: dict[str, float] = {
    "hipaa": 92.0,
    "fwa": 78.0,
}
DEFAULT_OTHER_FRAMEWORK_BASELINE = 80.0

AI_SNAPSHOT_SOURCES = frozenset({"chat", "document_analysis", "gap_assessment"})

PRO_MAX_FRAMEWORKS = 3

TERMINAL_TASK_STATUSES = {
    WorkspaceTaskStatus.APPROVED.value,
    WorkspaceTaskStatus.COMPLETED.value,
}

ACTIVE_TASK_STATUSES = {
    WorkspaceTaskStatus.PENDING.value,
    WorkspaceTaskStatus.IN_PROGRESS.value,
    WorkspaceTaskStatus.SUBMITTED_FOR_REVIEW.value,
    WorkspaceTaskStatus.REVIEWED.value,
    WorkspaceTaskStatus.APPROVED.value,
    WorkspaceTaskStatus.COMPLETED.value,
    WorkspaceTaskStatus.REJECTED.value,
}

TASK_HEALTH_WEIGHTS: dict[str, float] = {
    WorkspaceTaskStatus.COMPLETED.value: 1.0,
    WorkspaceTaskStatus.APPROVED.value: 1.0,
    WorkspaceTaskStatus.IN_PROGRESS.value: 0.5,
    WorkspaceTaskStatus.SUBMITTED_FOR_REVIEW.value: 0.5,
    WorkspaceTaskStatus.REVIEWED.value: 0.5,
    WorkspaceTaskStatus.PENDING.value: 0.0,
    WorkspaceTaskStatus.REJECTED.value: -0.5,
}


def framework_limit_for_plan(plan_type: PlanType) -> Optional[int]:
    """None = unlimited (Enterprise)."""
    if plan_type == PlanType.ENTERPRISE:
        return None
    if plan_type == PlanType.PRO:
        return PRO_MAX_FRAMEWORKS
    return 0


def calculate_ai_score(framework_scores: dict[str, float]) -> float:
    if not framework_scores:
        return 0.0
    weights = {k: FRAMEWORK_WEIGHTS.get(k, 0.10) for k in framework_scores}
    total_w = sum(weights.values()) or 1.0
    weighted = sum(framework_scores[k] * weights[k] for k in framework_scores)
    return round(weighted / total_w, 1)


def calculate_risk_level(ai_score: float, overdue_ratio: float) -> str:
    if ai_score >= 90:
        base = "low"
    elif ai_score >= 75:
        base = "medium"
    elif ai_score >= 60:
        base = "high"
    else:
        base = "critical"

    if overdue_ratio > 0.50:
        order = ["low", "medium", "high", "critical"]
        idx = min(order.index(base) + 2, len(order) - 1)
        return order[idx]
    if overdue_ratio > 0.30 and base == "low":
        return "medium"
    if overdue_ratio > 0.50 and base == "medium":
        return "high"
    if overdue_ratio > 0.40 and base == "high":
        return "critical"
    return base


def calculate_task_health_score(status_distribution: dict[str, int]) -> float:
    total = sum(status_distribution.values())
    if total == 0:
        return 100.0
    weighted_sum = sum(
        count * TASK_HEALTH_WEIGHTS.get(status, 0.0)
        for status, count in status_distribution.items()
    )
    max_possible = total * 1.0
    health = (weighted_sum / max_possible) * 100
    pending_ratio = status_distribution.get(WorkspaceTaskStatus.PENDING.value, 0) / total
    if pending_ratio > 0.40:
        health *= 1 - (pending_ratio - 0.40)
    return max(0.0, round(health, 1))


def calculate_audit_readiness(
    ai_score: float,
    document_completeness: float,
    critical_gaps: int,
) -> float:
    base = min(document_completeness, ai_score)
    maturity = max(0.5, 1.0 - (critical_gaps * 0.05))
    return round(base * maturity, 1)


def _parse_due_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _status_from_score(score: float) -> str:
    if score >= 80:
        return "good"
    if score >= 60:
        return "warning"
    return "critical"


async def ensure_default_frameworks(db: AsyncSession, org_id: uuid.UUID) -> None:
    """Ensure HIPAA + FWA rows exist (per slug; reactivate if previously removed)."""
    for slug in DEFAULT_STARTER_SLUGS:
        result = await db.execute(
            select(ComplianceFramework).where(
                ComplianceFramework.organization_id == org_id,
                ComplianceFramework.slug == slug,
            )
        )
        row = result.scalar_one_or_none()
        if row:
            if not row.is_active:
                row.is_active = True
            continue
        db.add(
            ComplianceFramework(
                organization_id=org_id,
                slug=slug,
                name=FRAMEWORK_CATALOG[slug],
                score=92.0 if slug == "hipaa" else 78.0,
                is_active=True,
            )
        )
    await db.flush()


async def _fetch_task_metrics(db: AsyncSession, org_id: uuid.UUID) -> dict[str, Any]:
    result = await db.execute(
        select(WorkspaceTask).where(WorkspaceTask.organization_id == org_id)
    )
    tasks = result.scalars().all()
    today = datetime.utcnow().date()

    status_distribution: dict[str, int] = {}
    overdue_count = 0
    active_non_terminal = 0

    for t in tasks:
        status_distribution[t.status] = status_distribution.get(t.status, 0) + 1
        if t.status in TERMINAL_TASK_STATUSES:
            continue
        active_non_terminal += 1
        due = _parse_due_date(t.due_date)
        if due and due < today:
            overdue_count += 1

    total_active = sum(
        status_distribution.get(s, 0)
        for s in ACTIVE_TASK_STATUSES
    )
    done = status_distribution.get(WorkspaceTaskStatus.COMPLETED.value, 0) + status_distribution.get(
        WorkspaceTaskStatus.APPROVED.value, 0
    )
    completion_rate = (done / total_active * 100) if total_active else 100.0

    health = calculate_task_health_score(status_distribution)
    overdue_ratio = overdue_count / active_non_terminal if active_non_terminal else 0.0
    if overdue_ratio > 0.20 and total_active:
        health = round(health * (1 - overdue_ratio), 1)

    return {
        "status_distribution": status_distribution,
        "task_health_score": health,
        "completion_rate": round(completion_rate, 1),
        "overdue_count": overdue_count,
        "overdue_ratio": round(overdue_ratio, 3),
        "open_tasks": total_active - done,
    }


async def _fetch_document_metrics(db: AsyncSession, org_id: uuid.UUID) -> dict[str, Any]:
    doc_result = await db.execute(
        select(Document).where(
            Document.organization_id == org_id,
            Document.is_active == True,
        )
    )
    docs = doc_result.scalars().all()
    total = len(docs)
    if total == 0:
        return {
            "total": 0,
            "analyzed": 0,
            "readiness_score": 0.0,
            "analyzing_count": 0,
            "analyzing": [],
        }

    analyzed = 0
    analyzing: list[dict[str, Any]] = []
    for doc in docs:
        chunk_count = await db.scalar(
            select(func.count()).select_from(DocumentChunk).where(
                DocumentChunk.document_id == doc.id
            )
        )
        if chunk_count and chunk_count > 0:
            analyzed += 1
        meta = doc.document_metadata or {}
        status = meta.get("ingestion_status") or ""
        if status in ("indexing", "gap_analysis", "processing"):
            # Legacy processing: with chunks = analyzing gaps
            phase = status
            if status == "processing":
                phase = "gap_analysis" if chunk_count else "indexing"
            analyzing.append(
                {
                    "id": str(doc.id),
                    "name": doc.title,
                    "status": phase,
                }
            )

    readiness = round((analyzed / total) * 100, 1)
    return {
        "total": total,
        "analyzed": analyzed,
        "readiness_score": readiness,
        "analyzing_count": len(analyzing),
        "analyzing": analyzing[:10],
    }


def _gap_severity_penalty(gap: ComplianceGap) -> int:
    if gap.severity == "critical":
        return 15
    if gap.severity == "high":
        return 8
    if gap.severity == "medium":
        return 4
    return 2


async def _framework_baseline_scores(
    db: AsyncSession,
    org_id: uuid.UUID,
    frameworks: list[ComplianceFramework],
) -> dict[str, float]:
    """Pre-penalty framework scores from latest AI analysis or catalog defaults."""
    snap_result = await db.execute(
        select(ComplianceScoreSnapshot)
        .where(ComplianceScoreSnapshot.organization_id == org_id)
        .where(ComplianceScoreSnapshot.source_type.in_(AI_SNAPSHOT_SOURCES))
        .order_by(ComplianceScoreSnapshot.calculated_at.desc())
        .limit(1)
    )
    snap = snap_result.scalar_one_or_none()
    snap_scores: dict[str, float] = {}
    if snap and snap.framework_scores:
        snap_scores = {
            k: float(v) for k, v in snap.framework_scores.items() if v is not None
        }

    baselines: dict[str, float] = {}
    for fw in frameworks:
        if not fw.is_active:
            continue
        slug = fw.slug
        if slug in snap_scores:
            baselines[slug] = snap_scores[slug]
        elif slug in DEFAULT_FRAMEWORK_BASELINES:
            baselines[slug] = DEFAULT_FRAMEWORK_BASELINES[slug]
        else:
            baselines[slug] = DEFAULT_OTHER_FRAMEWORK_BASELINE
    return baselines


async def _sync_framework_scores_from_gaps(
    db: AsyncSession,
    org_id: uuid.UUID,
    frameworks: list[ComplianceFramework],
) -> dict[str, float]:
    gap_result = await db.execute(
        select(ComplianceGap).where(
            ComplianceGap.organization_id == org_id,
            ComplianceGap.status == "open",
        )
    )
    open_gaps = gap_result.scalars().all()
    gaps_by_slug: dict[str, list[ComplianceGap]] = {}
    for g in open_gaps:
        gaps_by_slug.setdefault(g.framework_slug, []).append(g)

    baselines = await _framework_baseline_scores(db, org_id, frameworks)

    scores: dict[str, float] = {}
    for fw in frameworks:
        if not fw.is_active:
            continue
        gap_list = gaps_by_slug.get(fw.slug, [])
        penalty = sum(_gap_severity_penalty(g) for g in gap_list)
        base = baselines.get(fw.slug, DEFAULT_OTHER_FRAMEWORK_BASELINE)
        scores[fw.slug] = round(max(0.0, min(100.0, base - penalty)), 1)
        fw.score = scores[fw.slug]
    await db.flush()
    return scores


async def build_compliance_overview(
    db: AsyncSession,
    org: Organization,
    *,
    persist_snapshot: bool = True,
) -> dict[str, Any]:
    """Full compliance dashboard payload per technical spec."""
    org_id = org.id
    await ensure_default_frameworks(db, org_id)

    fw_result = await db.execute(
        select(ComplianceFramework).where(
            ComplianceFramework.organization_id == org_id,
            ComplianceFramework.is_active == True,
        )
    )
    frameworks = list(fw_result.scalars().all())
    framework_scores = await _sync_framework_scores_from_gaps(db, org_id, frameworks)
    ai_score = calculate_ai_score(framework_scores)

    task_metrics = await _fetch_task_metrics(db, org_id)
    doc_metrics = await _fetch_document_metrics(db, org_id)
    task_score = task_metrics["task_health_score"]
    doc_score = doc_metrics["readiness_score"]

    overall = round(ai_score * 0.50 + task_score * 0.30 + doc_score * 0.20)

    gap_result = await db.execute(
        select(ComplianceGap).where(
            ComplianceGap.organization_id == org_id,
            ComplianceGap.status == "open",
        )
    )
    open_gaps = gap_result.scalars().all()
    critical_gaps = sum(1 for g in open_gaps if g.severity == "critical")
    risk_level = calculate_risk_level(ai_score, task_metrics["overdue_ratio"])
    audit_readiness = calculate_audit_readiness(ai_score, doc_score, critical_gaps)

    at_risk = sum(
        1
        for fw in frameworks
        if fw.score is not None and float(fw.score) < 75
    )

    today = datetime.utcnow().date()
    task_due_result = await db.execute(
        select(WorkspaceTask).where(
            WorkspaceTask.organization_id == org_id,
            WorkspaceTask.due_date.isnot(None),
            WorkspaceTask.status.notin_(list(TERMINAL_TASK_STATUSES)),
        )
    )
    future_dues: list[date] = []
    for t in task_due_result.scalars().all():
        d = _parse_due_date(t.due_date)
        if d and d >= today:
            future_dues.append(d)
    days_to_audit = (min(future_dues) - today).days if future_dues else None

    upcoming_reviews = []
    upcoming_tasks = await db.execute(
        select(WorkspaceTask)
        .where(
            WorkspaceTask.organization_id == org_id,
            WorkspaceTask.due_date.isnot(None),
        )
        .order_by(WorkspaceTask.due_date.asc())
        .limit(5)
    )
    for t in upcoming_tasks.scalars().all():
        d = _parse_due_date(t.due_date)
        if not d:
            continue
        upcoming_reviews.append(
            {
                "id": str(t.id),
                "title": t.title,
                "date": d.isoformat(),
                "date_label": d.strftime("%b %d, %Y"),
                "type": "Mandatory" if t.priority == "high" else "Internal",
                "framework_slug": t.category.lower().replace(" ", "_")[:64],
            }
        )

    categories = []
    for fw in frameworks:
        gap_count = sum(1 for g in open_gaps if g.framework_slug == fw.slug)
        score = float(fw.score or 0)
        updated = fw.updated_at or fw.created_at
        delta = datetime.utcnow() - updated
        if delta.days == 0:
            last_updated = "Today"
        elif delta.days == 1:
            last_updated = "Yesterday"
        else:
            last_updated = f"{delta.days} days ago"
        categories.append(
            {
                "id": str(fw.id),
                "slug": fw.slug,
                "name": fw.name,
                "score": round(score),
                "gaps": gap_count,
                "last_updated": last_updated,
                "status": _status_from_score(score),
            }
        )

    recent_gaps = []
    for g in sorted(open_gaps, key=lambda x: x.created_at, reverse=True)[:10]:
        days_open = (datetime.utcnow() - g.created_at).days
        fw_name = FRAMEWORK_CATALOG.get(g.framework_slug, g.framework_slug.replace("_", " ").title())
        recent_gaps.append(
            {
                "id": str(g.id),
                "title": g.title,
                "category": fw_name,
                "framework_slug": g.framework_slug,
                "severity": g.severity,
                "days_open": max(0, days_open),
            }
        )

    plan_type = org.subscription.plan_type if org.subscription else PlanType.STANDARD
    limit = framework_limit_for_plan(plan_type)

    overview = {
        "workspace_id": str(org_id),
        "calculated_at": datetime.utcnow().isoformat(),
        "plan_type": plan_type.value,
        "framework_limit": limit,
        "active_framework_count": len(frameworks),
        "summary": {
            "overall_score": overall,
            "overall_change": None,
            "open_gaps": len(open_gaps),
            "gaps_resolved_hint": None,
            "at_risk_areas": at_risk,
            "days_to_audit": days_to_audit,
            "audit_track_label": "On track" if days_to_audit and days_to_audit > 14 else "Needs attention",
            "risk_level": risk_level,
            "audit_readiness": audit_readiness,
        },
        "components": {
            "ai_score": ai_score,
            "task_score": task_score,
            "document_score": doc_score,
            "weights": {"ai": 0.50, "task": 0.30, "document": 0.20},
        },
        "categories": categories,
        "recent_gaps": recent_gaps,
        "upcoming_reviews": upcoming_reviews[:5],
        "task_metrics": task_metrics,
        "document_metrics": doc_metrics,
        "framework_catalog": [
            {"slug": k, "name": v}
            for k, v in FRAMEWORK_CATALOG.items()
        ],
    }

    if persist_snapshot:
        db.add(
            ComplianceScoreSnapshot(
                organization_id=org_id,
                framework_scores=framework_scores,
                overall_score=overall,
                risk_level=risk_level,
                gaps_found=[
                    {
                        "id": str(g.id),
                        "title": g.title,
                        "severity": g.severity,
                        "framework_slug": g.framework_slug,
                    }
                    for g in open_gaps[:50]
                ],
                recommendations=_build_recommendations(overview),
                source_type="aggregation",
            )
        )
        await db.commit()

    return overview


def _build_recommendations(overview: dict[str, Any]) -> list[str]:
    recs: list[str] = []
    if overview["summary"]["overall_score"] < 75:
        recs.append("Priority: Improve documentation completeness — multiple gaps detected.")
    if overview["summary"]["risk_level"] in ("high", "critical"):
        recs.append("Priority: Address high-risk compliance gaps and overdue tasks.")
    if overview["document_metrics"]["readiness_score"] < 70:
        recs.append("Priority: Upload and analyze remaining documents for audit readiness.")
    if not recs:
        recs.append("Strong overall posture. Maintain current compliance practices.")
    return recs
