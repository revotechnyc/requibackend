"""
Trial AI prompt usage tracking and daily limits.
"""

import uuid
from datetime import datetime

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.endpoints.auth import get_current_active_user
from app.core.config import settings
from app.db.database import get_db
from app.db.models import Organization, Seat, Subscription, SubscriptionStatus, UsageRecord, User

TRIAL_DAILY_LIMIT = settings.trial_prompt_limit


def _trial_days_remaining(trial_end: datetime) -> int:
    return max(0, (trial_end.date() - datetime.utcnow().date()).days)


async def _get_active_seat_with_subscription(
    user_id: str, db: AsyncSession
) -> Seat | None:
    result = await db.execute(
        select(Seat)
        .where(Seat.user_id == user_id, Seat.is_active == True)
        .options(selectinload(Seat.organization).selectinload(Organization.subscription))
    )
    return result.scalar_one_or_none()


def _is_trialing_subscription(sub: Subscription | None) -> bool:
    if not sub or sub.status != SubscriptionStatus.TRIALING:
        return False
    if sub.trial_end and datetime.utcnow() > sub.trial_end:
        return False
    return True


async def get_today_usage(user_id: str, db: AsyncSession) -> int:
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    result = await db.execute(
        select(UsageRecord).where(
            UsageRecord.user_id == user_id,
            UsageRecord.date >= today_start,
        )
    )
    record = result.scalar_one_or_none()
    return record.prompt_count if record else 0


async def increment_usage_if_trialing(user_id: str, db: AsyncSession) -> None:
    """Increment today's prompt count for trialing users only (caller must commit)."""
    seat = await _get_active_seat_with_subscription(user_id, db)
    if not seat or not _is_trialing_subscription(seat.organization.subscription):
        return
    await _increment_usage_record(user_id, db)
    prompts_today = await get_today_usage(user_id, db)
    try:
        from app.services.notification_service import NotificationService

        org_id = seat.organization_id if seat.organization else None
        uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
        await NotificationService(db).notify_prompt_usage(
            uid,
            org_id,
            used=prompts_today,
            limit=TRIAL_DAILY_LIMIT,
        )
    except Exception:
        pass


async def _increment_usage_record(user_id: str, db: AsyncSession) -> None:
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    result = await db.execute(
        select(UsageRecord).where(
            UsageRecord.user_id == user_id,
            UsageRecord.date >= today_start,
        )
    )
    record = result.scalar_one_or_none()
    if record:
        record.prompt_count += 1
        record.updated_at = now
    else:
        db.add(
            UsageRecord(
                user_id=user_id,
                date=now,
                prompt_count=1,
            )
        )


async def get_trial_info(user_id: str, db: AsyncSession) -> dict:
    """Usage payload for GET /ai/usage and frontend counter."""
    seat = await _get_active_seat_with_subscription(user_id, db)
    if not seat or not seat.organization or not seat.organization.subscription:
        return {
            "is_trial": False,
            "prompts_today": 0,
            "prompts_limit": None,
            "trial_days_remaining": None,
            "trial_end": None,
        }

    sub = seat.organization.subscription
    if not _is_trialing_subscription(sub):
        return {
            "is_trial": False,
            "prompts_today": 0,
            "prompts_limit": None,
            "trial_days_remaining": None,
            "trial_end": None,
        }

    prompts_today = await get_today_usage(user_id, db)
    trial_days_remaining = 0
    trial_end_iso = None
    if sub.trial_end:
        trial_days_remaining = _trial_days_remaining(sub.trial_end)
        trial_end_iso = sub.trial_end.isoformat()

    return {
        "is_trial": True,
        "prompts_today": prompts_today,
        "prompts_limit": TRIAL_DAILY_LIMIT,
        "trial_days_remaining": trial_days_remaining,
        "trial_end": trial_end_iso,
    }


async def check_trial_usage_limit(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Block Intelligence prompts when trial daily limit is reached."""
    seat = await _get_active_seat_with_subscription(str(current_user.id), db)
    if not seat or not seat.organization:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No active organization membership",
        )

    sub = seat.organization.subscription
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No active subscription",
        )

    if not _is_trialing_subscription(sub):
        return

    if sub.trial_end and datetime.utcnow() > sub.trial_end:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your trial has expired. Upgrade to access full features.",
        )

    prompts_today = await get_today_usage(str(current_user.id), db)
    if prompts_today >= TRIAL_DAILY_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate Limit Reached — you've used all 3 AI prompts for today. Upgrade to continue.",
        )
