"""Celery Beat: task due-date reminder emails and in-app notifications."""

import asyncio
import logging

from celery import shared_task

from app.db.database import get_db_context
from app.services.task_reminder_service import process_task_reminders

logger = logging.getLogger(__name__)


@shared_task(name="app.tasks.task_reminders.send_task_due_reminders")
def send_task_due_reminders():
    """Run daily — notify assignees about upcoming, due-today, and overdue tasks."""

    async def _run():
        async with get_db_context() as db:
            return await process_task_reminders(db)

    try:
        return asyncio.run(_run())
    except Exception:
        logger.exception("Task reminder Celery task failed")
        raise
