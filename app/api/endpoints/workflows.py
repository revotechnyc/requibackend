"""Workspace workflow container API."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.endpoints.auth import get_current_active_user
from app.db.database import get_db
from app.db.models import Organization, Seat, User, WorkspaceWorkflow
from app.services.workflow_service import (
    get_workflow_for_org,
    plan_has_workflow,
    serialize_workflow_detail,
    serialize_workflow_list_item,
)

router = APIRouter()


async def _get_org_context(user: User, db: AsyncSession) -> tuple[Organization, Seat]:
    result = await db.execute(
        select(Seat)
        .where(Seat.user_id == user.id, Seat.is_active == True)
        .options(selectinload(Seat.organization).selectinload(Organization.subscription))
        .order_by(Seat.created_at.desc())
    )
    seat = result.scalars().first()
    if not seat:
        raise HTTPException(status_code=403, detail="No active workspace")
    org = seat.organization
    if not plan_has_workflow(org):
        raise HTTPException(
            status_code=403,
            detail="Workflow is not available on your current plan. Upgrade to Pro or Enterprise.",
        )
    return org, seat


class WorkflowCreate(BaseModel):
    title: str
    description: str = ""
    external_ref: Optional[str] = None
    priority: str = "medium"
    category: str = "General"
    due_date: Optional[str] = None


@router.get("/", response_model=dict)
async def list_workflows(
    status_filter: Optional[str] = None,
    search: Optional[str] = None,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org, _seat = await _get_org_context(current_user, db)
    query = (
        select(WorkspaceWorkflow)
        .where(WorkspaceWorkflow.organization_id == org.id)
        .options(selectinload(WorkspaceWorkflow.owner))
        .order_by(desc(WorkspaceWorkflow.created_at))
    )
    result = await db.execute(query)
    workflows = result.scalars().all()

    if status_filter and status_filter.lower() != "all":
        workflows = [w for w in workflows if w.status == status_filter.lower()]

    if search:
        q = search.strip().lower()
        workflows = [
            w
            for w in workflows
            if q in w.title.lower()
            or q in w.reference_code.lower()
            or (w.external_ref and q in w.external_ref.lower())
            or (w.description and q in w.description.lower())
        ]

    return {
        "workflows": [serialize_workflow_list_item(w) for w in workflows],
        "count": len(workflows),
    }


@router.get("/{workflow_id}", response_model=dict)
async def get_workflow(
    workflow_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org, _seat = await _get_org_context(current_user, db)
    try:
        workflow = await get_workflow_for_org(workflow_id, org.id, db)
    except ValueError:
        raise HTTPException(status_code=404, detail="Workflow not found")

    detail = await serialize_workflow_detail(workflow, org.id, db)
    return {"workflow": detail}


@router.post("/", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_workflow(
    data: WorkflowCreate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services.workflow_service import create_workflow_for_task

    org, _seat = await _get_org_context(current_user, db)
    title = data.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")

    workflow = await create_workflow_for_task(
        db,
        org,
        current_user,
        title=title,
        description=data.description,
        priority=data.priority,
        category=data.category,
        due_date=data.due_date,
        owner_id=current_user.id,
    )
    if data.external_ref:
        workflow.external_ref = data.external_ref.strip()
    await db.commit()
    await db.refresh(workflow, ["owner"])

    detail = await serialize_workflow_detail(workflow, org.id, db)
    return {"workflow": detail, "message": "Workflow created"}
