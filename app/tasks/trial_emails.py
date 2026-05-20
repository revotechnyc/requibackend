"""
Celery Beat: trial expiry reminder emails (2 days before trial_end by default).
"""

import asyncio
import logging

from celery import shared_task

from app.db.database import get_db_context
from app.services.trial_reminder_service import process_trial_two_day_reminders

logger = logging.getLogger(__name__)


@shared_task(name="app.tasks.trial_emails.send_trial_two_day_reminders")
def send_trial_two_day_reminders():
    """Run daily (9:00 AM Pacific by default) — email trialing org owners with 2 days left."""

    async def _run():
        async with get_db_context() as db:
            return await process_trial_two_day_reminders(db)

    try:
        return asyncio.run(_run())
    except Exception:
        logger.exception("Trial reminder Celery task failed")
        raise
