"""
Daily trial expiry reminder: email org owners when N calendar days remain on trialing subscriptions.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.db.models import Organization, Subscription, SubscriptionStatus, User
from app.services.email_service import send_trial_two_days_left_email

logger = logging.getLogger(__name__)

SETTINGS_SENT_KEY = "trial_reminder_2d_sent"
SETTINGS_SENT_AT_KEY = "trial_reminder_2d_sent_at"


def calendar_days_until_trial_end(trial_end: datetime) -> int:
    """UTC calendar-day difference between today and trial_end (matches usage/trial UI)."""
    return max(0, (trial_end.date() - datetime.utcnow().date()).days)


async def process_trial_two_day_reminders(db: AsyncSession) -> dict[str, Any]:
    """
    Find trialing subscriptions with exactly `trial_reminder_days_before_end` days left;
    email each organization owner once (tracked in organization.settings).
    """
    if not settings.trial_reminder_email_enabled:
        logger.info("Trial reminder emails disabled (TRIAL_REMINDER_EMAIL_ENABLED=false)")
        return {"skipped": True, "reason": "disabled"}

    if not settings.smtp_enabled:
        logger.warning("Trial reminder job skipped: SMTP not configured")
        return {"skipped": True, "reason": "smtp_not_configured"}

    target_days = settings.trial_reminder_days_before_end
    now = datetime.utcnow()

    result = await db.execute(
        select(Subscription)
        .where(
            Subscription.status == SubscriptionStatus.TRIALING,
            Subscription.trial_end.isnot(None),
            Subscription.trial_end > now,
        )
        .options(
            selectinload(Subscription.organization).selectinload(Organization.owner),
        )
    )
    subscriptions = result.scalars().all()

    stats: dict[str, Any] = {
        "checked": len(subscriptions),
        "target_days_remaining": target_days,
        "sent": 0,
        "skipped_already_sent": 0,
        "skipped_not_target_day": 0,
        "failed": 0,
        "errors": [],
    }

    for sub in subscriptions:
        org = sub.organization
        if not org or not sub.trial_end:
            continue

        days_left = calendar_days_until_trial_end(sub.trial_end)
        if days_left != target_days:
            stats["skipped_not_target_day"] += 1
            continue

        org_settings = dict(org.settings or {})
        if org_settings.get(SETTINGS_SENT_KEY):
            stats["skipped_already_sent"] += 1
            continue

        owner: User | None = org.owner
        if not owner or not owner.email:
            stats["failed"] += 1
            stats["errors"].append(f"org {org.id}: no owner email")
            continue

        ok = await send_trial_two_days_left_email(
            to_email=owner.email,
            first_name=owner.first_name or "",
            days_remaining=days_left,
            trial_end=sub.trial_end,
        )

        if ok:
            org_settings[SETTINGS_SENT_KEY] = True
            org_settings[SETTINGS_SENT_AT_KEY] = now.isoformat()
            org.settings = org_settings
            flag_modified(org, "settings")
            stats["sent"] += 1
        else:
            stats["failed"] += 1
            stats["errors"].append(f"org {org.id}: send failed for {owner.email}")

    await db.commit()
    logger.info("Trial 2-day reminder job finished: %s", stats)
    return stats
