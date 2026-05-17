"""
Knowledge management endpoints
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.endpoints.auth import get_current_active_user
from app.core.permissions import Feature, require_feature_dependency
from app.db.database import get_db
from app.db.models import (
    GapTask,
    GapTaskStatus,
    KnowledgeRecord,
    KnowledgeStatus,
    Organization,
    User,
)
from app.tasks.gap_resolution import approve_knowledge_task, reject_knowledge_task

router = APIRouter()


class KnowledgeUpdate(BaseModel):
    topic: Optional[str] = None
    question: Optional[str] = None
    answer: Optional[str] = None
    status: Optional[str] = None  # draft, pending_review, approved, rejected, stale, archived


class GapTaskUpdate(BaseModel):
    status: str  # resolved, rejected
    notes: Optional[str] = None


@router.get("/", response_model=List[dict])
async def list_knowledge(
    status: Optional[str] = None,
    organization: Organization = Depends(lambda: require_feature_dependency(Feature.KNOWLEDGE_STORAGE)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """List knowledge records"""
    query = select(KnowledgeRecord).where(
        KnowledgeRecord.organization_id == organization.id,
    )
    
    if status:
        query = query.where(KnowledgeRecord.status == status)
    
    result = await db.execute(query.order_by(KnowledgeRecord.created_at.desc()))
    records = result.scalars().all()
    
    return [
        {
            "id": str(r.id),
            "topic": r.topic,
            "question": r.question,
            "answer": r.answer[:200] + "..." if len(r.answer) > 200 else r.answer,
            "status": r.status.value,
            "confidence_score": float(r.confidence_score),
            "version": r.version,
            "created_at": r.created_at.isoformat(),
            "updated_at": r.updated_at.isoformat(),
        }
        for r in records
    ]


@router.get("/{knowledge_id}", response_model=dict)
async def get_knowledge(
    knowledge_id: str,
    organization: Organization = Depends(lambda: require_feature_dependency(Feature.KNOWLEDGE_STORAGE)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get knowledge record details"""
    result = await db.execute(
        select(KnowledgeRecord).where(
            KnowledgeRecord.id == knowledge_id,
            KnowledgeRecord.organization_id == organization.id,
        )
    )
    record = result.scalar_one_or_none()
    
    if not record:
        raise HTTPException(status_code=404, detail="Knowledge record not found")
    
    return {
        "id": str(record.id),
        "topic": record.topic,
        "question": record.question,
        "answer": record.answer,
        "status": record.status.value,
        "confidence_score": float(record.confidence_score),
        "relevance_score": float(record.relevance_score),
        "recency_score": float(record.recency_score),
        "trust_score": float(record.trust_score),
        "version": record.version,
        "citations": [
            {
                "id": str(c.id),
                "excerpt": c.excerpt,
                "relevance_score": float(c.relevance_score),
                "document_title": c.document_chunk.document.title if c.document_chunk else None,
            }
            for c in record.citations
        ],
        "reviewed_by": str(record.reviewed_by) if record.reviewed_by else None,
        "reviewed_at": record.reviewed_at.isoformat() if record.reviewed_at else None,
        "review_notes": record.review_notes,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


@router.patch("/{knowledge_id}", response_model=dict)
async def update_knowledge(
    knowledge_id: str,
    data: KnowledgeUpdate,
    organization: Organization = Depends(lambda: require_feature_dependency(Feature.KNOWLEDGE_STORAGE)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Update knowledge record"""
    result = await db.execute(
        select(KnowledgeRecord).where(
            KnowledgeRecord.id == knowledge_id,
            KnowledgeRecord.organization_id == organization.id,
        )
    )
    record = result.scalar_one_or_none()
    
    if not record:
        raise HTTPException(status_code=404, detail="Knowledge record not found")
    
    # Update fields
    if data.topic:
        record.topic = data.topic
    if data.question:
        record.question = data.question
    if data.answer:
        record.answer = data.answer
    if data.status:
        record.status = KnowledgeStatus(data.status.lower())
    
    await db.commit()
    await db.refresh(record)
    
    return {
        "id": str(record.id),
        "message": "Knowledge record updated",
    }


@router.post("/{knowledge_id}/approve")
async def approve_knowledge(
    knowledge_id: str,
    notes: Optional[str] = None,
    organization: Organization = Depends(lambda: require_feature_dependency(Feature.KNOWLEDGE_STORAGE)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Approve knowledge record"""
    result = await db.execute(
        select(KnowledgeRecord).where(
            KnowledgeRecord.id == knowledge_id,
            KnowledgeRecord.organization_id == organization.id,
        )
    )
    record = result.scalar_one_or_none()
    
    if not record:
        raise HTTPException(status_code=404, detail="Knowledge record not found")
    
    # Queue approval task
    task = approve_knowledge_task.delay(knowledge_id, str(current_user.id), notes)
    
    return {
        "message": "Approval queued",
        "task_id": task.id,
    }


@router.post("/{knowledge_id}/reject")
async def reject_knowledge(
    knowledge_id: str,
    reason: str,
    organization: Organization = Depends(lambda: require_feature_dependency(Feature.KNOWLEDGE_STORAGE)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Reject knowledge record"""
    result = await db.execute(
        select(KnowledgeRecord).where(
            KnowledgeRecord.id == knowledge_id,
            KnowledgeRecord.organization_id == organization.id,
        )
    )
    record = result.scalar_one_or_none()
    
    if not record:
        raise HTTPException(status_code=404, detail="Knowledge record not found")
    
    # Queue rejection task
    task = reject_knowledge_task.delay(knowledge_id, str(current_user.id), reason)
    
    return {
        "message": "Rejection queued",
        "task_id": task.id,
    }


# Gap Tasks
@router.get("/gaps", response_model=List[dict])
async def list_gap_tasks(
    status: Optional[str] = None,
    organization: Organization = Depends(lambda: require_feature_dependency(Feature.GAP_DETECTION)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """List gap detection tasks"""
    query = select(GapTask).where(
        GapTask.organization_id == organization.id,
    )
    
    if status:
        query = query.where(GapTask.status == status)
    
    result = await db.execute(query.order_by(GapTask.created_at.desc()))
    tasks = result.scalars().all()
    
    return [
        {
            "id": str(t.id),
            "original_query": t.original_query,
            "gap_description": t.gap_description,
            "status": t.status.value,
            "confidence_score": float(t.confidence_score),
            "proposed_knowledge_id": str(t.proposed_knowledge_id) if t.proposed_knowledge_id else None,
            "created_at": t.created_at.isoformat(),
            "updated_at": t.updated_at.isoformat(),
        }
        for t in tasks
    ]


@router.get("/gaps/{gap_id}", response_model=dict)
async def get_gap_task(
    gap_id: str,
    organization: Organization = Depends(lambda: require_feature_dependency(Feature.GAP_DETECTION)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get gap task details"""
    result = await db.execute(
        select(GapTask).where(
            GapTask.id == gap_id,
            GapTask.organization_id == organization.id,
        )
    )
    task = result.scalar_one_or_none()
    
    if not task:
        raise HTTPException(status_code=404, detail="Gap task not found")
    
    return {
        "id": str(task.id),
        "original_query": task.original_query,
        "gap_description": task.gap_description,
        "status": task.status.value,
        "confidence_score": float(task.confidence_score),
        "proposed_knowledge": {
            "id": str(task.proposed_knowledge.id),
            "topic": task.proposed_knowledge.topic,
            "answer": task.proposed_knowledge.answer,
        } if task.proposed_knowledge else None,
        "resolution_notes": task.resolution_notes,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


@router.patch("/gaps/{gap_id}", response_model=dict)
async def update_gap_task(
    gap_id: str,
    data: GapTaskUpdate,
    organization: Organization = Depends(lambda: require_feature_dependency(Feature.GAP_RESOLUTION)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Update gap task status"""
    result = await db.execute(
        select(GapTask).where(
            GapTask.id == gap_id,
            GapTask.organization_id == organization.id,
        )
    )
    task = result.scalar_one_or_none()
    
    if not task:
        raise HTTPException(status_code=404, detail="Gap task not found")
    
    task.status = GapTaskStatus(data.status.lower())
    task.resolution_notes = data.notes
    task.resolved_by = current_user.id
    from datetime import datetime
    task.resolved_at = datetime.utcnow()
    
    await db.commit()
    
    return {
        "id": str(task.id),
        "message": "Gap task updated",
    }
