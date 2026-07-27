"""
Analytics API — org operational intelligence for the Analytics tab.
Enterprise feature (PLAN_FEATURES.analytics).
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.endpoints.auth import get_current_active_user
from app.core.permissions import FeatureGate
from app.db.database import get_db
from app.db.models import Organization, PlanType, Seat, User
from app.services.analytics_service import build_analytics_overview

router = APIRouter()


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


def _require_analytics_plan(org: Organization) -> PlanType:
    plan = org.subscription.plan_type if org.subscription else PlanType.STANDARD
    if not FeatureGate.has_feature(plan, "analytics"):
        raise HTTPException(
            status_code=403,
            detail="Analytics requires an Enterprise plan.",
        )
    return plan


@router.get("/overview")
async def get_analytics_overview(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Organization-wide analytics: compliance/risk scores, gaps, department &
    requester risk breakdowns, task performance, audit readiness.
    """
    org, _seat = await _get_workspace(current_user, db)
    _require_analytics_plan(org)
    return await build_analytics_overview(db, org)
