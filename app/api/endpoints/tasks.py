"""
Tasks API — compliance task lifecycle with plan-aware restrictions.
Pro: single-owner tasks (no assignment / approval workflow).
Enterprise: assignee + reviewer + approver workflow.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime
from enum import Enum
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.endpoints.auth import get_current_active_user
from app.core.permissions import FeatureGate, PLAN_FEATURES
from app.db.database import get_db
from app.db.models import (
    Organization,
    PlanType,
    Seat,
    Subscription,
    User,
    UserRole,
    WorkspaceTask,
    WorkspaceTaskStatus,
)

router = APIRouter()


class TaskStatus(str, Enum):
    PENDING = WorkspaceTaskStatus.PENDING.value
    IN_PROGRESS = WorkspaceTaskStatus.IN_PROGRESS.value
    SUBMITTED_FOR_REVIEW = WorkspaceTaskStatus.SUBMITTED_FOR_REVIEW.value
    REVIEWED = WorkspaceTaskStatus.REVIEWED.value
    APPROVED = WorkspaceTaskStatus.APPROVED.value
    REJECTED = WorkspaceTaskStatus.REJECTED.value
    COMPLETED = WorkspaceTaskStatus.COMPLETED.value


class TaskPriority(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TaskCreate(BaseModel):
    title: str
    description: str = ""
    priority: str = "medium"
    category: str = "General"
    due_date: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    assignee_id: Optional[str] = None
    reviewer_id: Optional[str] = None
    approver_id: Optional[str] = None


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[str] = None
    category: Optional[str] = None
    due_date: Optional[str] = None
    tags: Optional[List[str]] = None
    assignee_id: Optional[str] = None
    reviewer_id: Optional[str] = None
    approver_id: Optional[str] = None


class TaskStatusTransition(BaseModel):
    status: str
    comment: Optional[str] = None


VALID_TRANSITIONS = {
    TaskStatus.PENDING: [TaskStatus.IN_PROGRESS],
    TaskStatus.IN_PROGRESS: [TaskStatus.SUBMITTED_FOR_REVIEW, TaskStatus.COMPLETED],
    TaskStatus.SUBMITTED_FOR_REVIEW: [TaskStatus.REVIEWED, TaskStatus.REJECTED],
    TaskStatus.REVIEWED: [TaskStatus.APPROVED, TaskStatus.REJECTED],
    TaskStatus.APPROVED: [TaskStatus.COMPLETED],
    TaskStatus.REJECTED: [TaskStatus.IN_PROGRESS],
    TaskStatus.COMPLETED: [],
}

REVIEWER_PICK_ROLES = {UserRole.REVIEWER.value, UserRole.ADMIN.value}
APPROVER_PICK_ROLES = {UserRole.APPROVER.value, UserRole.ADMIN.value}
ASSIGNEE_PICK_ROLES = {
    UserRole.ADMIN.value,
    UserRole.REVIEWER.value,
    UserRole.APPROVER.value,
    UserRole.CONTRIBUTOR.value,
    UserRole.SEO.value,
}


def _role_value(role: UserRole | str) -> str:
    return role.value if isinstance(role, UserRole) else str(role).lower()


def _user_display(user: Optional[User]) -> Optional[str]:
    if not user:
        return None
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    return name or user.email


def _is_enterprise(org: Organization) -> bool:
    if not org.subscription:
        return False
    return org.subscription.plan_type == PlanType.ENTERPRISE


def _plan_has_tasks(org: Organization) -> bool:
    if not org.subscription:
        return False
    plan = org.subscription.plan_type
    return FeatureGate.has_feature(plan, "tasks") or "tasks" in PLAN_FEATURES.get(plan, [])


async def _get_workspace_context(user: User, db: AsyncSession) -> tuple[Organization, Seat, UserRole]:
    result = await db.execute(
        select(Seat)
        .where(Seat.user_id == user.id, Seat.is_active == True)
        .options(selectinload(Seat.organization).selectinload(Organization.subscription))
        .order_by(Seat.created_at.desc())
    )
    seats = result.scalars().all()
    if not seats:
        raise HTTPException(status_code=403, detail="No active workspace")

    seat = seats[0]
    org = seat.organization
    if not _plan_has_tasks(org):
        raise HTTPException(
            status_code=403,
            detail="Tasks are not available on your current plan. Upgrade to Pro or Enterprise.",
        )
    return org, seat, seat.role


async def _get_task_for_org(
    task_id: str,
    org_id: UUID,
    db: AsyncSession,
) -> WorkspaceTask:
    try:
        tid = uuid_lib.UUID(task_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Task not found")

    result = await db.execute(
        select(WorkspaceTask)
        .where(WorkspaceTask.id == tid, WorkspaceTask.organization_id == org_id)
        .options(
            selectinload(WorkspaceTask.creator),
            selectinload(WorkspaceTask.assignee),
            selectinload(WorkspaceTask.reviewer),
            selectinload(WorkspaceTask.approver),
        )
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


async def _resolve_org_member(
    org_id: UUID,
    user_id: Optional[str],
    allowed_roles: set[str],
    db: AsyncSession,
    field_label: str,
) -> Optional[UUID]:
    if not user_id:
        return None
    try:
        uid = uuid_lib.UUID(user_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail=f"Invalid {field_label}")

    result = await db.execute(
        select(Seat).where(
            Seat.organization_id == org_id,
            Seat.user_id == uid,
            Seat.is_active == True,
        )
    )
    seat = result.scalar_one_or_none()
    if not seat:
        raise HTTPException(status_code=400, detail=f"{field_label} is not an active workspace member")
    if _role_value(seat.role) not in allowed_roles:
        raise HTTPException(
            status_code=400,
            detail=f"Selected {field_label} must have one of: {', '.join(sorted(allowed_roles))}",
        )
    return uid


def _serialize_task(task: WorkspaceTask) -> dict:
    return {
        "id": str(task.id),
        "workspace_id": str(task.organization_id),
        "title": task.title,
        "description": task.description or "",
        "status": task.status,
        "priority": task.priority,
        "category": task.category,
        "due_date": task.due_date,
        "tags": task.tags or [],
        "creator_id": str(task.creator_id),
        "creator_name": _user_display(task.creator),
        "assignee_id": str(task.assignee_id) if task.assignee_id else None,
        "assignee_name": _user_display(task.assignee),
        "reviewer_id": str(task.reviewer_id) if task.reviewer_id else None,
        "reviewer_name": _user_display(task.reviewer),
        "approver_id": str(task.approver_id) if task.approver_id else None,
        "approver_name": _user_display(task.approver),
        "comments": task.comments or [],
        "history": task.history or [],
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }


def _can_user_review(task: WorkspaceTask, user: User, user_role: UserRole) -> bool:
    if user_role == UserRole.ADMIN:
        return True
    if user_role != UserRole.REVIEWER:
        return False
    if task.reviewer_id and task.reviewer_id != user.id:
        return False
    return True


def _can_user_approve(task: WorkspaceTask, user: User, user_role: UserRole) -> bool:
    if user_role == UserRole.ADMIN:
        return True
    if user_role != UserRole.APPROVER:
        return False
    if task.approver_id and task.approver_id != user.id:
        return False
    return True


def _task_visible_to_user(task: WorkspaceTask, user_id: UUID, user_role: UserRole) -> bool:
    if user_role in (UserRole.ADMIN, UserRole.REVIEWER, UserRole.APPROVER, UserRole.VIEWER, UserRole.SEO):
        return True
    if user_role == UserRole.CONTRIBUTOR:
        return task.creator_id == user_id or task.assignee_id == user_id
    return False


@router.get("/eligible-members", response_model=dict)
async def list_eligible_members(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Active workspace members for task assignment dropdowns (Enterprise)."""
    org, _seat, user_role = await _get_workspace_context(current_user, db)

    if user_role in (UserRole.VIEWER, UserRole.REVIEWER, UserRole.APPROVER):
        raise HTTPException(status_code=403, detail="Your role cannot manage task assignments")

    if not _is_enterprise(org):
        return {"assignees": [], "reviewers": [], "approvers": [], "is_enterprise": False}

    result = await db.execute(
        select(Seat)
        .where(Seat.organization_id == org.id, Seat.is_active == True)
        .options(selectinload(Seat.user))
    )
    seats = result.scalars().all()

    def _member_row(seat: Seat) -> dict:
        u = seat.user
        return {
            "id": str(u.id),
            "email": u.email,
            "name": _user_display(u),
            "role": _role_value(seat.role),
        }

    assignees = [_member_row(s) for s in seats if _role_value(s.role) in ASSIGNEE_PICK_ROLES]
    reviewers = [_member_row(s) for s in seats if _role_value(s.role) in REVIEWER_PICK_ROLES]
    approvers = [_member_row(s) for s in seats if _role_value(s.role) in APPROVER_PICK_ROLES]

    return {
        "assignees": assignees,
        "reviewers": reviewers,
        "approvers": approvers,
        "is_enterprise": True,
    }


