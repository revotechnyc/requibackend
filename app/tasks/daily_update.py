"""
Daily self-update job
Runs every 24 hours to re-ingest sources and revalidate knowledge
"""

from datetime import datetime, timedelta
from uuid import UUID

from celery import shared_task
from sqlalchemy import select

from app.core.config import settings
from app.db.database import get_db_context
from app.db.models import (
    Document,
    GapTask,
    KnowledgeRecord,
    KnowledgeStatus,
    Organization,
    PlanType,
    Source,
    Subscription,
)
from app.services.retrieval import KnowledgeService


@shared_task
def run_daily_update():
    """Main daily update task - runs for all enterprise organizations"""
    async def _update():
        async with get_db_context() as db:
            # Get all enterprise organizations with active subscriptions
            result = await db.execute(
                select(Organization)
                .join(Subscription)
                .where(
                    Subscription.plan_type == PlanType.ENTERPRISE,
                    Subscription.status.in_(["active", "trialing"]),
                )
            )
            organizations = result.scalars().all()
            
            results = []
            for org in organizations:
                org_result = await update_organization(db, org.id)
                results.append(org_result)
            
            return {
                "timestamp": datetime.utcnow().isoformat(),
                "organizations_processed": len(results),
                "results": results,
            }
    
    import asyncio
    return asyncio.run(_update())


async def update_organization(db, organization_id: UUID) -> dict:
    """Update a single organization"""
    result = {
        "organization_id": str(organization_id),
        "sources_reingested": 0,
        "documents_processed": 0,
        "knowledge_flagged": 0,
        "errors": [],
    }
    
    try:
        # Step 1: Re-ingest active sources
        sources_result = await db.execute(
            select(Source).where(
                Source.organization_id == organization_id,
                Source.is_active == True,
                Source.ingest_frequency.in_(["daily", "weekly"]),
            )
        )
        sources = sources_result.scalars().all()
        
        for source in sources:
            # Check if it's time to re-ingest (daily or weekly)
            should_ingest = False
            if source.ingest_frequency == "daily":
                should_ingest = True
            elif source.ingest_frequency == "weekly" and source.last_ingested_at:
                days_since = (datetime.utcnow() - source.last_ingested_at).days
                should_ingest = days_since >= 7
            
            if should_ingest:
                from app.tasks.ingestion import ingest_source_task
                ingest_source_task.delay(str(source.id))
                result["sources_reingested"] += 1
        
        # Step 2: Flag stale knowledge records
        knowledge_service = KnowledgeService()
        stale_records = await knowledge_service.flag_stale_knowledge(
            db, organization_id, days=settings.knowledge_stale_days
        )
        result["knowledge_flagged"] = len(stale_records)
        
        # Step 3: Create review tasks for flagged knowledge
        for record in stale_records:
            # Check if review task already exists
            existing = await db.execute(
                select(GapTask).where(
                    GapTask.proposed_knowledge_id == record.id,
                    GapTask.status.in_(["detected", "in_progress"]),
                )
            )
            if not existing.scalar_one_or_none():
                gap_task = GapTask(
                    organization_id=organization_id,
                    original_query=record.question,
                    gap_description=f"Knowledge record '{record.topic}' is stale and needs revalidation",
                    confidence_score=record.confidence_score,
                    status="detected",
                    proposed_knowledge_id=record.id,
                )
                db.add(gap_task)
        
        await db.commit()
        
        # Step 4: Count documents
        docs_result = await db.execute(
            select(Document).where(
                Document.organization_id == organization_id,
                Document.is_active == True,
            )
        )
        result["documents_processed"] = len(docs_result.scalars().all())
    
    except Exception as e:
        result["errors"].append(str(e))
    
    return result


@shared_task
def revalidate_knowledge_task(knowledge_id: str):
    """Revalidate a single knowledge record"""
    async def _revalidate():
        async with get_db_context() as db:
            result = await db.execute(
                select(KnowledgeRecord).where(KnowledgeRecord.id == UUID(knowledge_id))
            )
            record = result.scalar_one_or_none()
            
            if not record:
                return {"error": f"Knowledge record {knowledge_id} not found"}
            
            # TODO: Implement actual revalidation logic
            # This would re-query sources, check for updates, etc.
            
            record.last_validated_at = datetime.utcnow()
            await db.commit()
            
            return {
                "knowledge_id": knowledge_id,
                "status": "revalidated",
                "timestamp": datetime.utcnow().isoformat(),
            }
    
    import asyncio
    return asyncio.run(_revalidate())


@shared_task
def cleanup_old_audit_logs_task(days: int = 90):
    """Clean up audit logs older than specified days"""
    async def _cleanup():
        async with get_db_context() as db:
            from app.db.models import AuditLog
            
            cutoff_date = datetime.utcnow() - timedelta(days=days)
            
            result = await db.execute(
                select(AuditLog).where(AuditLog.created_at < cutoff_date)
            )
            old_logs = result.scalars().all()
            
            count = len(old_logs)
            for log in old_logs:
                await db.delete(log)
            
            await db.commit()
            
            return {
                "logs_deleted": count,
                "cutoff_date": cutoff_date.isoformat(),
            }
    
    import asyncio
    return asyncio.run(_cleanup())
