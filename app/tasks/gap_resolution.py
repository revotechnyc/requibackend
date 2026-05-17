"""
Gap resolution tasks
"""

from uuid import UUID

from celery import shared_task
from sqlalchemy import select

from app.db.database import get_db_context
from app.db.models import DocumentChunk, GapTask, GapTaskStatus, KnowledgeRecord, KnowledgeStatus
from app.services.ml import GPT55Service
from app.services.retrieval import RetrievalService


@shared_task(bind=True, max_retries=3)
def resolve_gap_task(self, gap_task_id: str):
    """Attempt to resolve a knowledge gap"""
    async def _resolve():
        async with get_db_context() as db:
            # Get gap task
            result = await db.execute(
                select(GapTask).where(GapTask.id == UUID(gap_task_id))
            )
            gap_task = result.scalar_one_or_none()
            
            if not gap_task:
                return {"error": f"Gap task {gap_task_id} not found"}
            
            if gap_task.status != GapTaskStatus.DETECTED:
                return {"error": f"Gap task is not in DETECTED status"}
            
            # Update status
            gap_task.status = GapTaskStatus.IN_PROGRESS
            await db.commit()
            
            try:
                # Step 1: Search for additional sources
                retrieval_service = RetrievalService()
                context, citations = await retrieval_service.get_context_for_query(
                    db,
                    gap_task.organization_id,
                    gap_task.original_query,
                    max_chunks=10,  # Get more context for gap resolution
                )
                
                # Step 2: Generate proposed knowledge using GPT-5.5
                gpt55_service = GPT55Service()
                proposed = await gpt55_service.generate_proposed_knowledge(
                    gap_task.original_query,
                    context,
                    citations,
                )
                
                # Step 3: Create knowledge record
                knowledge = KnowledgeRecord(
                    organization_id=gap_task.organization_id,
                    topic=proposed["topic"],
                    question=proposed["question"],
                    answer=proposed["answer"],
                    confidence_score=gap_task.confidence_score,
                    status=KnowledgeStatus.PENDING_REVIEW,
                )
                db.add(knowledge)
                await db.commit()
                await db.refresh(knowledge)
                
                # Step 4: Create citations
                for citation_data in citations:
                    from app.db.models import Citation
                    
                    citation = Citation(
                        knowledge_record_id=knowledge.id,
                        document_chunk_id=UUID(citation_data["chunk_id"]),
                        relevance_score=citation_data["relevance_score"],
                        excerpt=citation_data["excerpt"],
                    )
                    db.add(citation)
                
                # Step 5: Update gap task
                gap_task.proposed_knowledge_id = knowledge.id
                gap_task.status = GapTaskStatus.RESOLVED
                await db.commit()
                
                return {
                    "gap_task_id": gap_task_id,
                    "knowledge_id": str(knowledge.id),
                    "status": "resolved",
                    "citations_created": len(citations),
                }
            
            except Exception as e:
                gap_task.status = GapTaskStatus.DETECTED  # Reset status
                await db.commit()
                raise self.retry(exc=e, countdown=300)
    
    import asyncio
    return asyncio.run(_resolve())


@shared_task
def batch_resolve_gaps_task(organization_id: str, max_tasks: int = 10):
    """Resolve multiple pending gap tasks"""
    async def _batch_resolve():
        async with get_db_context() as db:
            # Get pending gap tasks
            result = await db.execute(
                select(GapTask)
                .where(
                    GapTask.organization_id == UUID(organization_id),
                    GapTask.status == GapTaskStatus.DETECTED,
                )
                .limit(max_tasks)
            )
            tasks = result.scalars().all()
            
            # Queue resolution for each
            task_ids = []
            for gap_task in tasks:
                task = resolve_gap_task.delay(str(gap_task.id))
                task_ids.append(task.id)
            
            return {
                "organization_id": organization_id,
                "tasks_queued": len(task_ids),
                "task_ids": task_ids,
            }
    
    import asyncio
    return asyncio.run(_batch_resolve())


@shared_task
def approve_knowledge_task(knowledge_id: str, user_id: str, notes: str = None):
    """Approve a proposed knowledge record"""
    async def _approve():
        async with get_db_context() as db:
            from datetime import datetime
            
            result = await db.execute(
                select(KnowledgeRecord).where(KnowledgeRecord.id == UUID(knowledge_id))
            )
            knowledge = result.scalar_one_or_none()
            
            if not knowledge:
                return {"error": f"Knowledge record {knowledge_id} not found"}
            
            knowledge.status = KnowledgeStatus.APPROVED
            knowledge.reviewed_by = UUID(user_id)
            knowledge.reviewed_at = datetime.utcnow()
            knowledge.review_notes = notes
            knowledge.last_validated_at = datetime.utcnow()
            
            await db.commit()
            
            return {
                "knowledge_id": knowledge_id,
                "status": "approved",
                "reviewed_by": user_id,
            }
    
    import asyncio
    return asyncio.run(_approve())


@shared_task
def reject_knowledge_task(knowledge_id: str, user_id: str, reason: str):
    """Reject a proposed knowledge record"""
    async def _reject():
        async with get_db_context() as db:
            from datetime import datetime
            
            result = await db.execute(
                select(KnowledgeRecord).where(KnowledgeRecord.id == UUID(knowledge_id))
            )
            knowledge = result.scalar_one_or_none()
            
            if not knowledge:
                return {"error": f"Knowledge record {knowledge_id} not found"}
            
            knowledge.status = KnowledgeStatus.REJECTED
            knowledge.reviewed_by = UUID(user_id)
            knowledge.reviewed_at = datetime.utcnow()
            knowledge.review_notes = reason
            
            await db.commit()
            
            return {
                "knowledge_id": knowledge_id,
                "status": "rejected",
                "reason": reason,
            }
    
    import asyncio
    return asyncio.run(_reject())
