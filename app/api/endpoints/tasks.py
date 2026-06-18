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
from app.services.enterprise_roles import is_workspace_admin
from app.services.task_visibility import task_visible_to_user
from app.services.task_resolution_service import parse_resolution_json, save_task_resolution
from app.services.workflow_context_builder import build_workflow_context
from app.services.workflow_service import (
    create_workflow_for_task,
    get_workflow_for_org,
    log_workflow_activity,
    plan_has_workflow,
)
from app.db.database import get_db
from app.db.models import (
    Conversation,
    Document,
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
    document_id: Optional[str] = None
    document_ids: Optional[List[str]] = None


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
    document_id: Optional[str] = None
    document_ids: Optional[List[str]] = None


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

# Active workspace members pickable for assignee/reviewer/approver.
# Excludes billing owner (enterprise_admin) and read-only viewers.
TASK_PICK_EXCLUDED_ROLES = frozenset({
    UserRole.ENTERPRISE_ADMIN.value,
    UserRole.VIEWER.value,
})
TASK_PICK_ROLES = {
    r.value for r in UserRole if r.value not in TASK_PICK_EXCLUDED_ROLES
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
            selectinload(WorkspaceTask.document),
            selectinload(WorkspaceTask.resolution_document),
        )
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


async def _resolve_org_document(
    org_id: UUID,
    document_id: Optional[str],
    db: AsyncSession,
) -> Optional[UUID]:
    if not document_id:
        return None
    try:
        did = uuid_lib.UUID(document_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid document")

    result = await db.execute(
        select(Document).where(
            Document.id == did,
            Document.organization_id == org_id,
            Document.is_active == True,
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(
            status_code=400,
            detail="Document not found in your organization",
        )
    return did


def _task_document_uuids(task: WorkspaceTask) -> list[UUID]:
    ids: list[UUID] = []
    if task.document_ids:
        for raw in task.document_ids:
            try:
                ids.append(uuid_lib.UUID(str(raw)))
            except (ValueError, AttributeError, TypeError):
                continue
    elif task.document_id:
        ids.append(task.document_id)
    return ids


async def _resolve_org_documents(
    org_id: UUID,
    document_ids: Optional[List[str]],
    legacy_document_id: Optional[str],
    db: AsyncSession,
) -> tuple[list[str], Optional[UUID]]:
    candidates: list[str] = []
    if document_ids is not None:
        candidates.extend(document_ids)
    elif legacy_document_id:
        candidates.append(legacy_document_id)

    seen: set[str] = set()
    ordered: list[str] = []
    for raw in candidates:
        key = str(raw).strip()
        if key and key not in seen:
            seen.add(key)
            ordered.append(key)

    resolved: list[str] = []
    for did in ordered:
        uid = await _resolve_org_document(org_id, did, db)
        if uid:
            resolved.append(str(uid))

    primary = uuid_lib.UUID(resolved[0]) if resolved else None
    return resolved, primary


async def _documents_map_for_tasks(
    org_id: UUID,
    tasks: list[WorkspaceTask],
    db: AsyncSession,
) -> dict[str, list[dict]]:
    all_uuids: list[UUID] = []
    for task in tasks:
        all_uuids.extend(_task_document_uuids(task))

    if not all_uuids:
        return {
            str(task.id): (
                [{"id": str(task.document.id), "title": task.document.title}]
                if task.document
                else []
            )
            for task in tasks
        }

    unique = list(dict.fromkeys(all_uuids))
    result = await db.execute(
        select(Document).where(
            Document.id.in_(unique),
            Document.organization_id == org_id,
            Document.is_active == True,
        )
    )
    by_id = {doc.id: doc for doc in result.scalars().all()}

    out: dict[str, list[dict]] = {}
    for task in tasks:
        payloads: list[dict] = []
        for uid in _task_document_uuids(task):
            doc = by_id.get(uid)
            if doc:
                payloads.append({"id": str(doc.id), "title": doc.title})
        if not payloads and task.document:
            payloads = [{"id": str(task.document.id), "title": task.document.title}]
        out[str(task.id)] = payloads
    return out


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


def _serialize_task(
    task: WorkspaceTask,
    org: Optional[Organization] = None,
    documents: Optional[list[dict]] = None,
) -> dict:
    assignee_name = _user_display(task.assignee)
    # Pro: show creator/admin as owner when no explicit assignee (legacy tasks)
    if (
        not assignee_name
        and org is not None
        and not _is_enterprise(org)
        and task.creator
    ):
        assignee_name = _user_display(task.creator)

    docs = list(documents) if documents is not None else []
    if not docs and task.document:
        docs = [{"id": str(task.document.id), "title": task.document.title}]

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
        "assignee_name": assignee_name,
        "reviewer_id": str(task.reviewer_id) if task.reviewer_id else None,
        "reviewer_name": _user_display(task.reviewer),
        "approver_id": str(task.approver_id) if task.approver_id else None,
        "approver_name": _user_display(task.approver),
        "document_ids": [d["id"] for d in docs],
        "documents": docs,
        "document_id": docs[0]["id"] if docs else None,
        "document_title": docs[0]["title"] if docs else None,
        "workflow_id": str(task.workflow_id) if task.workflow_id else None,
        "resolution_result": task.resolution_result,
        "resolution_history": task.resolution_history or [],
        "resolution_document_id": (
            str(task.resolution_document_id) if task.resolution_document_id else None
        ),
        "resolution_document_title": (
            task.resolution_document.title if task.resolution_document else None
        ),
        "execution_conversation_id": (
            str(task.execution_conversation_id) if task.execution_conversation_id else None
        ),
        "comments": task.comments or [],
        "history": task.history or [],
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }


def _can_user_act_as_assignee(task: WorkspaceTask, user: User, user_role: UserRole) -> bool:
    """Start work, submit for review, resume after rejection — assigned user or admin."""
    if is_workspace_admin(user_role):
        return True
    if task.assignee_id is not None and task.assignee_id == user.id:
        return True
    # Legacy Pro tasks without explicit assignee — creator may complete
    if task.assignee_id is None and task.creator_id == user.id:
        return user_role in (UserRole.CONTRIBUTOR, UserRole.SEO)
    return False


def _can_user_review(task: WorkspaceTask, user: User, user_role: UserRole) -> bool:
    if is_workspace_admin(user_role):
        return True
    if task.reviewer_id:
        return task.reviewer_id == user.id
    # Legacy tasks with no named reviewer — seat role reviewer may act
    return user_role == UserRole.REVIEWER


def _can_user_approve(task: WorkspaceTask, user: User, user_role: UserRole) -> bool:
    if is_workspace_admin(user_role):
        return True
    if task.approver_id:
        return task.approver_id == user.id
    # Legacy tasks with no named approver — seat role approver may act
    return user_role == UserRole.APPROVER


def _task_workflow_permissions(
    task: WorkspaceTask,
    user: User,
    user_role: UserRole,
    enterprise: bool,
) -> dict:
    """Per-task workflow action flags."""
    if not enterprise:
        status = TaskStatus(task.status)
        assignee_ok = _can_user_act_as_assignee(task, user, user_role)
        return {
            "can_start_work": False,
            "can_submit_for_review": False,
            "can_complete_review": False,
            "can_approve_task": False,
            "can_reject_at_review": False,
            "can_reject_at_approval": False,
            "can_resume_work": False,
            "can_mark_complete": False,
            "can_complete_task": status != TaskStatus.COMPLETED and assignee_ok,
        }

    status = TaskStatus(task.status)
    assignee_ok = _can_user_act_as_assignee(task, user, user_role)

    return {
        "can_start_work": status == TaskStatus.PENDING and assignee_ok,
        "can_submit_for_review": status == TaskStatus.IN_PROGRESS and assignee_ok,
        "can_complete_review": status == TaskStatus.SUBMITTED_FOR_REVIEW
        and _can_user_review(task, user, user_role),
        "can_reject_at_review": status == TaskStatus.SUBMITTED_FOR_REVIEW
        and _can_user_review(task, user, user_role),
        "can_approve_task": status == TaskStatus.REVIEWED
        and _can_user_approve(task, user, user_role),
        "can_reject_at_approval": status == TaskStatus.REVIEWED
        and _can_user_approve(task, user, user_role),
        "can_resume_work": status == TaskStatus.REJECTED and assignee_ok,
        "can_mark_complete": status == TaskStatus.APPROVED and assignee_ok,
        "can_complete_task": False,
    }


def _workflow_notice(
    task: WorkspaceTask,
    user: User,
    user_role: UserRole,
    enterprise: bool,
    workflow: dict,
) -> Optional[str]:
    if not enterprise:
        status = TaskStatus(task.status)
        if status == TaskStatus.COMPLETED:
            return "This task is completed."
        if workflow.get("can_complete_task"):
            return None
        if user_role == UserRole.VIEWER:
            return "You have read-only access to this task."
        return "Only the workspace admin or task owner can complete this task."

    status = TaskStatus(task.status)
    assignee_name = _user_display(task.assignee) or "the assignee"
    reviewer_name = _user_display(task.reviewer) or "the assigned reviewer"
    approver_name = _user_display(task.approver) or "the assigned approver"

    if status == TaskStatus.PENDING:
        if workflow["can_start_work"]:
            return None
        if user_role == UserRole.REVIEWER:
            return (
                f"The assignee has not started this task yet. "
                f"You can review it after {assignee_name} submits it for review."
            )
        if user_role == UserRole.APPROVER:
            return (
                "The assignee has not started this task yet. "
                "Approval will be available after the task is submitted and reviewed."
            )
        if user_role in (UserRole.CONTRIBUTOR, UserRole.SEO):
            return "Only the assigned contributor/SEO or an admin can start this task."
        if user_role == UserRole.VIEWER:
            return "This task has not been started yet."

    if status == TaskStatus.IN_PROGRESS:
        if workflow["can_submit_for_review"]:
            return None
        if user_role == UserRole.REVIEWER:
            return (
                f"{assignee_name} is still working on this task. "
                "It has not been submitted for review yet."
            )
        if user_role == UserRole.APPROVER:
            return (
                "This task is in progress. It must be submitted for review and "
                "reviewed before you can approve it."
            )
        if user_role in (UserRole.CONTRIBUTOR, UserRole.SEO):
            return "Only the assignee or an admin can submit this task for review."
        if user_role == UserRole.VIEWER:
            return f"This task is in progress. {assignee_name} is working on it."

    if status == TaskStatus.SUBMITTED_FOR_REVIEW:
        if workflow["can_complete_review"] or workflow["can_reject_at_review"]:
            return None
        if user_role in (UserRole.CONTRIBUTOR, UserRole.SEO) and _can_user_act_as_assignee(
            task, user, user_role
        ):
            return f"Your submission is awaiting review by {reviewer_name}."
        if user_role == UserRole.APPROVER:
            return (
                f"This task is awaiting review by {reviewer_name} "
                "before it can be submitted for final approval."
            )
        if user_role == UserRole.REVIEWER:
            return f"You are not the assigned reviewer for this task. Awaiting {reviewer_name}."
        return f"This task is awaiting review by {reviewer_name}."

    if status == TaskStatus.REVIEWED:
        if workflow["can_approve_task"] or workflow["can_reject_at_approval"]:
            return None
        if user_role == UserRole.REVIEWER:
            return (
                f"Review is complete. This task is awaiting final approval by {approver_name}."
            )
        if user_role in (UserRole.CONTRIBUTOR, UserRole.SEO):
            return (
                f"Your task has been reviewed and is pending final approval by {approver_name}."
            )
        if user_role == UserRole.APPROVER:
            return "You are not the assigned approver for this task."
        return f"This task is awaiting final approval by {approver_name}."

    if status == TaskStatus.APPROVED:
        if workflow["can_mark_complete"]:
            return None
        if user_role in (UserRole.REVIEWER, UserRole.APPROVER):
            return "This task has been approved. The assignee or an admin can mark it complete."
        if user_role == UserRole.VIEWER:
            return "This task has been approved and is awaiting completion."

    if status == TaskStatus.REJECTED:
        if workflow["can_resume_work"]:
            return None
        if user_role == UserRole.REVIEWER:
            return f"This task was rejected and returned to {assignee_name} for revision."
        if user_role == UserRole.APPROVER:
            return f"This task was rejected and returned to {assignee_name} for revision."
        if user_role in (UserRole.CONTRIBUTOR, UserRole.SEO):
            return "Only the assignee or an admin can resume work on this rejected task."

    if status == TaskStatus.COMPLETED:
        return "This task is completed."

    return None


def _task_visible_to_user(task: WorkspaceTask, user_id: UUID, user_role: UserRole) -> bool:
    return task_visible_to_user(task, user_id, user_role)


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

    members = [_member_row(s) for s in seats if _role_value(s.role) in TASK_PICK_ROLES]

    return {
        "assignees": members,
        "reviewers": members,
        "approvers": members,
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
            org.id, data.assignee_id, TASK_PICK_ROLES, db, "assignee"
        )
        reviewer_id = await _resolve_org_member(
            org.id, data.reviewer_id, TASK_PICK_ROLES, db, "reviewer"
        )
        approver_id = await _resolve_org_member(
            org.id, data.approver_id, TASK_PICK_ROLES, db, "approver"
        )
    else:
        # Pro: single-owner — admin (or creator) is assignee by default
        if is_workspace_admin(user_role):
            assignee_id = current_user.id
        elif user_role in (UserRole.CONTRIBUTOR, UserRole.SEO):
            assignee_id = current_user.id

    resolved_doc_ids, document_id = await _resolve_org_documents(
        org.id,
        data.document_ids,
        data.document_id,
        db,
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

    workflow_id = None
    workflow_record = None
    if plan_has_workflow(org):
        owner_id = assignee_id or current_user.id
        workflow_record = await create_workflow_for_task(
            db,
            org,
            current_user,
            title=data.title.strip(),
            description=data.description or "",
            priority=(data.priority or "medium").lower(),
            category=data.category or "General",
            due_date=data.due_date,
            owner_id=owner_id,
        )
        workflow_id = workflow_record.id

    task = WorkspaceTask(
        organization_id=org.id,
        creator_id=current_user.id,
        assignee_id=assignee_id,
        reviewer_id=reviewer_id,
        approver_id=approver_id,
        document_id=document_id,
        document_ids=resolved_doc_ids,
        workflow_id=workflow_id,
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

    if workflow_record:
        await log_workflow_activity(
            db,
            workflow_record.id,
            org.id,
            current_user.id,
            "task_linked",
            {
                "task_id": str(task.id),
                "task_title": task.title,
                "assignee_id": str(assignee_id) if assignee_id else None,
            },
        )

    await db.refresh(task, ["creator", "assignee", "reviewer", "approver", "document", "resolution_document"])

    docs_map = await _documents_map_for_tasks(org.id, [task], db)
    return {
        "task": _serialize_task(task, org, documents=docs_map.get(str(task.id), [])),
        "workflow_id": str(workflow_id) if workflow_id else None,
        "message": "Task created",
    }


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
            selectinload(WorkspaceTask.document),
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

    docs_map = await _documents_map_for_tasks(org.id, tasks, db)
    serialized = [
        _serialize_task(t, org, documents=docs_map.get(str(t.id), [])) for t in tasks
    ]

    counts = {s.value: 0 for s in TaskStatus}
    for t in all_tasks:
        if _task_visible_to_user(t, user_id, user_role) and t.status in counts:
            counts[t.status] += 1

    enterprise = _is_enterprise(org)
    return {
        "tasks": serialized,
        "counts": counts,
        "can_create": user_role not in (UserRole.VIEWER, UserRole.REVIEWER, UserRole.APPROVER),
        "can_review": user_role in (
            UserRole.ENTERPRISE_ADMIN,
            UserRole.ADMIN,
            UserRole.REVIEWER,
        ),
        "can_approve": user_role in (
            UserRole.ENTERPRISE_ADMIN,
            UserRole.ADMIN,
            UserRole.APPROVER,
        ),
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
    enterprise = _is_enterprise(org)
    workflow = _task_workflow_permissions(task, current_user, user_role, enterprise)
    notice = _workflow_notice(task, current_user, user_role, enterprise, workflow)

    docs_map = await _documents_map_for_tasks(org.id, [task], db)
    return {
        "task": _serialize_task(task, org, documents=docs_map.get(str(task.id), [])),
        "permissions": {
            "can_edit": user_role in (
                UserRole.ENTERPRISE_ADMIN,
                UserRole.ADMIN,
                UserRole.CONTRIBUTOR,
            ) and (is_workspace_admin(user_role) or is_owner),
            "can_review": _can_user_review(task, current_user, user_role),
            "can_approve": _can_user_approve(task, current_user, user_role),
            "can_delete": is_workspace_admin(user_role),
            "can_submit": _can_user_act_as_assignee(task, current_user, user_role),
            "is_enterprise": enterprise,
            **workflow,
        },
        "workflow_message": notice,
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

    pro_direct_complete = (
        not enterprise
        and target_status == TaskStatus.COMPLETED
        and current_status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS)
    )
    if pro_direct_complete:
        if not _can_user_act_as_assignee(task, current_user, user_role):
            raise HTTPException(
                status_code=403,
                detail="Only the workspace admin or task owner can complete this task",
            )
    elif target_status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid transition: {current_status.value} -> {target_status.value}. "
            f"Allowed: {[s.value for s in allowed]}",
        )

    if target_status == TaskStatus.IN_PROGRESS and current_status == TaskStatus.PENDING:
        if enterprise and not _can_user_act_as_assignee(task, current_user, user_role):
            raise HTTPException(
                status_code=403,
                detail="Only the assigned assignee or an admin can start this task",
            )

    if target_status == TaskStatus.IN_PROGRESS and current_status == TaskStatus.REJECTED:
        if enterprise and not _can_user_act_as_assignee(task, current_user, user_role):
            raise HTTPException(
                status_code=403,
                detail="Only the assignee or an admin can resume work on this task",
            )

    if target_status == TaskStatus.SUBMITTED_FOR_REVIEW:
        if enterprise and not _can_user_act_as_assignee(task, current_user, user_role):
            raise HTTPException(
                status_code=403,
                detail="Only the assignee or an admin can submit this task for review",
            )

    if target_status == TaskStatus.COMPLETED and current_status == TaskStatus.APPROVED:
        if enterprise and not _can_user_act_as_assignee(task, current_user, user_role):
            raise HTTPException(
                status_code=403,
                detail="Only the assignee or an admin can mark this task complete",
            )

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
    await db.refresh(task, ["creator", "assignee", "reviewer", "approver", "document", "resolution_document"])

    docs_map = await _documents_map_for_tasks(org.id, [task], db)
    return {
        "task": _serialize_task(task, org, documents=docs_map.get(str(task.id), [])),
        "message": f"Status transitioned to {target_status.value}",
    }


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
                org.id, data.assignee_id or None, TASK_PICK_ROLES, db, "assignee"
            )
        if data.reviewer_id is not None:
            task.reviewer_id = await _resolve_org_member(
                org.id, data.reviewer_id or None, TASK_PICK_ROLES, db, "reviewer"
            )
        if data.approver_id is not None:
            task.approver_id = await _resolve_org_member(
                org.id, data.approver_id or None, TASK_PICK_ROLES, db, "approver"
            )

    if data.document_ids is not None:
        resolved_doc_ids, primary_doc_id = await _resolve_org_documents(
            org.id, data.document_ids, None, db
        )
        task.document_ids = resolved_doc_ids
        task.document_id = primary_doc_id
    elif data.document_id is not None:
        resolved_doc_ids, primary_doc_id = await _resolve_org_documents(
            org.id, None, data.document_id or None, db
        )
        task.document_ids = resolved_doc_ids
        task.document_id = primary_doc_id

    task.updated_at = datetime.utcnow()
    await db.flush()
    await db.refresh(task, ["creator", "assignee", "reviewer", "approver", "document", "resolution_document"])
    docs_map = await _documents_map_for_tasks(org.id, [task], db)
    return {
        "task": _serialize_task(task, org, documents=docs_map.get(str(task.id), [])),
        "message": "Task updated",
    }


class TaskResolutionSave(BaseModel):
    resolution: Optional[dict] = None
    raw_content: Optional[str] = None
    conversation_id: Optional[str] = None


@router.post("/{task_id}/execute", response_model=dict)
async def execute_task(
    task_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Start task execution — transitions to in_progress and returns context for Intelligence."""
    org, _seat, user_role = await _get_workspace_context(current_user, db)
    task = await _get_task_for_org(task_id, org.id, db)

    if not _task_visible_to_user(task, current_user.id, user_role):
        raise HTTPException(status_code=403, detail="You do not have access to this task")

    enterprise = _is_enterprise(org)
    perms = _task_workflow_permissions(task, current_user, user_role, enterprise)
    if not (perms.get("can_start_work") or perms.get("can_resume_work")):
        if task.status not in (TaskStatus.IN_PROGRESS.value,):
            raise HTTPException(
                status_code=403,
                detail="You cannot execute this task in its current state",
            )

    if task.status == TaskStatus.PENDING.value:
        task.status = TaskStatus.IN_PROGRESS.value
        history = list(task.history or [])
        history.append(
            {
                "action": "started",
                "user_id": str(current_user.id),
                "user_name": _user_display(current_user),
                "timestamp": datetime.utcnow().isoformat(),
                "note": "Task execution started via Intelligence",
            }
        )
        task.history = history
        task.updated_at = datetime.utcnow()
        if task.workflow_id:
            await log_workflow_activity(
                db,
                task.workflow_id,
                org.id,
                current_user.id,
                "task_execution_started",
                {"task_id": str(task.id), "task_title": task.title},
            )

    execution_conversation = Conversation(
        id=uuid_lib.uuid4(),
        organization_id=org.id,
        user_id=current_user.id,
        title=f"Task: {task.title[:80]}",
        workflow_id=task.workflow_id,
        task_id=task.id,
    )
    db.add(execution_conversation)
    task.execution_conversation_id = execution_conversation.id
    exec_history = list(task.history or [])
    exec_history.append(
        {
            "action": "execution_session_started",
            "user_id": str(current_user.id),
            "user_name": _user_display(current_user),
            "timestamp": datetime.utcnow().isoformat(),
            "conversation_id": str(execution_conversation.id),
        }
    )
    task.history = exec_history
    task.updated_at = datetime.utcnow()

    await db.commit()
    await db.refresh(task, ["creator", "assignee", "reviewer", "approver", "document", "resolution_document"])

    docs_map = await _documents_map_for_tasks(org.id, [task], db)
    context = None
    if task.workflow_id:
        try:
            workflow = await get_workflow_for_org(str(task.workflow_id), org.id, db)
            context = await build_workflow_context(
                workflow, org.id, db, focus_task_id=task.id
            )
        except ValueError:
            context = None

    return {
        "task": _serialize_task(task, org, documents=docs_map.get(str(task.id), [])),
        "workflow_id": str(task.workflow_id) if task.workflow_id else None,
        "conversation_id": str(execution_conversation.id),
        "context": context,
        "message": "Task ready for Intelligence execution",
    }


@router.post("/{task_id}/resolution", response_model=dict)
async def save_task_resolution_endpoint(
    task_id: str,
    data: TaskResolutionSave,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Save structured AI resolution to task (and workflow findings)."""
    org, _seat, user_role = await _get_workspace_context(current_user, db)
    task = await _get_task_for_org(task_id, org.id, db)

    if not _task_visible_to_user(task, current_user.id, user_role):
        raise HTTPException(status_code=403, detail="You do not have access to this task")

    enterprise = _is_enterprise(org)
    perms = _task_workflow_permissions(task, current_user, user_role, enterprise)
    assignee_ok = _can_user_act_as_assignee(task, current_user, user_role)
    if not (assignee_ok or is_workspace_admin(user_role)):
        raise HTTPException(status_code=403, detail="Only the assignee can save task resolution")

    resolution = data.resolution
    if not resolution and data.raw_content:
        resolution = parse_resolution_json(data.raw_content)
    if not resolution or not isinstance(resolution, dict):
        raise HTTPException(status_code=400, detail="Valid resolution JSON is required")

    conv_uuid = None
    if data.conversation_id:
        try:
            conv_uuid = uuid_lib.UUID(data.conversation_id)
        except (ValueError, AttributeError):
            conv_uuid = None

    task, finding = await save_task_resolution(
        db,
        task,
        org.id,
        current_user,
        resolution,
        conversation_id=conv_uuid,
        raw_content=data.raw_content,
    )
    await db.commit()
    await db.refresh(task, ["creator", "assignee", "reviewer", "approver", "document", "resolution_document"])
    docs_map = await _documents_map_for_tasks(org.id, [task], db)

    return {
        "task": _serialize_task(task, org, documents=docs_map.get(str(task.id), [])),
        "finding_id": str(finding.id) if finding else None,
        "message": "Task resolution saved",
    }


@router.delete("/{task_id}", response_model=dict)
async def delete_task(
    task_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org, _seat, user_role = await _get_workspace_context(current_user, db)
    if not is_workspace_admin(user_role):
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
    await db.refresh(task, ["creator", "assignee", "reviewer", "approver", "document", "resolution_document"])
    docs_map = await _documents_map_for_tasks(org.id, [task], db)
    return {
        "task": _serialize_task(task, org, documents=docs_map.get(str(task.id), [])),
        "message": "Comment added",
    }
