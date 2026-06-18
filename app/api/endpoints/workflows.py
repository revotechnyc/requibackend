"""Workspace workflow container API."""

from __future__ import annotations

import uuid as uuid_lib
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.endpoints.auth import get_current_active_user
from app.db.database import get_db
from app.db.models import Organization, Seat, User, WorkspaceWorkflow
from app.services.task_visibility import workflow_ids_visible_to_user, workflow_visible_to_user
from app.services.workflow_service import (
    create_workflow_for_task,
    get_workflow_for_org,
    log_workflow_activity,
    plan_has_workflow,
    serialize_workflow_detail,
    serialize_workflow_list_item,
)
from app.services.workflow_context_builder import build_workflow_context
from app.db.models import WorkflowFinding

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


class WorkflowFindingCreate(BaseModel):
    summary: str = ""
    findings: list = Field(default_factory=list)
    risk_level: Optional[str] = None
    recommendations: list = Field(default_factory=list)
    evidence_refs: list = Field(default_factory=list)
    task_id: Optional[str] = None
    raw_payload: dict = Field(default_factory=dict)


@router.get("/", response_model=dict)
async def list_workflows(
    status_filter: Optional[str] = None,
    search: Optional[str] = None,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org, seat = await _get_org_context(current_user, db)
    user_id = current_user.id
    user_role = seat.role
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

    visible_ids = await workflow_ids_visible_to_user(
        org.id,
        user_id,
        user_role,
        db,
        [w.id for w in workflows],
    )
    workflows = [w for w in workflows if w.id in visible_ids]

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
    org, seat = await _get_org_context(current_user, db)
    try:
        workflow = await get_workflow_for_org(workflow_id, org.id, db)
    except ValueError:
        raise HTTPException(status_code=404, detail="Workflow not found")

    if not await workflow_visible_to_user(
        workflow, org.id, current_user.id, seat.role, db
    ):
        raise HTTPException(status_code=403, detail="You do not have access to this workflow")

    detail = await serialize_workflow_detail(
        workflow,
        org.id,
        db,
        user_id=current_user.id,
        user_role=seat.role,
    )
    return {"workflow": detail}


@router.get("/{workflow_id}/context", response_model=dict)
async def get_workflow_context(
    workflow_id: str,
    task_id: Optional[str] = None,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org, seat = await _get_org_context(current_user, db)
    try:
        workflow = await get_workflow_for_org(workflow_id, org.id, db)
    except ValueError:
        raise HTTPException(status_code=404, detail="Workflow not found")

    if not await workflow_visible_to_user(
        workflow, org.id, current_user.id, seat.role, db
    ):
        raise HTTPException(status_code=403, detail="You do not have access to this workflow")

    focus_task = None
    if task_id:
        try:
            focus_task = uuid_lib.UUID(task_id)
        except (ValueError, AttributeError):
            focus_task = None

    context = await build_workflow_context(
        workflow, org.id, db, focus_task_id=focus_task
    )
    return {"context": context}


@router.get("/{workflow_id}/findings", response_model=dict)
async def get_workflow_findings(
    workflow_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org, seat = await _get_org_context(current_user, db)
    try:
        workflow = await get_workflow_for_org(workflow_id, org.id, db)
    except ValueError:
        raise HTTPException(status_code=404, detail="Workflow not found")

    if not await workflow_visible_to_user(
        workflow, org.id, current_user.id, seat.role, db
    ):
        raise HTTPException(status_code=403, detail="You do not have access to this workflow")

    result = await db.execute(
        select(WorkflowFinding)
        .where(WorkflowFinding.workflow_id == workflow.id)
        .order_by(desc(WorkflowFinding.created_at))
        .limit(1)
    )
    finding = result.scalar_one_or_none()
    if not finding:
        return {"finding": None}
    return {
        "finding": {
            "id": str(finding.id),
            "workflow_id": str(finding.workflow_id),
            "task_id": str(finding.task_id) if finding.task_id else None,
            "summary": finding.summary,
            "findings": finding.findings or [],
            "risk_level": finding.risk_level,
            "recommendations": finding.recommendations or [],
            "evidence_refs": finding.evidence_refs or [],
            "created_at": finding.created_at.isoformat() if finding.created_at else None,
        }
    }


@router.post("/{workflow_id}/findings", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_workflow_finding(
    workflow_id: str,
    data: WorkflowFindingCreate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org, seat = await _get_org_context(current_user, db)
    try:
        workflow = await get_workflow_for_org(workflow_id, org.id, db)
    except ValueError:
        raise HTTPException(status_code=404, detail="Workflow not found")

    if not await workflow_visible_to_user(
        workflow, org.id, current_user.id, seat.role, db
    ):
        raise HTTPException(status_code=403, detail="You do not have access to this workflow")

    task_uuid = None
    if data.task_id:
        try:
            task_uuid = uuid_lib.UUID(data.task_id)
        except (ValueError, AttributeError):
            task_uuid = None

    finding = WorkflowFinding(
        id=uuid_lib.uuid4(),
        workflow_id=workflow.id,
        organization_id=org.id,
        task_id=task_uuid,
        created_by_id=current_user.id,
        summary=data.summary,
        findings=data.findings,
        risk_level=data.risk_level,
        recommendations=data.recommendations,
        evidence_refs=data.evidence_refs,
        raw_payload=data.raw_payload,
    )
    db.add(finding)
    await log_workflow_activity(
        db,
        workflow.id,
        org.id,
        current_user.id,
        "findings_saved",
        {"finding_id": str(finding.id), "task_id": data.task_id},
    )
    await db.commit()
    await db.refresh(finding)
    return {
        "finding": {
            "id": str(finding.id),
            "summary": finding.summary,
            "findings": finding.findings,
            "risk_level": finding.risk_level,
            "recommendations": finding.recommendations,
        },
        "message": "Findings saved",
    }


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
