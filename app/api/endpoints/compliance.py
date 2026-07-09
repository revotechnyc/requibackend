"""Compliance dashboard API — overview, frameworks, gaps (Pro + Enterprise)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.endpoints.auth import get_current_active_user
from app.core.permissions import FeatureGate, PermissionChecker
from app.db.database import get_db
from app.db.models import (
    ComplianceFramework,
    ComplianceGap,
    Organization,
    PlanType,
    Seat,
    User,
    UserRole,
)
from app.services.compliance_gap_helpers import gap_to_dict
from app.services.compliance_service import (
    FRAMEWORK_CATALOG,
    build_compliance_overview,
    ensure_default_frameworks,
    framework_limit_for_plan,
)

router = APIRouter()


class FrameworkCreate(BaseModel):
    slug: str = Field(..., min_length=2, max_length=64)
    name: Optional[str] = None


class GapCreate(BaseModel):
    framework_slug: str
    title: str = Field(..., min_length=3, max_length=500)
    description: Optional[str] = None
    severity: str = Field(default="medium", pattern="^(critical|high|medium|low)$")
    category: str = "General"


class GapUpdate(BaseModel):
    status: Optional[str] = Field(default=None, pattern="^(open|resolved)$")
    severity: Optional[str] = Field(default=None, pattern="^(critical|high|medium|low)$")


class BulkGapResolve(BaseModel):
    gap_ids: list[str] = Field(..., min_length=1, max_length=200)


async def _get_workspace(user: User, db: AsyncSession) -> tuple[Organization, Seat]:
    result = await db.execute(
        select(Seat)
        .where(Seat.user_id == user.id, Seat.is_active == True)
        .options(
            selectinload(Seat.organization).selectinload(Organization.subscription),
        )
    )
    seat = result.scalar_one_or_none()
    if not seat:
        raise HTTPException(status_code=403, detail="No active workspace")
    return seat.organization, seat


def _require_compliance_plan(org: Organization) -> PlanType:
    plan = org.subscription.plan_type if org.subscription else PlanType.STANDARD
    if not FeatureGate.has_feature(plan, "compliance"):
        raise HTTPException(
            status_code=403,
            detail="Compliance dashboard requires Pro or Enterprise plan.",
        )
    return plan


def _can_manage_gaps(seat: Seat) -> bool:
    return seat.role in (UserRole.ADMIN, UserRole.REVIEWER) or PermissionChecker.can_administrate(
        seat.role
    )


async def _gap_status_counts(db: AsyncSession, org_id: uuid.UUID) -> dict[str, int]:
    result = await db.execute(
        select(ComplianceGap.status, func.count())
        .where(ComplianceGap.organization_id == org_id)
        .group_by(ComplianceGap.status)
    )
    by_status = {row[0]: int(row[1]) for row in result.all()}
    open_count = by_status.get("open", 0)
    resolved_count = by_status.get("resolved", 0)
    return {
        "open": open_count,
        "resolved": resolved_count,
        "all": open_count + resolved_count,
    }


async def _resolve_gaps_for_org(
    db: AsyncSession,
    org: Organization,
    gap_ids: list[str],
) -> list[ComplianceGap]:
    parsed_ids: list[uuid.UUID] = []
    for gap_id in gap_ids:
        try:
            parsed_ids.append(uuid.UUID(gap_id))
        except ValueError:
            continue

    if not parsed_ids:
        raise HTTPException(status_code=400, detail="No valid gap IDs provided")

    result = await db.execute(
        select(ComplianceGap).where(
            ComplianceGap.organization_id == org.id,
            ComplianceGap.id.in_(parsed_ids),
            ComplianceGap.status == "open",
        )
    )
    gaps = result.scalars().all()
    if not gaps:
        raise HTTPException(status_code=404, detail="No open gaps found for the given IDs")

    now = datetime.utcnow()
    for gap in gaps:
        gap.status = "resolved"
        gap.resolved_at = now

    await db.commit()
    await build_compliance_overview(db, org, persist_snapshot=True)
    return gaps


@router.get("/overview")
async def get_compliance_overview(
    refresh: bool = False,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Aggregated compliance dashboard (scores, categories, gaps, calendar hints)."""
    org, _seat = await _get_workspace(current_user, db)
    _require_compliance_plan(org)
    return await build_compliance_overview(db, org, persist_snapshot=True)


