"""Notification API endpoints for REQUI."""
import uuid
from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query, Body, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_current_user
from app.services.notification_service import NotificationService
from app.services.email_service import get_email_service
from app.services.scheduler_service import NotificationScheduler
from app.models.notification import (
    Notification, NotificationType, NotificationStatus, NotificationPriority,
    NotificationChannel, NotificationPreference
)

router = APIRouter(prefix="/notifications", tags=["notifications"])


# ============================================================
# SCHEMAS
# ============================================================

class NotificationResponse(BaseModel):
    id: str
    type: str
    status: str
    priority: str
    title: str
    message: str
    cta_link: Optional[str] = None
    cta_label: Optional[str] = None
    channel: str
    created_at: datetime
    opened_at: Optional[datetime] = None
    dismissed_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class NotificationListResponse(BaseModel):
    notifications: List[NotificationResponse]
    total: int
    unread_count: int


class CreateNotificationRequest(BaseModel):
    type: str = Field(..., description="Notification type (e.g., 'welcome', 'trial_3_days_left')")
    channel: str = Field(default="in_app", description="Delivery channel")
    template_vars: Optional[dict] = Field(default=None, description="Template variable replacements")
    metadata: Optional[dict] = Field(default=None)
    scheduled_for: Optional[datetime] = Field(default=None)


class UpdatePreferencesRequest(BaseModel):
    email_enabled: Optional[bool] = None
    in_app_enabled: Optional[bool] = None
    push_enabled: Optional[bool] = None
    trial_notifications: Optional[bool] = None
    team_notifications: Optional[bool] = None
    billing_notifications: Optional[bool] = None
    security_notifications: Optional[bool] = None
    workspace_notifications: Optional[bool] = None
    ai_notifications: Optional[bool] = None
    system_notifications: Optional[bool] = None


class BulkMarkReadRequest(BaseModel):
    notification_ids: Optional[List[str]] = Field(default=None, description="Specific IDs to mark. If null, marks all.")


# ============================================================
# ENDPOINTS
# ============================================================

@router.get("/", response_model=NotificationListResponse)
async def list_notifications(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    unread_only: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Get notifications for the current user."""
    svc = NotificationService(db)
    notifications = await svc.get_user_notifications(
        user_id=current_user.id,
        limit=limit,
        offset=offset,
        unread_only=unread_only,
    )
    unread_count = await svc.get_unread_count(current_user.id)

    return NotificationListResponse(
        notifications=[NotificationResponse.model_validate(n) for n in notifications],
        total=len(notifications),
        unread_count=unread_count,
    )


@router.get("/unread-count")
async def get_unread_count(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Get unread notification count (for badge)."""
    svc = NotificationService(db)
    count = await svc.get_unread_count(current_user.id)
    return {"unread_count": count}


@router.post("/{notification_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_as_read(
    notification_id: str,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Mark a single notification as read."""
    svc = NotificationService(db)
    await svc.mark_as_read(uuid.UUID(notification_id))


@router.post("/read-all", status_code=status.HTTP_204_NO_CONTENT)
async def mark_all_as_read(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Mark all notifications as read."""
    svc = NotificationService(db)
    await svc.mark_all_as_read(current_user.id)


@router.post("/{notification_id}/dismiss", status_code=status.HTTP_204_NO_CONTENT)
async def dismiss_notification(
    notification_id: str,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Dismiss a notification."""
    svc = NotificationService(db)
    await svc.dismiss(uuid.UUID(notification_id))


@router.post("/send", response_model=NotificationResponse, status_code=status.HTTP_201_CREATED)
async def create_notification(
    request: CreateNotificationRequest,
    user_id: Optional[str] = Query(None, description="Target user ID (admin only)"),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Create a notification (admin only for other users)."""
    # TODO: Add admin permission check
    target_user_id = uuid.UUID(user_id) if user_id else current_user.id

    svc = NotificationService(db)
    notif = await svc.create_notification(
        user_id=target_user_id,
        org_id=getattr(current_user, "organization_id", None),
        notif_type=NotificationType(request.type),
        channel=NotificationChannel(request.channel),
        template_vars=request.template_vars,
        metadata=request.metadata,
        scheduled_for=request.scheduled_for,
    )
    return NotificationResponse.model_validate(notif)


@router.get("/preferences")
async def get_preferences(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Get notification preferences."""
    svc = NotificationService(db)
    prefs = await svc.get_preferences(current_user.id)
    if not prefs:
        return {"preferences": None, "defaults": {
            "email_enabled": True,
            "in_app_enabled": True,
            "push_enabled": False,
            "trial_notifications": True,
            "team_notifications": True,
            "billing_notifications": True,
            "security_notifications": True,
            "workspace_notifications": True,
            "ai_notifications": True,
            "system_notifications": True,
        }}
    return {"preferences": {
        "email_enabled": prefs.email_enabled,
        "in_app_enabled": prefs.in_app_enabled,
        "push_enabled": prefs.push_enabled,
        "trial_notifications": prefs.trial_notifications,
        "team_notifications": prefs.team_notifications,
        "billing_notifications": prefs.billing_notifications,
        "security_notifications": prefs.security_notifications,
        "workspace_notifications": prefs.workspace_notifications,
        "ai_notifications": prefs.ai_notifications,
        "system_notifications": prefs.system_notifications,
    }}


@router.put("/preferences")
async def update_preferences(
    request: UpdatePreferencesRequest,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Update notification preferences."""
    svc = NotificationService(db)
    updated = await svc.update_preferences(
        current_user.id,
        **{k: v for k, v in request.model_dump().items() if v is not None}
    )
    return {"status": "updated", "preferences": {
        "email_enabled": updated.email_enabled,
        "in_app_enabled": updated.in_app_enabled,
        "push_enabled": updated.push_enabled,
    }}


# ============================================================
# ADMIN ENDPOINTS
# ============================================================

@router.post("/admin/broadcast")
async def broadcast_notification(
    title: str = Body(...),
    message: str = Body(...),
    notif_type: str = Body("new_feature_released"),
    priority: str = Body("low"),
    cta_link: Optional[str] = Body(None),
    cta_label: Optional[str] = Body(None),
    org_id: Optional[str] = Body(None),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Broadcast a notification to all users in an organization (admin only)."""
    # TODO: Add admin permission check
    svc = NotificationService(db)

    # For all users in org
    # TODO: Query all users and create notifications for each

    return {"status": "broadcast_queued", "recipients": 0}


@router.post("/admin/trial-sequence/{user_id}")
async def trigger_trial_sequence(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Manually trigger trial notification sequence (admin only)."""
    scheduler = NotificationScheduler(db)
    await scheduler.schedule_trial_sequence(
        user_id=uuid.UUID(user_id),
        org_id=getattr(current_user, "organization_id", None),
    )
    return {"status": "trial_sequence_scheduled", "user_id": user_id}
