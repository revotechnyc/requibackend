"""Shared helpers for compliance gap serialization and source metadata."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ComplianceGap, Conversation, WorkspaceTask, WorkspaceWorkflow


@dataclass
class GapSourceContext:
    source_type: str = "intelligence"
    source_label: Optional[str] = None
    conversation_id: Optional[uuid.UUID] = None
    task_id: Optional[uuid.UUID] = None
    task_name: Optional[str] = None
    contract_id: Optional[uuid.UUID] = None
    contract_name: Optional[str] = None
    project_name: Optional[str] = None


def normalize_gap_source_type(source_type: str) -> str:
    mapping = {
        "chat": "intelligence",
        "document_analysis": "document",
    }
    return mapping.get(source_type, source_type or "intelligence")


def _truncate_label(value: Optional[str], limit: int = 500) -> Optional[str]:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    return text[:limit]


async def build_gap_source_context(
    db: AsyncSession,
    *,
    user_message: str,
    source_type: str = "chat",
    conversation_id: Optional[uuid.UUID] = None,
    task_id: Optional[uuid.UUID] = None,
    workflow_id: Optional[uuid.UUID] = None,
    contract_id: Optional[uuid.UUID] = None,
    contract_name: Optional[str] = None,
    document_filename: Optional[str] = None,
) -> GapSourceContext:
    """Resolve display metadata from the creation context."""
    normalized = normalize_gap_source_type(source_type)
    task_name: Optional[str] = None
    project_name: Optional[str] = None
    source_label: Optional[str] = None

    if document_filename:
        source_label = _truncate_label(document_filename)
        normalized = "document"
        project_name = project_name or "Documents"
    elif contract_name:
        source_label = _truncate_label(contract_name)
        normalized = "clm"
    else:
        source_label = _truncate_label(user_message, 200)

    if task_id:
        task_result = await db.execute(
            select(WorkspaceTask).where(WorkspaceTask.id == task_id)
        )
        task = task_result.scalar_one_or_none()
        if task:
            task_name = _truncate_label(task.title)
            if not project_name and task.category:
                project_name = _truncate_label(task.category)

    if workflow_id:
        wf_result = await db.execute(
            select(WorkspaceWorkflow).where(WorkspaceWorkflow.id == workflow_id)
        )
        workflow = wf_result.scalar_one_or_none()
        if workflow:
            project_name = _truncate_label(workflow.title)

    if conversation_id and not source_label:
        conv_result = await db.execute(
            select(Conversation).where(Conversation.id == conversation_id)
        )
        conv = conv_result.scalar_one_or_none()
        if conv and conv.title:
            source_label = _truncate_label(conv.title)

    return GapSourceContext(
        source_type=normalized,
        source_label=source_label,
        conversation_id=conversation_id,
        task_id=task_id,
        task_name=task_name,
        contract_id=contract_id,
        contract_name=_truncate_label(contract_name),
        project_name=project_name,
    )


def apply_gap_source_context(gap: ComplianceGap, ctx: Optional[GapSourceContext]) -> None:
    if not ctx:
        return
    gap.source_type = ctx.source_type
    gap.source_label = ctx.source_label
    gap.conversation_id = ctx.conversation_id
    gap.task_id = ctx.task_id
    gap.task_name = ctx.task_name
    gap.contract_id = ctx.contract_id
    gap.contract_name = ctx.contract_name
    gap.project_name = ctx.project_name


def gap_to_dict(g: ComplianceGap, *, framework_name: Optional[str] = None) -> dict[str, Any]:
    days_open = max(0, (datetime.utcnow() - g.created_at).days) if g.status == "open" else 0
    return {
        "id": str(g.id),
        "title": g.title,
        "description": g.description,
        "framework_slug": g.framework_slug,
        "framework_name": framework_name,
        "severity": g.severity,
        "status": g.status,
        "category": g.category,
        "created_at": g.created_at.isoformat() if g.created_at else None,
        "updated_at": g.updated_at.isoformat() if g.updated_at else None,
        "resolved_at": g.resolved_at.isoformat() if g.resolved_at else None,
        "days_open": days_open,
        "source_type": g.source_type,
        "source_label": g.source_label,
        "conversation_id": str(g.conversation_id) if g.conversation_id else None,
        "task_id": str(g.task_id) if g.task_id else None,
        "task_name": g.task_name,
        "contract_id": str(g.contract_id) if g.contract_id else None,
        "contract_name": g.contract_name,
        "project_name": g.project_name,
    }
