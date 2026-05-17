"""
Admin endpoints for system management
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.endpoints.auth import get_current_active_user
from app.core.permissions import Feature, require_feature_dependency
from app.db.database import get_db
from app.db.models import (
    AuditLog,
    Document,
    GapTask,
    KnowledgeRecord,
    Organization,
    Source,
    Subscription,
    User,
    UserRole,
)
from app.tasks.daily_update import run_daily_update
from app.tasks.ingestion import ingest_source_task

router = APIRouter()


class StatsResponse(BaseModel):
    total_organizations: int
    total_users: int
    total_documents: int
    total_knowledge_records: int
    total_gap_tasks: int
    active_subscriptions: int


class DailyUpdateTrigger(BaseModel):
    organization_id: Optional[str] = None


@router.get("/stats", response_model=dict)
async def get_system_stats(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get system-wide statistics (superuser only)"""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Superuser access required")
    
    # Get counts
    org_count = await db.execute(select(func.count(Organization.id)))
    user_count = await db.execute(select(func.count(User.id)))
    doc_count = await db.execute(select(func.count(Document.id)))
    knowledge_count = await db.execute(select(func.count(KnowledgeRecord.id)))
    gap_count = await db.execute(select(func.count(GapTask.id)))
    sub_count = await db.execute(
        select(func.count(Subscription.id)).where(
            Subscription.status.in_(["active", "trialing"])
        )
    )
    
    return {
        "total_organizations": org_count.scalar(),
        "total_users": user_count.scalar(),
        "total_documents": doc_count.scalar(),
        "total_knowledge_records": knowledge_count.scalar(),
        "total_gap_tasks": gap_count.scalar(),
        "active_subscriptions": sub_count.scalar(),
    }


@router.get("/organizations", response_model=List[dict])
async def list_all_organizations(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """List all organizations (superuser only)"""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Superuser access required")
    
    result = await db.execute(select(Organization))
    orgs = result.scalars().all()
    
    return [
        {
            "id": str(o.id),
            "name": o.name,
            "slug": o.slug,
            "owner_email": o.owner.email,
            "member_count": len(o.seats),
            "subscription_plan": o.subscription.plan_type.value if o.subscription else None,
            "subscription_status": o.subscription.status.value if o.subscription else None,
            "created_at": o.created_at.isoformat(),
        }
        for o in orgs
    ]


@router.get("/audit-logs", response_model=List[dict])
async def get_audit_logs(
    organization_id: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = 100,
    organization: Organization = Depends(lambda: require_feature_dependency(Feature.AUDIT_LOGS)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get audit logs"""
    query = select(AuditLog).where(
        AuditLog.organization_id == organization.id,
    )
    
    if action:
        query = query.where(AuditLog.action == action)
    
    query = query.order_by(AuditLog.created_at.desc()).limit(limit)
    
    result = await db.execute(query)
    logs = result.scalars().all()
    
    return [
        {
            "id": str(l.id),
            "action": l.action,
            "resource_type": l.resource_type,
            "resource_id": str(l.resource_id) if l.resource_id else None,
            "user_email": l.user.email if l.user else None,
            "previous_state": l.previous_state,
            "new_state": l.new_state,
            "ip_address": l.ip_address,
            "created_at": l.created_at.isoformat(),
        }
        for l in logs
    ]


@router.post("/trigger-daily-update")
async def trigger_daily_update(
    data: DailyUpdateTrigger,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Trigger daily update job (Enterprise only)"""
    # Check if user is superuser or org admin
    if not current_user.is_superuser:
        # Check org admin
        from app.db.models import Seat
        
        seat_result = await db.execute(
            select(Seat).where(
                Seat.user_id == current_user.id,
                Seat.is_active == True,
            )
        )
        seat = seat_result.scalar_one_or_none()
        
        if not seat or seat.role != UserRole.ADMIN:
            raise HTTPException(status_code=403, detail="Admin access required")
    
    # Queue daily update
    task = run_daily_update.delay()
    
    return {
        "message": "Daily update triggered",
        "task_id": task.id,
    }


@router.post("/trigger-ingestion")
async def trigger_ingestion(
    source_id: str,
    organization: Organization = Depends(lambda: require_feature_dependency(Feature.ADMIN_DASHBOARD)),
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
    
    task = ingest_source_task.delay(str(source.id))
    
    return {
        "message": "Ingestion triggered",
        "task_id": task.id,
    }


@router.get("/health", response_model=dict)
async def system_health(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get system health status"""
    health = {
        "status": "healthy",
        "database": "unknown",
        "redis": "unknown",
    }
    
    # Check database
    try:
        await db.execute(select(1))
        health["database"] = "connected"
    except Exception:
        health["database"] = "disconnected"
        health["status"] = "unhealthy"
    
    # Check Redis
    try:
        from app.core.config import settings
        import redis
        
        r = redis.from_url(settings.redis_url)
        r.ping()
        health["redis"] = "connected"
    except Exception:
        health["redis"] = "disconnected"
        health["status"] = "unhealthy"
    
    return health
