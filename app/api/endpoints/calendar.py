"""Calendar API — task due dates as calendar events (Pro + Enterprise)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.tasks import _get_workspace_context, _plan_has_tasks, _task_visible_to_user
from app.core.permissions import FeatureGate
from app.db.database import get_db
from app.db.models import Organization, PlanType, User, WorkspaceTask, WorkspaceTaskStatus

router = APIRouter()


def _parse_due_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _event_status(task: WorkspaceTask, today: date) -> str:
    if task.status == WorkspaceTaskStatus.COMPLETED.value:
        return "completed"
    due = _parse_due_date(task.due_date)
    if not due:
        return "normal"
    if task.status != WorkspaceTaskStatus.COMPLETED.value and due < today:
        return "urgent"
    days = (due - today).days
    if days <= 3:
        return "urgent"
    if days <= 7:
        return "upcoming"
    return "normal"


def _task_to_calendar_event(task: WorkspaceTask, today: date) -> dict:
    due = _parse_due_date(task.due_date) or today
    status = _event_status(task, today)
    return {
        "id": str(task.id),
        "task_id": str(task.id),
        "title": task.title,
        "date": due.isoformat(),
        "type": "deadline",
        "category": task.category or "General",
        "status": status,
        "priority": task.priority,
        "task_status": task.status,
        "assignee_name": (
            f"{task.assignee.first_name} {task.assignee.last_name}".strip()
            if task.assignee
            else None
        ),
    }


@router.get("/events", response_model=dict)
async def list_calendar_events(
    year: int = Query(..., ge=2020, le=2100),
    month: int = Query(..., ge=1, le=12),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Calendar events from workspace tasks with due dates (current org)."""
    org, _seat, user_role = await _get_workspace_context(current_user, db)

    if not FeatureGate.has_feature(org.subscription.plan_type if org.subscription else PlanType.STANDARD, "calendar"):
        raise HTTPException(
            status_code=403,
            detail="Calendar is not available on your current plan.",
        )
    if not _plan_has_tasks(org):
        raise HTTPException(
            status_code=403,
            detail="Tasks are required for calendar events. Upgrade to Pro or Enterprise.",
        )

    month_start = date(year, month, 1)
    if month == 12:
        month_end = date(year + 1, 1, 1)
    else:
        month_end = date(year, month + 1, 1)

    result = await db.execute(
        select(WorkspaceTask)
        .where(
            WorkspaceTask.organization_id == org.id,
            WorkspaceTask.due_date.isnot(None),
        )
        .options(
            selectinload(WorkspaceTask.assignee),
            selectinload(WorkspaceTask.creator),
        )
    )
    all_tasks = result.scalars().all()
    today = datetime.utcnow().date()

    events = []
    for task in all_tasks:
        if not _task_visible_to_user(task, current_user.id, user_role):
            continue
        due = _parse_due_date(task.due_date)
        if not due or due < month_start or due >= month_end:
            continue
        events.append(_task_to_calendar_event(task, today))

    events.sort(key=lambda e: (e["date"], e["title"]))

    completed_count = sum(1 for e in events if e["status"] == "completed")
    urgent_count = sum(1 for e in events if e["status"] == "urgent")
    upcoming_count = sum(1 for e in events if e["status"] == "upcoming")

    return {
        "events": events,
        "year": year,
        "month": month,
        "counts": {
            "total": len(events),
            "urgent": urgent_count,
            "upcoming": upcoming_count,
            "completed": completed_count,
        },
    }
