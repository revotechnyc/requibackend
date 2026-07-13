"""
Knowledge source management endpoints
"""

import hashlib
import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.endpoints.auth import get_current_active_user
from app.core.config import settings
from app.core.permissions import Feature, require_feature_dependency
from app.db.database import get_db
from app.db.models import Document, DocumentChunk, Organization, PlanType, Source, SourceType, User, WorkspaceWorkflow
from app.services.workflow_service import log_workflow_activity
from app.services.document_storage import (
    build_document_file_url,
    content_type_for_extension,
    read_document_file,
    remove_document_file,
    save_document_file,
)
from app.services.document_text_extraction import extract_text_from_upload
from app.services.document_upload_pipeline import run_document_compliance_analysis
from app.services.retrieval import RetrievalService
from app.tasks.ingestion import ingest_document_task, ingest_source_task

router = APIRouter()
logger = logging.getLogger(__name__)

DOCUMENT_CATEGORIES = frozenset({
    "General",
    "Contracts",
    "Policies",
    "Reporting",
    "Training",
    "Audit",
})

ALLOWED_UPLOAD_EXTENSIONS = {".pdf", ".txt", ".md", ".csv", ".doc", ".docx", ".html", ".htm"}


def _max_upload_bytes() -> int:
    return int(settings.document_upload_max_bytes)


def _max_upload_mb_label() -> int:
    return max(1, _max_upload_bytes() // (1024 * 1024))


def _format_file_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"


def _normalize_category(category: Optional[str]) -> str:
    value = (category or "General").strip()
    if value not in DOCUMENT_CATEGORIES:
        allowed = ", ".join(sorted(DOCUMENT_CATEGORIES))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid category. Choose one of: {allowed}",
        )
    return value


def _file_extension(filename: str) -> str:
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower()


async def _enforce_document_page_limit(
    db: AsyncSession,
    organization_id: uuid.UUID,
    page_count: Optional[int],
) -> None:
    """Pro plan: enforce marketing page cap for AI document analysis."""
    if page_count is None:
        return
    org_result = await db.execute(
        select(Organization)
        .where(Organization.id == organization_id)
        .options(selectinload(Organization.subscription))
    )
    org = org_result.scalar_one_or_none()
    plan = org.subscription.plan_type if org and org.subscription else PlanType.STANDARD
    if plan != PlanType.PRO:
        return
    limit = settings.pro_plan_document_max_pages
    if page_count > limit:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"This document has {page_count} pages. Requi Pro supports AI analysis "
                f"for up to {limit} pages. Upgrade to Enterprise for unlimited pages, "
                f"or upload a shorter document."
            ),
        )


def _document_processing_status(document: Document, chunk_count: int) -> str:
    meta_status = (document.document_metadata or {}).get("ingestion_status")
    if meta_status in ("processed", "failed", "indexing", "gap_analysis", "processing"):
        # Legacy "processing": distinguish indexing vs gap analysis when possible.
        if meta_status == "processing":
            return "gap_analysis" if chunk_count > 0 else "indexing"
        return meta_status
    if chunk_count > 0:
        return "processed"
    if (document.document_metadata or {}).get("ingestion_error"):
        return "failed"
    if document.content:
        return "indexing"
    return "indexing"


def _serialize_document(document: Document, chunk_count: int = 0) -> dict:
    meta = document.document_metadata or {}
    ext = meta.get("file_extension", "")
    file_type = ext.upper() if ext else "FILE"
    return {
        "id": str(document.id),
        "name": document.title,
        "title": document.title,
        "type": file_type,
        "size": meta.get("size_display", ""),
        "size_bytes": meta.get("size_bytes", 0),
        "uploaded_by": meta.get("uploaded_by", "Unknown"),
        "uploaded_at": document.created_at.strftime("%b %d, %Y"),
        "created_at": document.created_at.isoformat(),
        "status": _document_processing_status(document, chunk_count),
        "category": meta.get("category", "General"),
        "chunk_count": chunk_count,
        "is_active": document.is_active,
        "has_original_file": bool(document.storage_path),
        "storage_path": document.storage_path,
        "file_url": build_document_file_url(document.storage_path),
        "ingestion_error": meta.get("ingestion_error"),
        "page_count": meta.get("page_count"),
    }


