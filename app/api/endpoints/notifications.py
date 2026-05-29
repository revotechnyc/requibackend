"""Notification API — in-app feed for Intelligence and trial usage."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.endpoints.auth import get_current_active_user
from app.db.database import get_db
from app.db.models import NotificationType, User
from app.services.notification_service import NotificationService, notification_icon_for_type

router = APIRouter(prefix="/notifications", tags=["notifications"])


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
    icon: str
    created_at: datetime
    opened_at: Optional[datetime] = None
    dismissed_at: Optional[datetime] = None
    read: bool

    class Config:
        from_attributes = True


class NotificationListResponse(BaseModel):
    notifications: List[NotificationResponse]
    total: int
    unread_count: int


class IntelligenceEventRequest(BaseModel):
    event: str = Field(
        ...,
        description="live_voice_ended — client reports RTC session ended",
    )
    conversation_title: Optional[str] = None


CLIENT_INTELLIGENCE_EVENTS = {
    "live_voice_ended": NotificationType.LIVE_VOICE_ENDED,
}


def _to_response(n) -> NotificationResponse:
    return NotificationResponse(
        id=str(n.id),
        type=n.type.value,
        status=n.status.value,
        priority=n.priority.value,
        title=n.title,
        message=n.message,
        cta_link=n.cta_link,
        cta_label=n.cta_label,
        channel=n.channel.value,
        icon=notification_icon_for_type(n.type),
        created_at=n.created_at,
        opened_at=n.opened_at,
        dismissed_at=n.dismissed_at,
        read=n.opened_at is not None,
    )


@router.get("/", response_model=NotificationListResponse)
async def list_notifications(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    unread_only: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    svc = NotificationService(db)
    notifications = await svc.get_user_notifications(
        user_id=current_user.id,
        limit=limit,
        offset=offset,
        unread_only=unread_only,
    )
    unread_count = await svc.get_unread_count(current_user.id)
    return NotificationListResponse(
        notifications=[_to_response(n) for n in notifications],
        total=len(notifications),
        unread_count=unread_count,
    )


@router.get("/unread-count")
async def get_unread_count(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    svc = NotificationService(db)
    count = await svc.get_unread_count(current_user.id)
    return {"unread_count": count}


@router.post("/{notification_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_as_read(
    notification_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    svc = NotificationService(db)
    await svc.mark_as_read(uuid.UUID(notification_id), current_user.id)


@router.post("/read-all", status_code=status.HTTP_204_NO_CONTENT)
async def mark_all_as_read(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    svc = NotificationService(db)
    await svc.mark_all_as_read(current_user.id)


@router.post("/{notification_id}/dismiss", status_code=status.HTTP_204_NO_CONTENT)
async def dismiss_notification(
    notification_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    svc = NotificationService(db)
    await svc.dismiss(uuid.UUID(notification_id), current_user.id)


@router.post("/intelligence-event", status_code=status.HTTP_201_CREATED)
async def intelligence_event(
    body: IntelligenceEventRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Record client-side Intelligence events (e.g. live voice ended)."""
    notif_type = CLIENT_INTELLIGENCE_EVENTS.get(body.event)
    if notif_type is None:
        raise HTTPException(status_code=400, detail="Unsupported intelligence event")

    org_id = None
    svc = NotificationService(db)
    vars_map = {}
    if body.conversation_title:
        vars_map["conversation_title"] = body.conversation_title.strip()[:120]

    notif = await svc.create_notification(
        current_user.id,
        org_id,
        notif_type,
        template_vars=vars_map or None,
        allow_duplicate_within_minutes=2,
    )
    if not notif:
        return {"created": False}
    return {"created": True, "notification": _to_response(notif)}
