"""
Tasks API — v2.1
Full lifecycle: PENDING -> IN_PROGRESS -> SUBMITTED_FOR_REVIEW -> [APPROVED|REJECTED] -> COMPLETED
Enterprise: multi-user assignment + approval workflows
Pro: single-owner (no assignment UI)
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.endpoints.auth import get_current_active_user
from app.core.permissions import PermissionChecker
from app.db.database import get_db
from app.db.models import Organization, PlanType, Seat, Subscription, SubscriptionStatus, User, UserRole

router = APIRouter()


# ============== Enums ==============

class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUBMITTED_FOR_REVIEW = "submitted_for_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    COMPLETED = "completed"


class TaskPriority(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# ============== Pydantic Models ==============

class TaskCreate(BaseModel):
    title: str
    description: str
    priority: str = "medium"
    due_date: Optional[str] = None
    tags: List[str] = []
    assignee_id: Optional[str] = None  # Enterprise only


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[str] = None
    due_date: Optional[str] = None
    tags: Optional[List[str]] = None
    assignee_id: Optional[str] = None


class TaskStatusTransition(BaseModel):
    status: str
    comment: Optional[str] = None


# ============== In-memory store ==============
TASKS_STORE: List[dict] = []
TASK_COUNTER = 0


def _next_task_id() -> int:
    global TASK_COUNTER
    TASK_COUNTER += 1
    return TASK_COUNTER


# ============== Valid Transitions ==============
VALID_TRANSITIONS = {
    TaskStatus.PENDING: [TaskStatus.IN_PROGRESS],
    TaskStatus.IN_PROGRESS: [TaskStatus.SUBMITTED_FOR_REVIEW],
    TaskStatus.SUBMITTED_FOR_REVIEW: [TaskStatus.APPROVED, TaskStatus.REJECTED],
    TaskStatus.APPROVED: [TaskStatus.COMPLETED],
    TaskStatus.REJECTED: [TaskStatus.IN_PROGRESS],  # Return to contributor
    TaskStatus.COMPLETED: [],  # Terminal
}


# ============== Helpers ==============

async def _get_org_and_role(user: User, db: AsyncSession):
    result = await db.execute(
        select(Seat).where(Seat.user_id == user.id, Seat.is_active == True)
        .options(selectinload(Seat.organization).selectinload(Organization.subscription))
    )
    seat = result.scalar_one_or_none()
    if not seat:
        raise HTTPException(status_code=403, detail="No active workspace")
    return seat.organization, seat.role


def _is_enterprise(org: Organization) -> bool:
    if not org.subscription:
        return False
    return org.subscription.plan_type == PlanType.ENTERPRISE


# ============== ENDPOINTS ==============

@router.post("/", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_task(
    data: TaskCreate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    POST /v1/tasks
    Create a task. Enterprise supports assignment. Pro is single-owner.
    """
    org, user_role = await _get_org_and_role(current_user, db)

    # Permission check: Admin, Reviewer, Contributor can create
    if user_role == UserRole.VIEWER:
        raise HTTPException(status_code=403, detail="View-Only users cannot create tasks")

    # Pro tier: no assignment
    if not _is_enterprise(org) and data.assignee_id:
        raise HTTPException(status_code=400, detail="Task assignment requires Enterprise plan")

    task_id = _next_task_id()
    now = datetime.utcnow().isoformat()

    task = {
        "id": str(task_id),
        "workspace_id": str(org.id),
        "title": data.title,
        "description": data.description,
        "status": TaskStatus.PENDING.value,
        "priority": data.priority,
        "due_date": data.due_date,
        "tags": data.tags,
        "creator_id": str(current_user.id),
        "creator_name": f"{current_user.first_name} {current_user.last_name}".strip() or current_user.email,
        "assignee_id": data.assignee_id,
        "assignee_name": None,  # PLACEHOLDER: Resolve from DB
        "comments": [],
        "history": [{"status": TaskStatus.PENDING.value, "by": str(current_user.id), "at": now, "note": "Task created"}],
        "created_at": now,
        "updated_at": now,
    }
    TASKS_STORE.append(task)

    return {"task": task, "message": "Task created"}


