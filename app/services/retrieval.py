"""
Knowledge retrieval service
Hybrid semantic + keyword search with reranking
"""

import hashlib
from typing import List, Optional, Tuple
from uuid import UUID

import numpy as np
from langchain.text_splitter import RecursiveCharacterTextSplitter
from openai import AsyncOpenAI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.db.models import Document, DocumentChunk, KnowledgeRecord, Source

# Initialize OpenAI client for embeddings
openai_client = AsyncOpenAI(api_key=settings.openai_api_key)


class EmbeddingService:
    """Text embedding service"""
    
    @staticmethod
    async def get_embedding(text: str) -> List[float]:
        """Get embedding vector for text"""
        response = await openai_client.embeddings.create(
            model=settings.embedding_model,
            input=text[:8000],  # Truncate to max input
        )
        return response.data[0].embedding
    
    @staticmethod
    async def get_embeddings_batch(texts: List[str]) -> List[List[float]]:
        """Get embeddings for multiple texts"""
        response = await openai_client.embeddings.create(
            model=settings.embedding_model,
            input=texts,
        )
        return [item.embedding for item in response.data]


class ChunkingService:
    """Document chunking service"""
    
    def __init__(self):
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
            separators=["\n\n", "\n", ". ", " ", ""],
        )
    
    def chunk_text(self, text: str) -> List[str]:
        """Split text into chunks"""
        return self.splitter.split_text(text)
    
    def chunk_document(self, document: Document) -> List[dict]:
        """Create chunks with metadata"""
        chunks = self.chunk_text(document.content or "")
        return [
            {
                "content": chunk,
                "chunk_index": i,
                "metadata": {
                    "document_id": str(document.id),
                    "source_id": str(document.source_id) if document.source_id else None,
                    "title": document.title,
                },
            }
            for i, chunk in enumerate(chunks)
        ]


class RetrievalService:
    """Hybrid retrieval service"""
    
    def __init__(self):
        self.embedding_service = EmbeddingService()
        self.chunking_service = ChunkingService()
    
    async def ingest_document(
        self,
        db: AsyncSession,
        document: Document,
    ) -> List[DocumentChunk]:
        """Ingest document: chunk and create embeddings"""
        # Delete existing chunks if re-ingesting
        await db.execute(
            select(DocumentChunk).where(DocumentChunk.document_id == document.id)
        )
        existing = await db.execute(
            select(DocumentChunk).where(DocumentChunk.document_id == document.id)
        )
        for chunk in existing.scalars():
            await db.delete(chunk)
        await db.flush()

        # Create chunks
        chunks_data = self.chunking_service.chunk_document(document)
        if not chunks_data:
            meta = dict(document.document_metadata or {})
            meta["ingestion_status"] = "failed"
            meta["ingestion_error"] = (
                meta.get("ingestion_error")
                or "No text chunks produced (empty file, scan-only PDF, or unreadable content)"
            )
            document.document_metadata = meta
            await db.commit()
            return []

        # Get embeddings
        texts = [chunk["content"] for chunk in chunks_data]
        embeddings = await self.embedding_service.get_embeddings_batch(texts)
        
        # Create chunk records
        chunk_records = []
        for chunk_data, embedding in zip(chunks_data, embeddings):
            chunk = DocumentChunk(
                document_id=document.id,
                content=chunk_data["content"],
                chunk_index=chunk_data["chunk_index"],
                embedding=embedding,
                chunk_metadata=chunk_data["metadata"],
            )
            db.add(chunk)
            chunk_records.append(chunk)
        
        # Update document
        document.content_hash = hashlib.sha256(
            (document.content or "").encode()
        ).hexdigest()
        document.version += 1

        meta = dict(document.document_metadata or {})
        meta["ingestion_status"] = "processed"
        meta.pop("ingestion_error", None)
        document.document_metadata = meta

        await db.commit()
        for chunk in chunk_records:
            await db.refresh(chunk)

        return chunk_records
    
    async def semantic_search(
        self,
        db: AsyncSession,
        organization_id: UUID,
        query: str,
        top_k: int = 10,
    ) -> List[Tuple[DocumentChunk, float]]:
        """Semantic search using vector similarity"""
        # Get query embedding
        query_embedding = await self.embedding_service.get_embedding(query)
        
        # Search using pgvector
        sql = text("""
            SELECT 
                dc.id,
                dc.document_id,
                dc.content,
                dc.chunk_index,
                dc.chunk_metadata,
                1 - (dc.embedding <=> :query_embedding) as similarity
            FROM document_chunks dc
            JOIN documents d ON dc.document_id = d.id
            WHERE d.organization_id = :org_id
              AND d.is_active = true
            ORDER BY dc.embedding <=> :query_embedding
            LIMIT :limit
        """)
        
        result = await db.execute(
            sql,
            {
                "query_embedding": str(query_embedding),
                "org_id": str(organization_id),
                "limit": top_k,
            },
        )
        
        chunks_with_scores = []
        for row in result.mappings():
            chunk = DocumentChunk(
                id=row["id"],
                document_id=row["document_id"],
                content=row["content"],
                chunk_index=row["chunk_index"],
                chunk_metadata=row["chunk_metadata"],
            )
            chunks_with_scores.append((chunk, float(row["similarity"])))
        
        return chunks_with_scores
    
    async def keyword_search(
        self,
        db: AsyncSession,
        organization_id: UUID,
        query: str,
        top_k: int = 10,
    ) -> List[Tuple[DocumentChunk, float]]:
        """Keyword search using PostgreSQL full-text search"""
        sql = text("""
            SELECT 
                dc.id,
                dc.document_id,
                dc.content,
                dc.chunk_index,
                dc.chunk_metadata,
                ts_rank(
                    to_tsvector('english', dc.content),
                    plainto_tsquery('english', :query)
                ) as rank
            FROM document_chunks dc
            JOIN documents d ON dc.document_id = d.id
            WHERE d.organization_id = :org_id
              AND d.is_active = true
              AND to_tsvector('english', dc.content) @@ plainto_tsquery('english', :query)
            ORDER BY rank DESC
            LIMIT :limit
        """)
        
        result = await db.execute(
            sql,
            {
                "query": query,
                "org_id": str(organization_id),
                "limit": top_k,
            },
        )
        
        chunks_with_scores = []
        for row in result.mappings():
            chunk = DocumentChunk(
                id=row["id"],
                document_id=row["document_id"],
                content=row["content"],
                chunk_index=row["chunk_index"],
                chunk_metadata=row["chunk_metadata"],
            )
            chunks_with_scores.append((chunk, float(row["rank"])))
        
        return chunks_with_scores
    
    async def hybrid_search(
        self,
        db: AsyncSession,
        organization_id: UUID,
        query: str,
        top_k: int = 5,
    ) -> List[Tuple[DocumentChunk, float]]:
        """Hybrid search combining semantic and keyword"""
        # Get results from both methods
        semantic_results = await self.semantic_search(
            db, organization_id, query, top_k=top_k * 2
        )
        keyword_results = await self.keyword_search(
            db, organization_id, query, top_k=top_k * 2
        )
        
        # Combine and deduplicate
        all_chunks = {}
        
        # Add semantic results with weight
        for chunk, score in semantic_results:
            all_chunks[chunk.id] = {
                "chunk": chunk,
                "semantic_score": score,
                "keyword_score": 0.0,
            }
        
        # Add keyword results with weight
        for chunk, score in keyword_results:
            if chunk.id in all_chunks:
                all_chunks[chunk.id]["keyword_score"] = score
            else:
                all_chunks[chunk.id] = {
                    "chunk": chunk,
                    "semantic_score": 0.0,
                    "keyword_score": score,
                }
        
        # Calculate combined score
        combined_results = []
        for item in all_chunks.values():
            # Weighted combination: 70% semantic, 30% keyword
            combined_score = (
                0.7 * item["semantic_score"] +
                0.3 * min(item["keyword_score"], 1.0)  # Normalize keyword score
            )
            combined_results.append((item["chunk"], combined_score))
        
        # Sort by combined score and return top_k
        combined_results.sort(key=lambda x: x[1], reverse=True)
        return combined_results[:top_k]
    
    async def get_context_for_query(
        self,
        db: AsyncSession,
        organization_id: UUID,
        query: str,
        max_chunks: int = 5,
    ) -> Tuple[str, List[dict]]:
        """Get context string and citations for a query"""
        # Search for relevant chunks
        results = await self.hybrid_search(
            db, organization_id, query, top_k=max_chunks
        )
        
        if not results:
            return "", []
        
        # Build context string
        context_parts = []
        citations = []
        
        for i, (chunk, score) in enumerate(results):
            # Get document info
            doc_result = await db.execute(
                select(Document).where(Document.id == chunk.document_id)
            )
            document = doc_result.scalar_one()
            
            # Add to context
            context_parts.append(
                f"[Source {i+1}] {document.title}:\n{chunk.content}"
            )
            
            # Build citation
            citations.append({
                "index": i + 1,
                "document_id": str(document.id),
                "document_title": document.title,
                "chunk_id": str(chunk.id),
                "relevance_score": round(score, 3),
                "excerpt": chunk.content[:200] + "..." if len(chunk.content) > 200 else chunk.content,
            })
        
        context = "\n\n---\n\n".join(context_parts)
        return context, citations


