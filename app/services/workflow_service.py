"""Workspace workflow container — create, serialize, activity logging."""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    Document,
    Organization,
    User,
    UserRole,
    WorkflowActivity,
    WorkspaceTask,
    WorkspaceWorkflow,
    WorkspaceWorkflowStatus,
)
from app.core.permissions import FeatureGate, PLAN_FEATURES
from app.services.task_visibility import (
    task_assignee_is_user,
    user_has_full_task_access,
)


def plan_has_workflow(org: Organization) -> bool:
    if not org.subscription:
        return False
    plan = org.subscription.plan_type
    return FeatureGate.has_feature(plan, "workflow") or "workflow" in PLAN_FEATURES.get(plan, [])


async def next_workflow_reference(org_id: UUID, db: AsyncSession) -> str:
    result = await db.execute(
        select(func.count())
        .select_from(WorkspaceWorkflow)
        .where(WorkspaceWorkflow.organization_id == org_id)
    )
    count = int(result.scalar_one() or 0)
    return f"WF-{1001 + count}"


async def log_workflow_activity(
    db: AsyncSession,
    workflow_id: UUID,
    org_id: UUID,
    actor_id: UUID,
    event_type: str,
    payload: Optional[dict] = None,
) -> None:
    db.add(
        WorkflowActivity(
            id=uuid_lib.uuid4(),
            workflow_id=workflow_id,
            organization_id=org_id,
            actor_id=actor_id,
            event_type=event_type,
            payload=payload or {},
            created_at=datetime.utcnow(),
        )
    )


async def create_workflow_for_task(
    db: AsyncSession,
    org: Organization,
    creator: User,
    *,
    title: str,
    description: str,
    priority: str,
    category: str,
    due_date: Optional[str],
    owner_id: UUID,
) -> WorkspaceWorkflow:
    now = datetime.utcnow()
    workflow = WorkspaceWorkflow(
        id=uuid_lib.uuid4(),
        organization_id=org.id,
        creator_id=creator.id,
        owner_id=owner_id,
        reference_code=await next_workflow_reference(org.id, db),
        title=title.strip(),
        description=description or "",
        status=WorkspaceWorkflowStatus.OPEN.value,
        source="manual",
        priority=(priority or "medium").lower(),
        category=category or "General",
        due_date=due_date,
        created_at=now,
        updated_at=now,
    )
    db.add(workflow)
    await db.flush()
    await log_workflow_activity(
        db,
        workflow.id,
        org.id,
        creator.id,
        "created",
        {"title": workflow.title, "source": "task_create"},
    )
    return workflow


def _user_display(user: Optional[User]) -> Optional[str]:
    if not user:
        return None
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    return name or user.email


