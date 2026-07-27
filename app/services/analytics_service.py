"""
Tenant Analytics — operational intelligence aggregates for the Analytics tab.
Reuses live compliance / task / gap data (no fake domain scores).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    ComplianceGap,
    Organization,
    User,
    WorkspaceTask,
    WorkspaceTaskStatus,
)
from app.services.compliance_service import (
    TERMINAL_TASK_STATUSES,
    build_compliance_overview,
    _parse_due_date,
)

RISK_LEVEL_SCORE = {
    "low": 25,
    "medium": 50,
    "high": 75,
    "critical": 90,
}

SEVERITY_WEIGHT = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
}


def _user_display_name(user: Optional[User]) -> str:
    if not user:
        return "Unassigned"
    name = f"{(user.first_name or '').strip()} {(user.last_name or '').strip()}".strip()
    return name or user.email or "Unknown"


def _gap_aging_buckets(gaps: list[ComplianceGap]) -> list[dict[str, Any]]:
    buckets = [
        ("0-30 days", 0, 30),
        ("31-60 days", 31, 60),
        ("61-90 days", 61, 90),
        ("90+ days", 91, 10_000),
    ]
    counts = [0, 0, 0, 0]
    now = datetime.utcnow()
    open_gaps = [g for g in gaps if (g.status or "").lower() == "open"]
    for g in open_gaps:
        days = max(0, (now - (g.created_at or now)).days)
        for i, (_label, lo, hi) in enumerate(buckets):
            if lo <= days <= hi:
                counts[i] += 1
                break
    total = sum(counts) or 1
    return [
        {
            "category": label,
            "count": counts[i],
            "pct": round(counts[i] / total * 100, 1),
        }
        for i, (label, _lo, _hi) in enumerate(buckets)
    ]


async def build_analytics_overview(
    db: AsyncSession,
    org: Organization,
) -> dict[str, Any]:
    """Build org-wide analytics payload for the Analytics dashboard."""
    overview = await build_compliance_overview(db, org, persist_snapshot=False)
    summary = overview.get("summary") or {}
    task_metrics = overview.get("task_metrics") or {}
    risk_level = (summary.get("risk_level") or "medium").lower()
    risk_score = RISK_LEVEL_SCORE.get(risk_level, 50)

    # --- Gaps ---
    gap_result = await db.execute(
        select(ComplianceGap).where(ComplianceGap.organization_id == org.id)
    )
    gaps = list(gap_result.scalars().all())
    open_gaps = [g for g in gaps if (g.status or "").lower() == "open"]
    resolved_gaps = [g for g in gaps if (g.status or "").lower() == "resolved"]

    severity_dist: dict[str, int] = defaultdict(int)
    for g in open_gaps:
        sev = (g.severity or "medium").lower()
        severity_dist[sev] += 1

    # Risks by department/area = gap category (workspace taxonomy)
    by_department: dict[str, dict[str, Any]] = {}
    for g in open_gaps:
        dept = (g.category or "General").strip() or "General"
        row = by_department.setdefault(
            dept,
            {"department": dept, "open_gaps": 0, "critical": 0, "high": 0, "risk_weight": 0},
        )
        row["open_gaps"] += 1
        sev = (g.severity or "medium").lower()
        if sev == "critical":
            row["critical"] += 1
        elif sev == "high":
            row["high"] += 1
        row["risk_weight"] += SEVERITY_WEIGHT.get(sev, 2)

    risks_by_department = sorted(
        by_department.values(),
        key=lambda r: (-r["risk_weight"], -r["open_gaps"], r["department"]),
    )[:12]

    # Risks by requester = gap source_label (who/what raised it) or "System"
    by_requester: dict[str, dict[str, Any]] = {}
    for g in open_gaps:
        requester = (g.source_label or "").strip() or (g.source_type or "System").strip() or "System"
        if len(requester) > 80:
            requester = requester[:77] + "…"
        row = by_requester.setdefault(
            requester,
            {"requester": requester, "open_gaps": 0, "critical": 0, "high": 0, "risk_weight": 0},
        )
        row["open_gaps"] += 1
        sev = (g.severity or "medium").lower()
        if sev == "critical":
            row["critical"] += 1
        elif sev == "high":
            row["high"] += 1
        row["risk_weight"] += SEVERITY_WEIGHT.get(sev, 2)

    risks_by_requester = sorted(
        by_requester.values(),
        key=lambda r: (-r["risk_weight"], -r["open_gaps"], r["requester"]),
    )[:12]

    # --- Task performance by assignee ---
    task_result = await db.execute(
        select(WorkspaceTask)
        .where(WorkspaceTask.organization_id == org.id)
        .options(
            selectinload(WorkspaceTask.assignee),
            selectinload(WorkspaceTask.creator),
        )
    )
    tasks = list(task_result.scalars().all())
    today = datetime.utcnow().date()

    by_assignee: dict[str, dict[str, Any]] = {}
    for t in tasks:
        key = str(t.assignee_id) if t.assignee_id else "unassigned"
        name = _user_display_name(t.assignee) if t.assignee_id else "Unassigned"
        row = by_assignee.setdefault(
            key,
            {
                "assignee_id": str(t.assignee_id) if t.assignee_id else None,
                "assignee_name": name,
                "total": 0,
                "open": 0,
                "completed": 0,
                "overdue": 0,
            },
        )
        row["total"] += 1
        status = t.status
        if status in (
            WorkspaceTaskStatus.COMPLETED.value,
            WorkspaceTaskStatus.APPROVED.value,
            "completed",
            "approved",
        ):
            row["completed"] += 1
        elif status not in TERMINAL_TASK_STATUSES:
            row["open"] += 1
            due = _parse_due_date(t.due_date)
            if due and due < today:
                row["overdue"] += 1

    task_performance = sorted(
        by_assignee.values(),
        key=lambda r: (-r["open"], -r["overdue"], r["assignee_name"]),
    )[:15]

    status_dist = task_metrics.get("status_distribution") or {}
    if not status_dist:
        # Derive if missing
        for t in tasks:
            status_dist[t.status] = status_dist.get(t.status, 0) + 1

    return {
        "organization_id": str(org.id),
        "organization_name": org.name,
        "calculated_at": overview.get("calculated_at") or datetime.utcnow().isoformat(),
        "plan_type": overview.get("plan_type"),
        "kpis": {
            "compliance_score": summary.get("overall_score", 0),
            "risk_score": risk_score,
            "risk_level": risk_level,
            "open_gaps": summary.get("open_gaps", len(open_gaps)),
            "resolved_gaps": len(resolved_gaps),
            "at_risk_areas": summary.get("at_risk_areas", 0),
            "audit_readiness": summary.get("audit_readiness", 0),
            "days_to_audit": summary.get("days_to_audit"),
            "audit_track_label": summary.get("audit_track_label"),
            "open_tasks": task_metrics.get("open_tasks", 0),
            "overdue_tasks": task_metrics.get("overdue_count", 0),
            "task_completion_rate": task_metrics.get("completion_rate", 0),
            "task_health_score": task_metrics.get("task_health_score", 0),
        },
        "components": overview.get("components") or {},
        "frameworks": overview.get("categories") or [],
        "gap_severity": [
            {"label": "Critical", "count": severity_dist.get("critical", 0), "color": "#EF4444"},
            {"label": "High", "count": severity_dist.get("high", 0), "color": "#F59E0B"},
            {"label": "Medium", "count": severity_dist.get("medium", 0), "color": "#FB923C"},
            {"label": "Low", "count": severity_dist.get("low", 0), "color": "#22C55E"},
        ],
        "task_status": [
            {"label": k.replace("_", " ").title(), "count": v}
            for k, v in sorted(status_dist.items(), key=lambda x: -x[1])
        ],
        "risks_by_department": risks_by_department,
        "risks_by_requester": risks_by_requester,
        "task_performance": task_performance,
        "recent_gaps": overview.get("recent_gaps") or [],
        "upcoming_reviews": overview.get("upcoming_reviews") or [],
        "gap_aging": _gap_aging_buckets(gaps),
        "document_metrics": overview.get("document_metrics") or {},
    }