class KnowledgeService:
    """Knowledge record management service"""
    
    @staticmethod
    async def create_knowledge_record(
        db: AsyncSession,
        organization_id: UUID,
        topic: str,
        question: str,
        answer: str,
        confidence_score: float,
        citations: List[dict],
    ) -> KnowledgeRecord:
        """Create a new knowledge record"""
        record = KnowledgeRecord(
            organization_id=organization_id,
            topic=topic,
            question=question,
            answer=answer,
            confidence_score=confidence_score,
            status="draft",
        )
        
        db.add(record)
        await db.commit()
        await db.refresh(record)
        
        # Create citations
        for citation_data in citations:
            # Citation creation logic here
            pass
        
        return record
    
    @staticmethod
    async def get_knowledge_by_topic(
        db: AsyncSession,
        organization_id: UUID,
        topic: str,
    ) -> Optional[KnowledgeRecord]:
        """Get knowledge record by topic"""
        result = await db.execute(
            select(KnowledgeRecord)
            .where(
                KnowledgeRecord.organization_id == organization_id,
                KnowledgeRecord.topic.ilike(f"%{topic}%"),
                KnowledgeRecord.status.in_(["approved", "draft"]),
            )
            .order_by(KnowledgeRecord.confidence_score.desc())
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def flag_stale_knowledge(
        db: AsyncSession,
        organization_id: UUID,
        days: int = 30,
    ) -> List[KnowledgeRecord]:
        """Flag knowledge records that haven't been validated"""
        from datetime import datetime, timedelta
        
        cutoff_date = datetime.utcnow() - timedelta(days=days)
        
        result = await db.execute(
            select(KnowledgeRecord)
            .where(
                KnowledgeRecord.organization_id == organization_id,
                KnowledgeRecord.status == "approved",
                KnowledgeRecord.last_validated_at < cutoff_date,
            )
        )
        
        stale_records = result.scalars().all()
        
        for record in stale_records:
            record.status = "stale"
        
        await db.commit()
        return stale_records
