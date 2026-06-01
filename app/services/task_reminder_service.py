"""Task due-date reminders — in-app notifications and email (Pro + Enterprise)."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.db.models import (
    NotificationType,
    Organization,
    PlanType,
    Seat,
    Subscription,
    SubscriptionStatus,
    User,
    WorkspaceTask,
    WorkspaceTaskStatus,
)
from app.services.email_service import send_task_reminder_email
from app.services.notification_service import NotificationService

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {WorkspaceTaskStatus.COMPLETED.value}
ACTIVE_PLANS = {PlanType.PRO, PlanType.ENTERPRISE}


def _parse_due_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    raw = str(value).strip()[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _days_until_due(due: date, today: date) -> int:
    return (due - today).days


def _recipient_user_ids(task: WorkspaceTask) -> list[UUID]:
    ids: list[UUID] = []
    if task.assignee_id:
        ids.append(task.assignee_id)
    elif task.creator_id:
        ids.append(task.creator_id)
    # Deduplicate while preserving order
    seen: set[UUID] = set()
    out: list[UUID] = []
    for uid in ids:
        if uid not in seen:
            seen.add(uid)
            out.append(uid)
    return out


async def _notify_user_for_task(
    db: AsyncSession,
    *,
    user: User,
    org_id: UUID,
    task: WorkspaceTask,
    notif_type: NotificationType,
    template_vars: dict[str, str],
) -> bool:
    svc = NotificationService(db)
    notif = await svc.create_notification(
        user_id=user.id,
        org_id=org_id,
        notif_type=notif_type,
        template_vars=template_vars,
        metadata={"task_id": str(task.id), "due_date": task.due_date},
        related_entity_type="workspace_task",
        related_entity_id=str(task.id),
        dedupe_same_day=True,
    )
    if not notif:
        return False

    if settings.task_reminder_email_enabled and settings.smtp_enabled:
        kind = notif_type.value.replace("task_", "")
        await send_task_reminder_email(
            to_email=user.email,
            first_name=user.first_name or "",
            task_title=task.title,
            due_date=task.due_date or "",
            reminder_kind=kind,
            task_id=str(task.id),
        )
    return True


async def process_task_reminders(db: AsyncSession) -> dict[str, Any]:
    """Scan open tasks with due dates; send due-soon, due-today, and overdue reminders."""
    if not settings.task_reminder_enabled:
        return {"skipped": True, "reason": "disabled"}

    today = datetime.utcnow().date()
    target_soon = settings.task_reminder_days_before

    result = await db.execute(
        select(WorkspaceTask)
        .where(
            WorkspaceTask.due_date.isnot(None),
            WorkspaceTask.status.notin_(list(TERMINAL_STATUSES)),
        )
        .options(
            selectinload(WorkspaceTask.assignee),
            selectinload(WorkspaceTask.creator),
            selectinload(WorkspaceTask.organization).selectinload(Organization.subscription),
        )
    )
    tasks = result.scalars().all()

    stats: dict[str, Any] = {
        "checked": len(tasks),
        "due_soon": 0,
        "due_today": 0,
        "overdue": 0,
        "skipped_plan": 0,
        "skipped_no_due": 0,
        "skipped_no_recipient": 0,
    }

    for task in tasks:
        org = task.organization
        sub = org.subscription if org else None
        if not sub or sub.plan_type not in ACTIVE_PLANS:
            stats["skipped_plan"] += 1
            continue
        if sub.status not in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING):
            stats["skipped_plan"] += 1
            continue

        due = _parse_due_date(task.due_date)
        if not due:
            stats["skipped_no_due"] += 1
            continue

        days = _days_until_due(due, today)
        if days == target_soon:
            notif_type = NotificationType.TASK_DUE_SOON
            stats_key = "due_soon"
        elif days == 0:
            notif_type = NotificationType.TASK_DUE_TODAY
            stats_key = "due_today"
        elif days < 0:
            notif_type = NotificationType.TASK_OVERDUE
            stats_key = "overdue"
        else:
            continue

        recipient_ids = _recipient_user_ids(task)
        if not recipient_ids:
            stats["skipped_no_recipient"] += 1
            continue

        template_vars = {
            "task_title": task.title,
            "due_date": due.isoformat(),
            "days_until": str(max(days, 0)),
            "task_id": str(task.id),
        }

        for uid in recipient_ids:
            user_result = await db.execute(select(User).where(User.id == uid))
            user = user_result.scalar_one_or_none()
            if not user or not user.is_active:
                continue
            sent = await _notify_user_for_task(
                db,
                user=user,
                org_id=task.organization_id,
                task=task,
                notif_type=notif_type,
                template_vars=template_vars,
            )
            if sent:
                stats[stats_key] += 1

    logger.info("Task reminders processed: %s", stats)
    return stats