def _relative_time(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    delta = datetime.utcnow() - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "Just now"
    if seconds < 3600:
        return f"{seconds // 60} minutes ago"
    if seconds < 86400:
        return f"{seconds // 3600} hours ago"
    return f"{seconds // 86400} days ago"


def serialize_workflow_list_item(workflow: WorkspaceWorkflow) -> dict:
    return {
        "id": str(workflow.id),
        "reference_code": workflow.reference_code,
        "case_number": workflow.external_ref or "N/A",
        "title": workflow.title,
        "source": workflow.source,
        "priority": workflow.priority,
        "owner": _user_display(workflow.owner) or "Unassigned",
        "owner_id": str(workflow.owner_id),
        "status": workflow.status,
        "created": workflow.created_at.date().isoformat() if workflow.created_at else "",
        "last_activity": _relative_time(workflow.updated_at),
        "due_date": workflow.due_date,
        "category": workflow.category,
    }


async def get_workflow_for_org(
    workflow_id: str,
    org_id: UUID,
    db: AsyncSession,
) -> WorkspaceWorkflow:
    try:
        wid = uuid_lib.UUID(workflow_id)
    except (ValueError, AttributeError):
        raise ValueError("Workflow not found")

    result = await db.execute(
        select(WorkspaceWorkflow)
        .where(WorkspaceWorkflow.id == wid, WorkspaceWorkflow.organization_id == org_id)
        .options(
            selectinload(WorkspaceWorkflow.creator),
            selectinload(WorkspaceWorkflow.owner),
        )
    )
    workflow = result.scalar_one_or_none()
    if not workflow:
        raise ValueError("Workflow not found")
    return workflow


async def serialize_workflow_detail(
    workflow: WorkspaceWorkflow,
    org_id: UUID,
    db: AsyncSession,
    user_id: Optional[UUID] = None,
    user_role: Optional[UserRole] = None,
) -> dict:
    tasks_result = await db.execute(
        select(WorkspaceTask)
        .where(
            WorkspaceTask.organization_id == org_id,
            WorkspaceTask.workflow_id == workflow.id,
        )
        .options(selectinload(WorkspaceTask.assignee))
        .order_by(WorkspaceTask.created_at.desc())
    )
    tasks = tasks_result.scalars().all()

    docs_result = await db.execute(
        select(Document)
        .where(
            Document.organization_id == org_id,
            Document.workflow_id == workflow.id,
            Document.is_active == True,
        )
        .order_by(Document.created_at.desc())
    )
    documents = docs_result.scalars().all()

    activities_result = await db.execute(
        select(WorkflowActivity)
        .where(WorkflowActivity.workflow_id == workflow.id)
        .options(selectinload(WorkflowActivity.actor))
        .order_by(WorkflowActivity.created_at.asc())
    )
    activities = activities_result.scalars().all()

    restrict_tasks = (
        user_id is not None
        and user_role is not None
        and not user_has_full_task_access(user_role)
    )

    linked_tasks = []
    visible_tasks = []
    for task in tasks:
        if restrict_tasks and not task_assignee_is_user(task, user_id):
            continue
        visible_tasks.append(task)
        status = task.status
        if status == "submitted_for_review":
            ui_status = "in_progress"
        elif status in ("pending", "in_progress", "completed"):
            ui_status = status if status != "pending" else "pending"
        elif status in ("reviewed", "approved", "submitted_for_review"):
            ui_status = "in_progress"
        else:
            ui_status = "pending"
        if status == "completed":
            ui_status = "completed"
        linked_tasks.append(
            {
                "id": str(task.id),
                "title": task.title,
                "assignee": _user_display(task.assignee) or "Unassigned",
                "status": ui_status,
            }
        )

    linked_documents = []
    for doc in documents:
        meta = doc.document_metadata or {}
        linked_documents.append(
            {
                "id": str(doc.id),
                "name": doc.title,
                "type": (meta.get("file_extension") or "FILE").upper(),
                "size": meta.get("size_display") or "",
            }
        )

    progress = 20
    if workflow.status == WorkspaceWorkflowStatus.COMPLETED.value:
        progress = 100
    elif workflow.status == WorkspaceWorkflowStatus.IN_REVIEW.value:
        progress = 55
    elif linked_tasks:
        completed = sum(1 for t in visible_tasks if t.status == "completed")
        progress = min(95, 20 + int((completed / max(len(visible_tasks), 1)) * 70))

    timeline = [
        {"id": "s1", "label": "Case Created", "status": "completed"},
        {
            "id": "s2",
            "label": "Documents Added",
            "status": "completed" if linked_documents else "pending",
        },
        {
            "id": "s3",
            "label": "Task Assigned",
            "status": "completed" if linked_tasks else "pending",
        },
        {
            "id": "s4",
            "label": "In Progress",
            "status": (
                "current"
                if workflow.status == WorkspaceWorkflowStatus.OPEN.value
                else "completed"
            ),
        },
        {
            "id": "s5",
            "label": "Resolution Sent",
            "status": (
                "completed"
                if workflow.status == WorkspaceWorkflowStatus.COMPLETED.value
                else "pending"
            ),
        },
        {
            "id": "s6",
            "label": "Workflow Closed",
            "status": (
                "completed"
                if workflow.status == WorkspaceWorkflowStatus.COMPLETED.value
                else "pending"
            ),
        },
    ]

    return {
        **serialize_workflow_list_item(workflow),
        "description": workflow.description,
        "salesforce_reference": workflow.external_ref,
        "progress": progress,
        "linked_tasks": linked_tasks,
        "linked_documents": linked_documents,
        "timeline": timeline,
        "communication": {
            "emails_sent": 0,
            "teams_notifications": 0,
            "last_contact": workflow.updated_at.date().isoformat() if workflow.updated_at else "",
        },
        "integrations": [
            {"name": "Salesforce", "active": workflow.source == "salesforce"},
            {"name": "Microsoft Teams", "active": False},
            {"name": "Outlook", "active": False},
            {"name": "SharePoint", "active": False},
        ],
        "activities": [
            {
                "id": str(a.id),
                "event_type": a.event_type,
                "actor_name": _user_display(a.actor),
                "payload": a.payload or {},
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in activities
        ],
    }