@router.get("/frameworks")
async def list_frameworks(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org, _seat = await _get_workspace(current_user, db)
    plan = _require_compliance_plan(org)
    await ensure_default_frameworks(db, org.id)
    await db.commit()

    result = await db.execute(
        select(ComplianceFramework).where(
            ComplianceFramework.organization_id == org.id,
            ComplianceFramework.is_active == True,
        )
    )
    frameworks = result.scalars().all()
    return {
        "frameworks": [
            {
                "id": str(f.id),
                "slug": f.slug,
                "name": f.name,
                "score": float(f.score) if f.score is not None else None,
            }
            for f in frameworks
        ],
        "limit": framework_limit_for_plan(plan),
        "count": len(frameworks),
        "catalog": [{"slug": k, "name": v} for k, v in FRAMEWORK_CATALOG.items()],
    }


@router.post("/frameworks", status_code=status.HTTP_201_CREATED)
async def add_framework(
    data: FrameworkCreate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org, seat = await _get_workspace(current_user, db)
    plan = _require_compliance_plan(org)
    if not PermissionChecker.can_administrate(seat.role):
        raise HTTPException(status_code=403, detail="Only administrators can manage frameworks")

    slug = data.slug.strip().lower().replace(" ", "_")
    if slug not in FRAMEWORK_CATALOG:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown framework. Choose from: {', '.join(FRAMEWORK_CATALOG.keys())}",
        )

    limit = framework_limit_for_plan(plan)
    count_result = await db.execute(
        select(ComplianceFramework).where(
            ComplianceFramework.organization_id == org.id,
            ComplianceFramework.is_active == True,
        )
    )
    active_count = len(count_result.scalars().all())
    if limit is not None and active_count >= limit:
        raise HTTPException(
            status_code=403,
            detail=f"Pro plan allows up to {limit} active frameworks. Upgrade to Enterprise for unlimited.",
        )

    existing = await db.execute(
        select(ComplianceFramework).where(
            ComplianceFramework.organization_id == org.id,
            ComplianceFramework.slug == slug,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Framework already active in library")

    fw = ComplianceFramework(
        organization_id=org.id,
        slug=slug,
        name=data.name or FRAMEWORK_CATALOG[slug],
        score=75.0,
        is_active=True,
    )
    db.add(fw)
    await db.commit()
    await db.refresh(fw)
    return {"framework": {"id": str(fw.id), "slug": fw.slug, "name": fw.name, "score": float(fw.score)}}


@router.delete("/frameworks/{framework_id}")
async def remove_framework(
    framework_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org, seat = await _get_workspace(current_user, db)
    _require_compliance_plan(org)
    if not PermissionChecker.can_administrate(seat.role):
        raise HTTPException(status_code=403, detail="Only administrators can manage frameworks")

    result = await db.execute(
        select(ComplianceFramework).where(
            ComplianceFramework.id == framework_id,
            ComplianceFramework.organization_id == org.id,
        )
    )
    fw = result.scalar_one_or_none()
    if not fw:
        raise HTTPException(status_code=404, detail="Framework not found")
    fw.is_active = False
    await db.commit()
    return {"message": "Framework removed from library", "slug": fw.slug}


@router.get("/gaps")
async def list_gaps(
    status: Optional[str] = Query(default="open", pattern="^(open|resolved|all)$"),
    search: Optional[str] = Query(default=None, max_length=200),
    sort_by: Literal[
        "created_at", "resolved_at", "title", "task_name", "source_label", "severity", "status"
    ] = Query(default="created_at"),
    sort_order: Literal["asc", "desc"] = Query(default="desc"),
    framework_slug: Optional[str] = Query(default=None, max_length=64),
    source_type: Optional[str] = Query(default=None, max_length=40),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org, _seat = await _get_workspace(current_user, db)
    _require_compliance_plan(org)

    q = select(ComplianceGap).where(ComplianceGap.organization_id == org.id)
    if status and status != "all":
        q = q.where(ComplianceGap.status == status)
    if framework_slug:
        q = q.where(ComplianceGap.framework_slug == framework_slug.strip().lower())
    if source_type:
        q = q.where(ComplianceGap.source_type == source_type.strip().lower())
    if search:
        term = f"%{search.strip()}%"
        q = q.where(
            or_(
                ComplianceGap.title.ilike(term),
                ComplianceGap.source_label.ilike(term),
                ComplianceGap.task_name.ilike(term),
                ComplianceGap.contract_name.ilike(term),
                ComplianceGap.project_name.ilike(term),
                ComplianceGap.category.ilike(term),
            )
        )

    sort_columns = {
        "created_at": ComplianceGap.created_at,
        "resolved_at": ComplianceGap.resolved_at,
        "title": ComplianceGap.title,
        "task_name": ComplianceGap.task_name,
        "source_label": ComplianceGap.source_label,
        "severity": ComplianceGap.severity,
        "status": ComplianceGap.status,
    }
    sort_col = sort_columns.get(sort_by, ComplianceGap.created_at)
    q = q.order_by(sort_col.asc() if sort_order == "asc" else sort_col.desc())

    result = await db.execute(q.offset(offset).limit(limit))
    gaps = result.scalars().all()

    count_q = select(func.count()).select_from(ComplianceGap).where(
        ComplianceGap.organization_id == org.id
    )
    if status and status != "all":
        count_q = count_q.where(ComplianceGap.status == status)
    if framework_slug:
        count_q = count_q.where(ComplianceGap.framework_slug == framework_slug.strip().lower())
    if source_type:
        count_q = count_q.where(ComplianceGap.source_type == source_type.strip().lower())
    if search:
        term = f"%{search.strip()}%"
        count_q = count_q.where(
            or_(
                ComplianceGap.title.ilike(term),
                ComplianceGap.source_label.ilike(term),
                ComplianceGap.task_name.ilike(term),
                ComplianceGap.contract_name.ilike(term),
                ComplianceGap.project_name.ilike(term),
                ComplianceGap.category.ilike(term),
            )
        )
    total = int((await db.execute(count_q)).scalar_one() or 0)
    counts = await _gap_status_counts(db, org.id)

    return {
        "gaps": [
            gap_to_dict(
                g,
                framework_name=FRAMEWORK_CATALOG.get(
                    g.framework_slug, g.framework_slug.replace("_", " ").title()
                ),
            )
            for g in gaps
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
        "counts": counts,
    }


@router.post("/gaps", status_code=status.HTTP_201_CREATED)
async def create_gap(
    data: GapCreate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org, seat = await _get_workspace(current_user, db)
    _require_compliance_plan(org)
    if not _can_manage_gaps(seat):
        raise HTTPException(status_code=403, detail="Insufficient permissions to create gaps")

    slug = data.framework_slug.strip().lower()
    gap = ComplianceGap(
        organization_id=org.id,
        framework_slug=slug,
        title=data.title,
        description=data.description,
        severity=data.severity,
        category=data.category,
        status="open",
        source_type="manual",
        source_label="Manual entry",
    )
    db.add(gap)
    await db.commit()
    await db.refresh(gap)
    await build_compliance_overview(db, org, persist_snapshot=True)
    return {"gap": {"id": str(gap.id), "title": gap.title}}


@router.post("/gaps/bulk-resolve")
async def bulk_resolve_gaps(
    data: BulkGapResolve,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org, seat = await _get_workspace(current_user, db)
    _require_compliance_plan(org)
    if not _can_manage_gaps(seat):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    gaps = await _resolve_gaps_for_org(db, org, data.gap_ids)
    return {
        "resolved_count": len(gaps),
        "gap_ids": [str(g.id) for g in gaps],
    }


@router.patch("/gaps/{gap_id}")
async def update_gap(
    gap_id: str,
    data: GapUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org, seat = await _get_workspace(current_user, db)
    _require_compliance_plan(org)
    if not _can_manage_gaps(seat):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    result = await db.execute(
        select(ComplianceGap).where(
            ComplianceGap.id == gap_id,
            ComplianceGap.organization_id == org.id,
        )
    )
    gap = result.scalar_one_or_none()
    if not gap:
        raise HTTPException(status_code=404, detail="Gap not found")

    if data.severity:
        gap.severity = data.severity
    if data.status:
        gap.status = data.status
        if data.status == "resolved":
            gap.resolved_at = datetime.utcnow()
    await db.commit()
    await build_compliance_overview(db, org, persist_snapshot=True)
    return {"gap": {"id": str(gap.id), "status": gap.status}}