@router.get("/", response_model=dict)
async def list_tasks(
    status: Optional[str] = None,
    priority: Optional[str] = None,
    assignee: Optional[str] = None,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    GET /v1/tasks
    List tasks. View-Only: view all tasks (read-only). Contributor: own tasks. Admin/Reviewer: all.
    """
    org, user_role = await _get_org_and_role(current_user, db)
    org_id = str(org.id)
    user_id = str(current_user.id)

    tasks = [t for t in TASKS_STORE if t.get("workspace_id") == org_id]

    # Role-based filtering
    if user_role == UserRole.CONTRIBUTOR:
        # Contributors see: tasks they created OR tasks assigned to them
        tasks = [t for t in tasks if t["creator_id"] == user_id or t.get("assignee_id") == user_id]
    # Admin, Reviewer, Viewer, SEO see all tasks in workspace

    if status:
        tasks = [t for t in tasks if t["status"] == status]
    if priority:
        tasks = [t for t in tasks if t["priority"] == priority]
    if assignee:
        tasks = [t for t in tasks if t.get("assignee_id") == assignee]

    tasks.sort(key=lambda x: x["created_at"], reverse=True)

    # Status counts
    counts = {s.value: len([t for t in TASKS_STORE if t.get("workspace_id") == org_id and t["status"] == s.value]) for s in TaskStatus}

    return {
        "tasks": tasks,
        "counts": counts,
        "can_create": user_role != UserRole.VIEWER,
        "can_review": user_role in [UserRole.ADMIN, UserRole.REVIEWER],
        "is_enterprise": _is_enterprise(org),
    }


@router.get("/{task_id}", response_model=dict)
async def get_task(
    task_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a single task with full history."""
    org, user_role = await _get_org_and_role(current_user, db)

    for task in TASKS_STORE:
        if task["id"] == task_id and task.get("workspace_id") == str(org.id):
            return {
                "task": task,
                "permissions": {
                    "can_edit": user_role in [UserRole.ADMIN, UserRole.CONTRIBUTOR] and task["creator_id"] == str(current_user.id),
                    "can_review": user_role in [UserRole.ADMIN, UserRole.REVIEWER],
                    "can_delete": user_role == UserRole.ADMIN,
                },
            }

    raise HTTPException(status_code=404, detail="Task not found")


@router.patch("/{task_id}/status", response_model=dict)
async def transition_task_status(
    task_id: str,
    data: TaskStatusTransition,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    PATCH /v1/tasks/{id}/status
    5-state lifecycle: PENDING -> IN_PROGRESS -> SUBMITTED_FOR_REVIEW -> [APPROVED|REJECTED] -> COMPLETED
    """
    org, user_role = await _get_org_and_role(current_user, db)

    # Validate target status
    try:
        target_status = TaskStatus(data.status.lower())
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid status. Valid: {[s.value for s in TaskStatus]}")

    for task in TASKS_STORE:
        if task["id"] == task_id and task.get("workspace_id") == str(org.id):
            current_status = TaskStatus(task["status"])

            # Check valid transition
            if target_status not in VALID_TRANSITIONS.get(current_status, []):
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid transition: {current_status.value} -> {target_status.value}. "
                           f"Allowed: {[s.value for s in VALID_TRANSITIONS.get(current_status, [])]}"
                )

            # Role permission checks for specific transitions
            if target_status == TaskStatus.SUBMITTED_FOR_REVIEW:
                if user_role == UserRole.VIEWER:
                    raise HTTPException(status_code=403, detail="View-Only users cannot submit for review")

            if target_status in [TaskStatus.APPROVED, TaskStatus.REJECTED]:
                if user_role not in [UserRole.ADMIN, UserRole.REVIEWER]:
                    raise HTTPException(status_code=403, detail="Only Admin or Reviewer can approve/reject")

            # Apply transition
            now = datetime.utcnow().isoformat()
            task["status"] = target_status.value
            task["updated_at"] = now
            task["history"].append({
                "from": current_status.value,
                "to": target_status.value,
                "by": str(current_user.id),
                "at": now,
                "comment": data.comment,
            })

            return {"task": task, "message": f"Status transitioned to {target_status.value}"}

    raise HTTPException(status_code=404, detail="Task not found")


@router.patch("/{task_id}", response_model=dict)
async def update_task(
    task_id: str,
    data: TaskUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Update task fields. Contributors can only edit their own tasks."""
    org, user_role = await _get_org_and_role(current_user, db)

    for task in TASKS_STORE:
        if task["id"] == task_id and task.get("workspace_id") == str(org.id):
            # Permission check
            is_owner = task["creator_id"] == str(current_user.id)
            if user_role == UserRole.CONTRIBUTOR and not is_owner:
                raise HTTPException(status_code=403, detail="Can only edit your own tasks")
            if user_role == UserRole.VIEWER:
                raise HTTPException(status_code=403, detail="View-Only users cannot edit tasks")

            if data.title: task["title"] = data.title
            if data.description: task["description"] = data.description
            if data.priority: task["priority"] = data.priority
            if data.due_date: task["due_date"] = data.due_date
            if data.tags: task["tags"] = data.tags
            if data.assignee_id and _is_enterprise(org):
                task["assignee_id"] = data.assignee_id

            task["updated_at"] = datetime.utcnow().isoformat()
            return {"task": task, "message": "Task updated"}

    raise HTTPException(status_code=404, detail="Task not found")


@router.delete("/{task_id}", response_model=dict)
async def delete_task(
    task_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a task. Admin only."""
    org, user_role = await _get_org_and_role(current_user, db)

    if user_role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Only Admin can delete tasks")

    for i, task in enumerate(TASKS_STORE):
        if task["id"] == task_id and task.get("workspace_id") == str(org.id):
            del TASKS_STORE[i]
            return {"message": "Task deleted"}

    raise HTTPException(status_code=404, detail="Task not found")


@router.post("/{task_id}/comments", response_model=dict)
async def add_comment(
    task_id: str,
    comment: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Add a comment to a task. All roles except Viewer can comment."""
    org, user_role = await _get_org_and_role(current_user, db)

    if user_role == UserRole.VIEWER:
        raise HTTPException(status_code=403, detail="View-Only users cannot comment")

    for task in TASKS_STORE:
        if task["id"] == task_id and task.get("workspace_id") == str(org.id):
            task["comments"].append({
                "id": f"c_{len(task['comments'])}",
                "author": str(current_user.id),
                "author_name": f"{current_user.first_name} {current_user.last_name}".strip() or current_user.email,
                "text": comment,
                "created_at": datetime.utcnow().isoformat(),
            })
            return {"task": task, "message": "Comment added"}

    raise HTTPException(status_code=404, detail="Task not found")