@router.post("/", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_task(
    data: TaskCreate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org, seat, user_role = await _get_workspace_context(current_user, db)

    if user_role == UserRole.VIEWER:
        raise HTTPException(status_code=403, detail="View-Only users cannot create tasks")
    if user_role in (UserRole.REVIEWER, UserRole.APPROVER):
        raise HTTPException(status_code=403, detail="Your role cannot create tasks")

    enterprise = _is_enterprise(org)
    if not enterprise and (data.assignee_id or data.reviewer_id or data.approver_id):
        raise HTTPException(
            status_code=400,
            detail="Task assignment and approval workflows require Enterprise plan",
        )

    assignee_id = None
    reviewer_id = None
    approver_id = None
    if enterprise:
        assignee_id = await _resolve_org_member(
            org.id, data.assignee_id, ASSIGNEE_PICK_ROLES, db, "assignee"
        )
        reviewer_id = await _resolve_org_member(
            org.id, data.reviewer_id, REVIEWER_PICK_ROLES, db, "reviewer"
        )
        approver_id = await _resolve_org_member(
            org.id, data.approver_id, APPROVER_PICK_ROLES, db, "approver"
        )

    now = datetime.utcnow()
    history = [
        {
            "status": TaskStatus.PENDING.value,
            "by": str(current_user.id),
            "at": now.isoformat(),
            "note": "Task created",
        }
    ]

    task = WorkspaceTask(
        organization_id=org.id,
        creator_id=current_user.id,
        assignee_id=assignee_id,
        reviewer_id=reviewer_id,
        approver_id=approver_id,
        title=data.title.strip(),
        description=data.description or "",
        status=TaskStatus.PENDING.value,
        priority=(data.priority or "medium").lower(),
        category=data.category or "General",
        due_date=data.due_date,
        tags=data.tags or [],
        comments=[],
        history=history,
        created_at=now,
        updated_at=now,
    )
    db.add(task)
    await db.flush()
    await db.refresh(task, ["creator", "assignee", "reviewer", "approver"])

    return {"task": _serialize_task(task), "message": "Task created"}


@router.get("/", response_model=dict)
async def list_tasks(
    status_filter: Optional[str] = None,
    priority: Optional[str] = None,
    assignee: Optional[str] = None,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org, _seat, user_role = await _get_workspace_context(current_user, db)
    user_id = current_user.id

    query = (
        select(WorkspaceTask)
        .where(WorkspaceTask.organization_id == org.id)
        .options(
            selectinload(WorkspaceTask.creator),
            selectinload(WorkspaceTask.assignee),
            selectinload(WorkspaceTask.reviewer),
            selectinload(WorkspaceTask.approver),
        )
        .order_by(desc(WorkspaceTask.created_at))
    )
    result = await db.execute(query)
    all_tasks = result.scalars().all()

    tasks = [t for t in all_tasks if _task_visible_to_user(t, user_id, user_role)]

    if status_filter:
        sf = status_filter.lower().replace(" ", "_")
        if sf == "submitted":
            sf = TaskStatus.SUBMITTED_FOR_REVIEW.value
        tasks = [t for t in tasks if t.status == sf]
    if priority:
        tasks = [t for t in tasks if t.priority == priority.lower()]
    if assignee:
        tasks = [t for t in tasks if t.assignee_id and str(t.assignee_id) == assignee]

    serialized = [_serialize_task(t) for t in tasks]

    counts = {s.value: 0 for s in TaskStatus}
    for t in all_tasks:
        if t.status in counts:
            counts[t.status] += 1

    enterprise = _is_enterprise(org)
    return {
        "tasks": serialized,
        "counts": counts,
        "can_create": user_role not in (UserRole.VIEWER, UserRole.REVIEWER, UserRole.APPROVER),
        "can_review": user_role in (UserRole.ADMIN, UserRole.REVIEWER),
        "can_approve": user_role in (UserRole.ADMIN, UserRole.APPROVER),
        "is_enterprise": enterprise,
    }


@router.get("/{task_id}", response_model=dict)
async def get_task(
    task_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org, _seat, user_role = await _get_workspace_context(current_user, db)
    task = await _get_task_for_org(task_id, org.id, db)

    if not _task_visible_to_user(task, current_user.id, user_role):
        raise HTTPException(status_code=403, detail="You do not have access to this task")

    is_owner = task.creator_id == current_user.id
    return {
        "task": _serialize_task(task),
        "permissions": {
            "can_edit": user_role in (UserRole.ADMIN, UserRole.CONTRIBUTOR) and (
                user_role == UserRole.ADMIN or is_owner
            ),
            "can_review": _can_user_review(task, current_user, user_role),
            "can_approve": _can_user_approve(task, current_user, user_role),
            "can_delete": user_role == UserRole.ADMIN,
            "can_submit": user_role != UserRole.VIEWER,
            "is_enterprise": _is_enterprise(org),
        },
    }


@router.patch("/{task_id}/status", response_model=dict)
async def transition_task_status(
    task_id: str,
    data: TaskStatusTransition,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org, _seat, user_role = await _get_workspace_context(current_user, db)
    task = await _get_task_for_org(task_id, org.id, db)

    if not _task_visible_to_user(task, current_user.id, user_role):
        raise HTTPException(status_code=403, detail="You do not have access to this task")

    try:
        target_status = TaskStatus(data.status.lower())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Valid: {[s.value for s in TaskStatus]}",
        )

    current_status = TaskStatus(task.status)
    allowed = list(VALID_TRANSITIONS.get(current_status, []))
    enterprise = _is_enterprise(org)

    if (
        current_status == TaskStatus.IN_PROGRESS
        and target_status == TaskStatus.COMPLETED
        and enterprise
    ):
        raise HTTPException(
            status_code=400,
            detail="Enterprise tasks must go through review and approval before completion",
        )
    if (
        current_status == TaskStatus.IN_PROGRESS
        and target_status == TaskStatus.SUBMITTED_FOR_REVIEW
        and not enterprise
    ):
        raise HTTPException(
            status_code=400,
            detail="Submit for review requires Enterprise plan. Mark complete instead.",
        )

    if target_status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid transition: {current_status.value} -> {target_status.value}. "
            f"Allowed: {[s.value for s in allowed]}",
        )

    if target_status == TaskStatus.SUBMITTED_FOR_REVIEW:
        if user_role == UserRole.VIEWER:
            raise HTTPException(status_code=403, detail="View-Only users cannot submit for review")

    if target_status == TaskStatus.REVIEWED:
        if not _can_user_review(task, current_user, user_role):
            raise HTTPException(
                status_code=403,
                detail="Only the assigned reviewer or admin can complete review",
            )

    if target_status == TaskStatus.REJECTED and current_status == TaskStatus.SUBMITTED_FOR_REVIEW:
        if not _can_user_review(task, current_user, user_role):
            raise HTTPException(status_code=403, detail="Only reviewer or admin can reject at review stage")

    if target_status in (TaskStatus.APPROVED, TaskStatus.REJECTED) and current_status == TaskStatus.REVIEWED:
        if not _can_user_approve(task, current_user, user_role):
            raise HTTPException(
                status_code=403,
                detail="Only the assigned approver or admin can approve or reject",
            )

    now = datetime.utcnow()
    history = list(task.history or [])
    history.append(
        {
            "from": current_status.value,
            "to": target_status.value,
            "by": str(current_user.id),
            "by_name": _user_display(current_user),
            "at": now.isoformat(),
            "comment": data.comment,
        }
    )
    if data.comment:
        comments = list(task.comments or [])
        comment_type = "comment"
        if target_status == TaskStatus.SUBMITTED_FOR_REVIEW:
            comment_type = "submission"
        elif target_status == TaskStatus.REVIEWED:
            comment_type = "review"
        elif target_status == TaskStatus.APPROVED:
            comment_type = "approval"
        elif target_status == TaskStatus.REJECTED:
            comment_type = "rejection"
        comments.append(
            {
                "id": f"c_{len(comments)}",
                "author": str(current_user.id),
                "author_name": _user_display(current_user),
                "text": data.comment,
                "created_at": now.isoformat(),
                "type": comment_type,
            }
        )
        task.comments = comments

    task.status = target_status.value
    task.updated_at = now
    task.history = history
    await db.flush()
    await db.refresh(task, ["creator", "assignee", "reviewer", "approver"])

    return {"task": _serialize_task(task), "message": f"Status transitioned to {target_status.value}"}


@router.patch("/{task_id}", response_model=dict)
async def update_task(
    task_id: str,
    data: TaskUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org, _seat, user_role = await _get_workspace_context(current_user, db)
    task = await _get_task_for_org(task_id, org.id, db)

    is_owner = task.creator_id == current_user.id
    if user_role == UserRole.CONTRIBUTOR and not is_owner:
        raise HTTPException(status_code=403, detail="Can only edit your own tasks")
    if user_role in (UserRole.VIEWER, UserRole.REVIEWER, UserRole.APPROVER):
        raise HTTPException(status_code=403, detail="Your role cannot edit tasks")

    enterprise = _is_enterprise(org)

    if data.title is not None:
        task.title = data.title.strip()
    if data.description is not None:
        task.description = data.description
    if data.priority is not None:
        task.priority = data.priority.lower()
    if data.category is not None:
        task.category = data.category
    if data.due_date is not None:
        task.due_date = data.due_date
    if data.tags is not None:
        task.tags = data.tags

    if enterprise:
        if data.assignee_id is not None:
            task.assignee_id = await _resolve_org_member(
                org.id, data.assignee_id or None, ASSIGNEE_PICK_ROLES, db, "assignee"
            )
        if data.reviewer_id is not None:
            task.reviewer_id = await _resolve_org_member(
                org.id, data.reviewer_id or None, REVIEWER_PICK_ROLES, db, "reviewer"
            )
        if data.approver_id is not None:
            task.approver_id = await _resolve_org_member(
                org.id, data.approver_id or None, APPROVER_PICK_ROLES, db, "approver"
            )

    task.updated_at = datetime.utcnow()
    await db.flush()
    await db.refresh(task, ["creator", "assignee", "reviewer", "approver"])
    return {"task": _serialize_task(task), "message": "Task updated"}


@router.delete("/{task_id}", response_model=dict)
async def delete_task(
    task_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org, _seat, user_role = await _get_workspace_context(current_user, db)
    if user_role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Only Admin can delete tasks")

    task = await _get_task_for_org(task_id, org.id, db)
    await db.delete(task)
    return {"message": "Task deleted"}


@router.post("/{task_id}/comments", response_model=dict)
async def add_comment(
    task_id: str,
    comment: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org, _seat, user_role = await _get_workspace_context(current_user, db)
    if user_role == UserRole.VIEWER:
        raise HTTPException(status_code=403, detail="View-Only users cannot comment")

    task = await _get_task_for_org(task_id, org.id, db)
    if not _task_visible_to_user(task, current_user.id, user_role):
        raise HTTPException(status_code=403, detail="You do not have access to this task")

    now = datetime.utcnow()
    comments = list(task.comments or [])
    comments.append(
        {
            "id": f"c_{len(comments)}",
            "author": str(current_user.id),
            "author_name": _user_display(current_user),
            "text": comment,
            "created_at": now.isoformat(),
            "type": "comment",
        }
    )
    task.comments = comments
    task.updated_at = now
    await db.flush()
    await db.refresh(task, ["creator", "assignee", "reviewer", "approver"])
    return {"task": _serialize_task(task), "message": "Comment added"}