async def _get_org_document(
    document_id: str,
    organization: Organization,
    db: AsyncSession,
) -> Document:
    try:
        doc_uuid = uuid.UUID(document_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Document not found") from exc

    result = await db.execute(
        select(Document).where(
            Document.id == doc_uuid,
            Document.organization_id == organization.id,
            Document.is_active.is_(True),
        )
    )
    document = result.scalar_one_or_none()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    return document


async def _run_document_ingestion_inline(db: AsyncSession, document: Document) -> str:
    """Run chunking + embeddings in-process; updates document_metadata via RetrievalService."""
    if not (document.content or "").strip():
        document.document_metadata = {
            **(document.document_metadata or {}),
            "ingestion_status": "failed",
            "ingestion_error": "No extractable text in file",
        }
        await db.commit()
        return "failed"
    try:
        retrieval = RetrievalService()
        chunks = await retrieval.ingest_document(db, document)
        status_value = "processed" if chunks else "failed"
        if chunks:
            result = await db.execute(select(Document).where(Document.id == document.id))
            doc = result.scalar_one_or_none()
            if doc:
                meta = dict(doc.document_metadata or {})
                meta["ingestion_status"] = "gap_analysis"
                meta.pop("ingestion_error", None)
                doc.document_metadata = meta
                await db.commit()
            try:
                await run_document_compliance_analysis(db, document.id, filename=document.title)
            except Exception as comp_exc:
                logger.warning("inline_compliance_skip doc=%s: %s", document.id, comp_exc)
            result = await db.execute(select(Document).where(Document.id == document.id))
            doc = result.scalar_one_or_none()
            if doc:
                meta = dict(doc.document_metadata or {})
                meta["ingestion_status"] = "processed"
                meta.pop("ingestion_error", None)
                doc.document_metadata = meta
                await db.commit()
        return status_value
    except Exception as exc:
        await db.rollback()
        result = await db.execute(select(Document).where(Document.id == document.id))
        doc = result.scalar_one_or_none()
        if doc:
            doc.document_metadata = {
                **(doc.document_metadata or {}),
                "ingestion_status": "failed",
                "ingestion_error": str(exc)[:2000],
            }
            await db.commit()
        return "failed"


async def _queue_or_run_ingestion(db: AsyncSession, document: Document) -> str:
    """Queue Celery ingestion when enabled; otherwise run inline (dev fallback)."""
    if settings.document_ingest_use_async_worker:
        try:
            ingest_document_task.delay(str(document.id))
            return "indexing"
        except Exception:
            logger.warning(
                "document_ingest_use_async_worker=true but Celery publish failed; ingesting inline",
                exc_info=True,
            )
    return await _run_document_ingestion_inline(db, document)


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
    category: Optional[str] = "General"


class DocumentUpdate(BaseModel):
    category: str


# ==========================
# Documents (before /{source_id} routes)
# ==========================

@router.get("/documents", response_model=List[dict])
async def list_documents(
    organization: Organization = Depends(require_feature_dependency(Feature.KNOWLEDGE_STORAGE)),
    db: AsyncSession = Depends(get_db),
):
    """List organization documents for the Documents tab."""
    result = await db.execute(
        select(Document)
        .where(
            Document.organization_id == organization.id,
            Document.is_active.is_(True),
        )
        .order_by(Document.created_at.desc())
    )
    documents = result.scalars().all()

    chunk_counts: dict = {}
    if documents:
        doc_ids = [doc.id for doc in documents]
        count_result = await db.execute(
            select(DocumentChunk.document_id, func.count(DocumentChunk.id))
            .where(DocumentChunk.document_id.in_(doc_ids))
            .group_by(DocumentChunk.document_id)
        )
        chunk_counts = {row[0]: row[1] for row in count_result.all()}

    return [
        _serialize_document(doc, chunk_counts.get(doc.id, 0))
        for doc in documents
    ]


async def _resolve_workflow_id(
    workflow_id: Optional[str],
    org_id: uuid.UUID,
    db: AsyncSession,
) -> Optional[uuid.UUID]:
    if not workflow_id:
        return None
    try:
        wid = uuid.UUID(workflow_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid workflow")

    result = await db.execute(
        select(WorkspaceWorkflow).where(
            WorkspaceWorkflow.id == wid,
            WorkspaceWorkflow.organization_id == org_id,
        )
    )
    workflow = result.scalar_one_or_none()
    if not workflow:
        raise HTTPException(status_code=400, detail="Workflow not found in your organization")
    return wid


@router.post("/documents/upload", response_model=dict)
async def upload_document_file(
    file: UploadFile = File(...),
    category: Optional[str] = Form("General"),
    workflow_id: Optional[str] = Form(None),
    organization: Organization = Depends(require_feature_dependency(Feature.KNOWLEDGE_STORAGE)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload a file, store content, and queue embedding ingestion."""
    doc_category = _normalize_category(category)
    filename = file.filename or "upload"
    ext = _file_extension(filename)
    if f".{ext}" not in ALLOWED_UPLOAD_EXTENSIONS and ext:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File type not allowed. Supported: {', '.join(sorted(ALLOWED_UPLOAD_EXTENSIONS))}",
        )

    raw = await file.read()
    if len(raw) > _max_upload_bytes():
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds {_max_upload_mb_label()} MB limit",
        )
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file")

    try:
        extraction = extract_text_from_upload(filename, raw)
        extracted = extraction.text
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not read file: {exc}",
        ) from exc

    await _enforce_document_page_limit(db, organization.id, extraction.page_count)

    content_hash = hashlib.sha256(raw).hexdigest()
    uploader = f"{current_user.first_name} {current_user.last_name}".strip() or current_user.email

    linked_workflow_id = await _resolve_workflow_id(workflow_id, organization.id, db)

    doc_id = uuid.uuid4()
    file_content_type = file.content_type or content_type_for_extension(ext)
    try:
        storage_path = save_document_file(
            organization.id,
            doc_id,
            filename,
            raw,
            content_type=file_content_type,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    document = Document(
        id=doc_id,
        organization_id=organization.id,
        workflow_id=linked_workflow_id,
        title=filename,
        content=extracted or None,
        content_hash=content_hash,
        storage_path=storage_path,
        document_metadata={
            "category": doc_category,
            "file_extension": ext,
            "content_type": file.content_type or content_type_for_extension(ext),
            "size_bytes": len(raw),
            "size_display": _format_file_size(len(raw)),
            "uploaded_by": uploader,
            "uploaded_by_id": str(current_user.id),
            "ingestion_status": "indexing",
            "page_count": extraction.page_count,
            "extraction_method": extraction.method,
        },
    )
    db.add(document)
    await db.commit()
    await db.refresh(document)

    ingestion_status = await _queue_or_run_ingestion(db, document)
    await db.commit()
    await db.refresh(document)

    chunk_count_result = await db.execute(
        select(func.count(DocumentChunk.id)).where(DocumentChunk.document_id == document.id)
    )
    chunk_total = int(chunk_count_result.scalar_one() or 0)
    saved_doc_id = document.id

    doc_fresh = await db.get(Document, saved_doc_id)
    if not doc_fresh:
        doc_result = await db.execute(select(Document).where(Document.id == saved_doc_id))
        doc_fresh = doc_result.scalar_one()

    if linked_workflow_id:
        await log_workflow_activity(
            db,
            linked_workflow_id,
            organization.id,
            current_user.id,
            "document_linked",
            {"document_id": str(saved_doc_id), "filename": filename},
        )
        await db.commit()

    return {
        **_serialize_document(doc_fresh, chunk_total),
        "status": ingestion_status,
        "message": (
            "Document uploaded; indexing and gap analysis running in background."
            if ingestion_status in ("indexing", "processing", "gap_analysis")
            else "Document uploaded successfully"
        ),
    }


@router.post("/documents", response_model=dict)
async def create_document(
    data: DocumentCreate,
    organization: Organization = Depends(require_feature_dependency(Feature.KNOWLEDGE_STORAGE)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Create document from JSON (title + optional text content or URL)."""
    doc_category = _normalize_category(data.category)
    content = data.content or ""
    content_hash = hashlib.sha256((content or "").encode()).hexdigest()
    uploader = f"{current_user.first_name} {current_user.last_name}".strip() or current_user.email

    document = Document(
        organization_id=organization.id,
        title=data.title,
        url=data.url,
        content=content or None,
        source_id=data.source_id,
        content_hash=content_hash,
        document_metadata={
            "category": doc_category,
            "uploaded_by": uploader,
            "uploaded_by_id": str(current_user.id),
            "ingestion_status": "indexing",
        },
    )
    db.add(document)
    await db.commit()
    await db.refresh(document)

    ingestion_status = await _queue_or_run_ingestion(db, document)
    await db.refresh(document)

    return {
        "id": str(document.id),
        "status": ingestion_status,
        "message": "Document created and ingestion queued",
    }


@router.patch("/documents/{document_id}", response_model=dict)
async def update_document(
    document_id: str,
    data: DocumentUpdate,
    organization: Organization = Depends(require_feature_dependency(Feature.KNOWLEDGE_STORAGE)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Update document metadata (e.g. category for filtering)."""
    doc_category = _normalize_category(data.category)
    result = await db.execute(
        select(Document).where(
            Document.id == document_id,
            Document.organization_id == organization.id,
            Document.is_active.is_(True),
        )
    )
    document = result.scalar_one_or_none()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    meta = dict(document.document_metadata or {})
    meta["category"] = doc_category
    document.document_metadata = meta
    await db.commit()
    await db.refresh(document)

    count_result = await db.execute(
        select(func.count(DocumentChunk.id)).where(DocumentChunk.document_id == document.id)
    )
    chunk_count = count_result.scalar() or 0
    return _serialize_document(document, chunk_count)


@router.get("/documents/{document_id}/preview", response_model=dict)
async def preview_document(
    document_id: str,
    organization: Organization = Depends(require_feature_dependency(Feature.KNOWLEDGE_STORAGE)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Metadata for in-app document preview."""
    document = await _get_org_document(document_id, organization, db)
    count_result = await db.execute(
        select(func.count(DocumentChunk.id)).where(DocumentChunk.document_id == document.id)
    )
    chunk_count = count_result.scalar() or 0
    meta = document.document_metadata or {}
    ext = meta.get("file_extension", "")
    payload = _serialize_document(document, chunk_count)
    payload["content_type"] = meta.get("content_type") or content_type_for_extension(ext)
    payload["text_preview"] = (document.content or "")[:100_000] if document.content else None
    return payload


@router.get("/documents/{document_id}/file")
async def download_document_file(
    document_id: str,
    organization: Organization = Depends(require_feature_dependency(Feature.KNOWLEDGE_STORAGE)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Download the original uploaded file."""
    document = await _get_org_document(document_id, organization, db)
    if not document.storage_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Original file is not available for this document. Re-upload to enable download.",
        )
    try:
        file_bytes = read_document_file(document.storage_path)
    except (ValueError, FileNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found on server",
        ) from None

    meta = document.document_metadata or {}
    ext = meta.get("file_extension", "")
    media_type = meta.get("content_type") or content_type_for_extension(ext)
    return Response(
        content=file_bytes,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{document.title}"'},
    )


@router.get("/documents/{document_id}/file/text")
async def download_document_as_text(
    document_id: str,
    organization: Organization = Depends(require_feature_dependency(Feature.KNOWLEDGE_STORAGE)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Fallback download as plain text when original binary is unavailable."""
    document = await _get_org_document(document_id, organization, db)
    if not document.content:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No text content available for this document",
        )
    base = document.title.rsplit(".", 1)[0] if "." in document.title else document.title
    return Response(
        content=document.content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{base}.txt"'},
    )


@router.get("/documents/{document_id}", response_model=dict)
async def get_document(
    document_id: str,
    organization: Organization = Depends(require_feature_dependency(Feature.KNOWLEDGE_STORAGE)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get document details."""
    document = await _get_org_document(document_id, organization, db)
    count_result = await db.execute(
        select(func.count(DocumentChunk.id)).where(DocumentChunk.document_id == document.id)
    )
    chunk_count = count_result.scalar() or 0
    return _serialize_document(document, chunk_count)


@router.post("/documents/{document_id}/reprocess", response_model=dict)
async def reprocess_document(
    document_id: str,
    organization: Organization = Depends(require_feature_dependency(Feature.KNOWLEDGE_STORAGE)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Re-queue chunking and embedding for a failed or stale document."""
    document = await _get_org_document(document_id, organization, db)
    meta = dict(document.document_metadata or {})
    meta["ingestion_status"] = "indexing"
    meta.pop("ingestion_error", None)
    document.document_metadata = meta
    await db.commit()
    await db.refresh(document)

    ingestion_status = await _queue_or_run_ingestion(db, document)
    await db.refresh(document)

    count_result = await db.execute(
        select(func.count(DocumentChunk.id)).where(DocumentChunk.document_id == document.id)
    )
    chunk_count = int(count_result.scalar_one() or 0)
    return {
        **_serialize_document(document, chunk_count),
        "status": ingestion_status,
        "message": (
            "Document re-queued for background indexing and gap analysis."
            if ingestion_status in ("indexing", "processing", "gap_analysis")
            else "Document reprocessed"
        ),
    }


@router.delete("/documents/{document_id}")
async def delete_document(
    document_id: str,
    organization: Organization = Depends(require_feature_dependency(Feature.KNOWLEDGE_STORAGE)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a document."""
    document = await _get_org_document(document_id, organization, db)
    remove_document_file(document.storage_path)
    document.is_active = False
    await db.commit()

    return {"message": "Document deleted"}


# ==========================
# Knowledge sources
# ==========================

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
