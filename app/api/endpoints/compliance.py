"""Compliance dashboard API — overview, frameworks, gaps (Pro + Enterprise)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
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
    status: Optional[str] = "open",
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org, _seat = await _get_workspace(current_user, db)
    _require_compliance_plan(org)

    q = select(ComplianceGap).where(ComplianceGap.organization_id == org.id)
    if status:
        q = q.where(ComplianceGap.status == status)
    result = await db.execute(q.order_by(ComplianceGap.created_at.desc()))
    gaps = result.scalars().all()
    return {
        "gaps": [
            {
                "id": str(g.id),
                "title": g.title,
                "framework_slug": g.framework_slug,
                "severity": g.severity,
                "status": g.status,
                "category": g.category,
                "days_open": max(0, (g.updated_at - g.created_at).days),
            }
            for g in gaps
        ]
    }


@router.post("/gaps", status_code=status.HTTP_201_CREATED)
async def create_gap(
    data: GapCreate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org, seat = await _get_workspace(current_user, db)
    _require_compliance_plan(org)
    if seat.role not in (UserRole.ADMIN, UserRole.REVIEWER) and not PermissionChecker.can_administrate(
        seat.role
    ):
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
    )
    db.add(gap)
    await db.commit()
    await db.refresh(gap)
    await build_compliance_overview(db, org, persist_snapshot=True)
    return {"gap": {"id": str(gap.id), "title": gap.title}}


@router.patch("/gaps/{gap_id}")
async def update_gap(
    gap_id: str,
    data: GapUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org, seat = await _get_workspace(current_user, db)
    _require_compliance_plan(org)
    if seat.role not in (UserRole.ADMIN, UserRole.REVIEWER) and not PermissionChecker.can_administrate(
        seat.role
    ):
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
            from datetime import datetime

            gap.resolved_at = datetime.utcnow()
    await db.commit()
    await build_compliance_overview(db, org, persist_snapshot=True)
    return {"gap": {"id": str(gap.id), "status": gap.status}}
