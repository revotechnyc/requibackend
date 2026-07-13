"""
Document ingestion tasks
"""

import asyncio
import logging
from uuid import UUID

import requests
from celery import shared_task
from sqlalchemy import select

from app.db.database import get_db_context
from app.db.models import Document, Source
from app.services.document_upload_pipeline import run_document_compliance_analysis
from app.services.retrieval import RetrievalService

logger = logging.getLogger(__name__)


async def _mark_ingestion_failed(document_id: UUID, error_message: str) -> None:
    """Persist failed ingestion status after rollback (separate short transaction)."""
    async with get_db_context() as db:
        result = await db.execute(select(Document).where(Document.id == document_id))
        document = result.scalar_one_or_none()
        if not document:
            return
        meta = dict(document.document_metadata or {})
        meta["ingestion_status"] = "failed"
        meta["ingestion_error"] = (error_message or "Ingestion failed")[:2000]
        document.document_metadata = meta
        await db.commit()


async def _ingest_document_core(db, document: Document) -> list:
    """Chunk + embed a document; returns chunk records (may be empty)."""
    if not (document.content or "").strip():
        document.document_metadata = {
            **(document.document_metadata or {}),
            "ingestion_status": "failed",
            "ingestion_error": "No extractable text in file",
        }
        await db.commit()
        return []

    retrieval = RetrievalService()
    return await retrieval.ingest_document(db, document)


@shared_task(bind=True, max_retries=3)
def ingest_document_task(self, document_id: str):
    """Ingest a single document: chunk, embed, compliance analysis."""

    async def _ingest():
        doc_uuid = UUID(document_id)
        async with get_db_context() as db:
            result = await db.execute(select(Document).where(Document.id == doc_uuid))
            document = result.scalar_one_or_none()

            if not document:
                return {"error": f"Document {document_id} not found"}

            filename = document.title

            # Download content if URL provided
            if document.url and not document.content:
                try:
                    response = requests.get(document.url, timeout=30)
                    response.raise_for_status()

                    content_type = response.headers.get("content-type", "")

                    if "pdf" in content_type:
                        from pypdf import PdfReader
                        import io

                        pdf_file = io.BytesIO(response.content)
                        reader = PdfReader(pdf_file)
                        text = ""
                        for page in reader.pages:
                            text += (page.extract_text() or "") + "\n"
                        document.content = text

                    elif "html" in content_type:
                        from bs4 import BeautifulSoup

                        soup = BeautifulSoup(response.content, "html.parser")
                        for script in soup(["script", "style"]):
                            script.decompose()
                        document.content = soup.get_text(separator="\n")

                    else:
                        document.content = response.text

                except Exception as e:
                    meta = dict(document.document_metadata or {})
                    meta["ingestion_error"] = str(e)
                    document.document_metadata = meta
                    await db.commit()
                    raise self.retry(exc=e, countdown=60) from e

            try:
                chunks = await _ingest_document_core(db, document)
            except Exception as e:
                logger.exception("ingest_document_task failed for %s", document_id)
                await _mark_ingestion_failed(doc_uuid, str(e))
                raise

            if document.source_id:
                source_result = await db.execute(
                    select(Source).where(Source.id == document.source_id)
                )
                source = source_result.scalar_one_or_none()
                if source:
                    from datetime import datetime

                    source.last_ingested_at = datetime.utcnow()

            await db.commit()

            if chunks and (document.content or "").strip():
                # Ensure UI sees gap_analysis phase before long LLM work.
                result = await db.execute(select(Document).where(Document.id == doc_uuid))
                doc = result.scalar_one_or_none()
                if doc:
                    meta = dict(doc.document_metadata or {})
                    meta["ingestion_status"] = "gap_analysis"
                    meta.pop("ingestion_error", None)
                    doc.document_metadata = meta
                    await db.commit()
                try:
                    await run_document_compliance_analysis(
                        db,
                        doc_uuid,
                        filename=filename,
                    )
                except Exception as exc:
                    logger.warning(
                        "ingest_document_task compliance failed for %s: %s",
                        document_id,
                        exc,
                    )

            if chunks:
                result = await db.execute(select(Document).where(Document.id == doc_uuid))
                doc = result.scalar_one_or_none()
                if doc:
                    meta = dict(doc.document_metadata or {})
                    meta["ingestion_status"] = "processed"
                    meta.pop("ingestion_error", None)
                    doc.document_metadata = meta
                    await db.commit()

            return {
                "document_id": document_id,
                "chunks_created": len(chunks) if chunks else 0,
                "status": "success" if chunks else "failed",
            }

    return asyncio.run(_ingest())


@shared_task
def ingest_source_task(source_id: str):
    """Ingest all documents from a source"""

    async def _ingest_source():
        async with get_db_context() as db:
            result = await db.execute(select(Source).where(Source.id == UUID(source_id)))
            source = result.scalar_one_or_none()

            if not source:
                return {"error": f"Source {source_id} not found"}

            result = await db.execute(
                select(Document).where(
                    Document.source_id == UUID(source_id),
                    Document.is_active == True,
                )
            )
            documents = result.scalars().all()

            task_ids = []
            for doc in documents:
                task = ingest_document_task.delay(str(doc.id))
                task_ids.append(task.id)

            return {
                "source_id": source_id,
                "documents_queued": len(task_ids),
                "task_ids": task_ids,
            }

    return asyncio.run(_ingest_source())


@shared_task
def batch_ingest_task(organization_id: str, document_ids: list):
    """Batch ingest multiple documents"""
    results = []
    for doc_id in document_ids:
        result = ingest_document_task.delay(doc_id)
        results.append(result.id)

    return {
        "organization_id": organization_id,
        "documents_queued": len(results),
        "task_ids": results,
    }
