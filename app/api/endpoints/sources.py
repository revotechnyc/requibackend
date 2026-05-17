"""
Knowledge source management endpoints
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.endpoints.auth import get_current_active_user
from app.core.permissions import Feature, require_feature_dependency
from app.db.database import get_db
from app.db.models import Document, Organization, Source, SourceType, User
from app.tasks.ingestion import ingest_document_task, ingest_source_task

router = APIRouter()


class SourceCreate(BaseModel):
    name: str
    url: str
    source_type: str  # regulation, guidance, policy, article, internal
    description: Optional[str] = None
    authority_score: float = 1.0
    is_official: bool = False
    ingest_frequency: str = "manual"  # daily, weekly, manual


class SourceUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    description: Optional[str] = None
    authority_score: Optional[float] = None
    is_official: Optional[bool] = None
    ingest_frequency: Optional[str] = None
    is_active: Optional[bool] = None


class DocumentCreate(BaseModel):
    title: str
    url: Optional[str] = None
    content: Optional[str] = None
    source_id: Optional[str] = None


@router.get("/", response_model=List[dict])
async def list_sources(
    organization: Organization = Depends(lambda: require_feature_dependency(Feature.AI_QA)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """List organization's knowledge sources"""
    result = await db.execute(
        select(Source).where(
            Source.organization_id == organization.id,
        )
    )
    sources = result.scalars().all()
    
    return [
        {
            "id": str(s.id),
            "name": s.name,
            "url": s.url,
            "source_type": s.source_type.value,
            "description": s.description,
            "authority_score": float(s.authority_score),
            "is_official": s.is_official,
            "is_active": s.is_active,
            "ingest_frequency": s.ingest_frequency,
            "last_ingested_at": s.last_ingested_at.isoformat() if s.last_ingested_at else None,
            "created_at": s.created_at.isoformat(),
        }
        for s in sources
    ]


@router.post("/", response_model=dict)
async def create_source(
    data: SourceCreate,
    organization: Organization = Depends(lambda: require_feature_dependency(Feature.CUSTOM_SOURCES)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Create new knowledge source (Enterprise only)"""
    source = Source(
        organization_id=organization.id,
        name=data.name,
        url=data.url,
        source_type=SourceType(data.source_type.lower()),
        description=data.description,
        authority_score=data.authority_score,
        is_official=data.is_official,
        ingest_frequency=data.ingest_frequency,
    )
    db.add(source)
    await db.commit()
    await db.refresh(source)
    
    return {
        "id": str(source.id),
        "name": source.name,
        "message": "Source created successfully",
    }


@router.get("/{source_id}", response_model=dict)
async def get_source(
    source_id: str,
    organization: Organization = Depends(lambda: require_feature_dependency(Feature.AI_QA)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get source details"""
    result = await db.execute(
        select(Source).where(
            Source.id == source_id,
            Source.organization_id == organization.id,
        )
    )
    source = result.scalar_one_or_none()
    
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    
    return {
        "id": str(source.id),
        "name": source.name,
        "url": source.url,
        "source_type": source.source_type.value,
        "description": source.description,
        "authority_score": float(source.authority_score),
        "is_official": source.is_official,
        "is_active": source.is_active,
        "ingest_frequency": source.ingest_frequency,
        "last_ingested_at": source.last_ingested_at.isoformat() if source.last_ingested_at else None,
        "documents": [
            {
                "id": str(d.id),
                "title": d.title,
                "version": d.version,
                "is_active": d.is_active,
            }
            for d in source.documents
        ],
    }


@router.patch("/{source_id}", response_model=dict)
async def update_source(
    source_id: str,
    data: SourceUpdate,
    organization: Organization = Depends(lambda: require_feature_dependency(Feature.CUSTOM_SOURCES)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Update source"""
    result = await db.execute(
        select(Source).where(
            Source.id == source_id,
            Source.organization_id == organization.id,
        )
    )
    source = result.scalar_one_or_none()
    
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    
    if data.name:
        source.name = data.name
    if data.url:
        source.url = data.url
    if data.description is not None:
        source.description = data.description
    if data.authority_score is not None:
        source.authority_score = data.authority_score
    if data.is_official is not None:
        source.is_official = data.is_official
    if data.ingest_frequency:
        source.ingest_frequency = data.ingest_frequency
    if data.is_active is not None:
        source.is_active = data.is_active
    
    await db.commit()
    await db.refresh(source)
    
    return {
        "id": str(source.id),
        "message": "Source updated",
    }


@router.post("/{source_id}/ingest")
async def trigger_ingestion(
    source_id: str,
    organization: Organization = Depends(lambda: require_feature_dependency(Feature.CUSTOM_SOURCES)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Trigger source ingestion"""
    result = await db.execute(
        select(Source).where(
            Source.id == source_id,
            Source.organization_id == organization.id,
        )
    )
    source = result.scalar_one_or_none()
    
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    
    # Queue ingestion task
    task = ingest_source_task.delay(str(source.id))
    
    return {
        "message": "Ingestion queued",
        "task_id": task.id,
    }


@router.delete("/{source_id}")
async def delete_source(
    source_id: str,
    organization: Organization = Depends(lambda: require_feature_dependency(Feature.CUSTOM_SOURCES)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete source (soft delete)"""
    result = await db.execute(
        select(Source).where(
            Source.id == source_id,
            Source.organization_id == organization.id,
        )
    )
    source = result.scalar_one_or_none()
    
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    
    source.is_active = False
    await db.commit()
    
    return {"message": "Source deleted"}


# Documents
@router.post("/documents", response_model=dict)
async def create_document(
    data: DocumentCreate,
    organization: Organization = Depends(lambda: require_feature_dependency(Feature.CUSTOM_SOURCES)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Create document"""
    document = Document(
        organization_id=organization.id,
        title=data.title,
        url=data.url,
        content=data.content,
        source_id=data.source_id,
        content_hash="",  # Will be set during ingestion
    )
    db.add(document)
    await db.commit()
    await db.refresh(document)
    
    # Queue ingestion
    task = ingest_document_task.delay(str(document.id))
    
    return {
        "id": str(document.id),
        "message": "Document created and ingestion queued",
        "task_id": task.id,
    }


@router.get("/documents/{document_id}", response_model=dict)
async def get_document(
    document_id: str,
    organization: Organization = Depends(lambda: require_feature_dependency(Feature.AI_QA)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get document details"""
    result = await db.execute(
        select(Document).where(
            Document.id == document_id,
            Document.organization_id == organization.id,
        )
    )
    document = result.scalar_one_or_none()
    
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    return {
        "id": str(document.id),
        "title": document.title,
        "url": document.url,
        "version": document.version,
        "is_active": document.is_active,
        "chunk_count": len(document.chunks),
        "created_at": document.created_at.isoformat(),
    }
