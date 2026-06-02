"""
Knowledge source management endpoints
"""

import hashlib
import io
import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.endpoints.auth import get_current_active_user
from app.core.config import settings
from app.core.permissions import Feature, require_feature_dependency
from app.db.database import get_db
from app.db.models import Document, DocumentChunk, Organization, Source, SourceType, User
from app.services.document_storage import (
    content_type_for_extension,
    remove_document_file,
    resolve_storage_path,
    save_document_file,
)
from app.services.compliance_ai_integration import process_intelligence_compliance_update
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

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB
ALLOWED_UPLOAD_EXTENSIONS = {".pdf", ".txt", ".md", ".csv", ".doc", ".docx", ".html", ".htm"}


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


def _extract_text_from_upload(filename: str, raw: bytes) -> str:
    ext = _file_extension(filename)

    if ext == "pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw))
        parts = []
        for page in reader.pages:
            parts.append(page.extract_text() or "")
        return "\n".join(parts).strip()

    if ext in ("txt", "md", "csv", "html", "htm"):
        return raw.decode("utf-8", errors="ignore").strip()

    if ext in ("doc", "docx"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Word documents are not supported yet. Please upload PDF or TXT.",
        )

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unsupported file type: .{ext or 'unknown'}",
    )


def _document_processing_status(document: Document, chunk_count: int) -> str:
    meta_status = (document.document_metadata or {}).get("ingestion_status")
    if meta_status in ("processed", "processing", "failed"):
        if meta_status == "processing" and chunk_count > 0:
            return "processed"
        return meta_status
    if chunk_count > 0:
        return "processed"
    if (document.document_metadata or {}).get("ingestion_error"):
        return "failed"
    if document.content:
        return "processing"
    return "processing"


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
        return "processed" if chunks else "failed"
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
    """Run ingestion inline by default; optionally queue Celery when DOCUMENT_INGEST_USE_ASYNC_WORKER=true."""
    if settings.document_ingest_use_async_worker:
        try:
            ingest_document_task.delay(str(document.id))
            return "processing"
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


@router.post("/documents/upload", response_model=dict)
async def upload_document_file(
    file: UploadFile = File(...),
    category: Optional[str] = Form("General"),
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
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File exceeds 25 MB limit",
        )
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file")

    try:
        extracted = _extract_text_from_upload(filename, raw)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not read file: {exc}",
        ) from exc

    content_hash = hashlib.sha256(raw).hexdigest()
    uploader = f"{current_user.first_name} {current_user.last_name}".strip() or current_user.email

    doc_id = uuid.uuid4()
    storage_path = save_document_file(organization.id, doc_id, filename, raw)

    document = Document(
        id=doc_id,
        organization_id=organization.id,
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
            "ingestion_status": "processing",
        },
    )
    db.add(document)
    await db.commit()
    await db.refresh(document)

    ingestion_status = await _queue_or_run_ingestion(db, document)
    await db.refresh(document)

    chunk_count_result = await db.execute(
        select(func.count(DocumentChunk.id)).where(DocumentChunk.document_id == document.id)
    )
    chunk_total = int(chunk_count_result.scalar_one() or 0)

    if extracted:
        try:
            org_full = await db.execute(
                select(Organization)
                .where(Organization.id == organization.id)
                .options(selectinload(Organization.subscription))
            )
            org = org_full.scalar_one()
            await process_intelligence_compliance_update(
                db,
                org,
                user_message=(
                    f"Review this uploaded compliance document ({filename}) for HIPAA, "
                    f"FWA, and security gaps."
                ),
                assistant_message=extracted[:8000],
                source_type="document_analysis",
                has_documents=True,
                use_mock=settings.mock_chat_stream,
            )
        except Exception as exc:
            logger.warning("document_upload_compliance_skip: %s", exc)

    return {
        **_serialize_document(document, chunk_total),
        "status": ingestion_status,
        "message": "Document uploaded successfully",
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
            "ingestion_status": "processing",
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
        path = resolve_storage_path(document.storage_path)
    except (ValueError, FileNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found on server",
        ) from None

    meta = document.document_metadata or {}
    ext = meta.get("file_extension", "")
    media_type = meta.get("content_type") or content_type_for_extension(ext)
    return FileResponse(
        path=path,
        media_type=media_type,
        filename=document.title,
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
