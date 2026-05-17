"""
Document ingestion tasks
"""

import hashlib
from uuid import UUID

import requests
from celery import shared_task
from sqlalchemy import select

from app.db.database import get_db_context
from app.db.models import Document, Source
from app.services.retrieval import RetrievalService


@shared_task(bind=True, max_retries=3)
def ingest_document_task(self, document_id: str):
    """Ingest a single document: download, chunk, embed"""
    async def _ingest():
        async with get_db_context() as db:
            # Get document
            result = await db.execute(
                select(Document).where(Document.id == UUID(document_id))
            )
            document = result.scalar_one_or_none()
            
            if not document:
                return {"error": f"Document {document_id} not found"}
            
            # Download content if URL provided
            if document.url and not document.content:
                try:
                    response = requests.get(document.url, timeout=30)
                    response.raise_for_status()
                    
                    # Extract text based on content type
                    content_type = response.headers.get("content-type", "")
                    
                    if "pdf" in content_type:
                        # PDF parsing
                        from pypdf import PdfReader
                        import io
                        
                        pdf_file = io.BytesIO(response.content)
                        reader = PdfReader(pdf_file)
                        text = ""
                        for page in reader.pages:
                            text += page.extract_text() + "\n"
                        document.content = text
                    
                    elif "html" in content_type:
                        # HTML parsing
                        from bs4 import BeautifulSoup
                        soup = BeautifulSoup(response.content, "html.parser")
                        # Remove script and style elements
                        for script in soup(["script", "style"]):
                            script.decompose()
                        document.content = soup.get_text(separator="\n")
                    
                    else:
                        # Plain text
                        document.content = response.text
                
                except Exception as e:
                    document.document_metadata["ingestion_error"] = str(e)
                    await db.commit()
                    raise self.retry(exc=e, countdown=60)
            
            # Process document
            retrieval_service = RetrievalService()
            chunks = await retrieval_service.ingest_document(db, document)
            
            # Update source last_ingested_at
            if document.source_id:
                source_result = await db.execute(
                    select(Source).where(Source.id == document.source_id)
                )
                source = source_result.scalar_one_or_none()
                if source:
                    from datetime import datetime
                    source.last_ingested_at = datetime.utcnow()
            
            await db.commit()
            
            return {
                "document_id": document_id,
                "chunks_created": len(chunks),
                "status": "success",
            }
    
    import asyncio
    return asyncio.run(_ingest())


@shared_task
def ingest_source_task(source_id: str):
    """Ingest all documents from a source"""
    async def _ingest_source():
        async with get_db_context() as db:
            result = await db.execute(
                select(Source).where(Source.id == UUID(source_id))
            )
            source = result.scalar_one_or_none()
            
            if not source:
                return {"error": f"Source {source_id} not found"}
            
            # Get all documents for this source
            result = await db.execute(
                select(Document).where(
                    Document.source_id == UUID(source_id),
                    Document.is_active == True,
                )
            )
            documents = result.scalars().all()
            
            # Queue ingestion for each document
            task_ids = []
            for doc in documents:
                task = ingest_document_task.delay(str(doc.id))
                task_ids.append(task.id)
            
            return {
                "source_id": source_id,
                "documents_queued": len(task_ids),
                "task_ids": task_ids,
            }
    
    import asyncio
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
