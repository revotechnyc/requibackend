"""In-app notifications for working product areas (Intelligence + trial usage)."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Notification,
    NotificationChannel,
    NotificationPreference,
    NotificationPriority,
    NotificationStatus,
    NotificationType,
    WORKING_IN_APP_NOTIFICATION_TYPES,
)

# ============================================================
# TEMPLATES — aligned with live Intelligence / trial features
# ============================================================

NOTIFICATION_TEMPLATES: Dict[NotificationType, Dict[str, Any]] = {
    NotificationType.WELCOME: {
        "title": "Welcome to Requi Health",
        "message": "Your compliance intelligence workspace is ready. Open Intelligence to ask questions, upload documents, or start a live voice session with Sonia.",
        "priority": NotificationPriority.HIGH,
        "cta_link": "/intelligence",
        "cta_label": "Open Intelligence",
        "icon": "sparkles",
    },
    NotificationType.TRIAL_STARTED: {
        "title": "Your trial is active",
        "message": "You have access to Requi Intelligence, including AI chat and live voice (RTC). Trial includes a daily AI prompt allowance.",
        "priority": NotificationPriority.HIGH,
        "cta_link": "/intelligence",
        "cta_label": "Start with Intelligence",
        "icon": "sparkles",
    },
    NotificationType.TRIAL_3_DAYS_LEFT: {
        "title": "{days_remaining} days left in your trial",
        "message": "Your trial ends soon. Upgrade to keep Intelligence chat, live voice, and saved conversations.",
        "priority": NotificationPriority.HIGH,
        "cta_link": "/pricing",
        "cta_label": "View plans",
        "icon": "zap",
    },
    NotificationType.TRIAL_1_DAY_LEFT: {
        "title": "Trial ends tomorrow",
        "message": "One day remains on your trial. Upgrade to avoid losing access to Intelligence and live voice.",
        "priority": NotificationPriority.CRITICAL,
        "cta_link": "/pricing",
        "cta_label": "Upgrade now",
        "icon": "zap",
    },
    NotificationType.TRIAL_EXPIRED: {
        "title": "Trial ended",
        "message": "Your trial has ended. Upgrade to continue using Intelligence and live voice sessions.",
        "priority": NotificationPriority.CRITICAL,
        "cta_link": "/pricing",
        "cta_label": "Choose a plan",
        "icon": "lock",
    },
    NotificationType.PROMPT_NEAR_LIMIT: {
        "title": "Almost at today’s AI limit",
        "message": "You’ve used {used} of {limit} Intelligence prompts today. {remaining} remaining before the daily reset.",
        "priority": NotificationPriority.MEDIUM,
        "cta_link": "/pricing",
        "cta_label": "Upgrade",
        "icon": "zap",
    },
    NotificationType.PROMPT_LIMIT_REACHED: {
        "title": "Daily Intelligence limit reached",
        "message": "You’ve used all {limit} trial prompts for today. Upgrade for continued access or try again after the daily reset.",
        "priority": NotificationPriority.HIGH,
        "cta_link": "/pricing",
        "cta_label": "Upgrade",
        "icon": "zap",
    },
    NotificationType.LIVE_VOICE_CONNECTED: {
        "title": "Live voice session started",
        "message": "You’re connected to Sonia via secure real-time voice. Speak naturally — your conversation is saved to Intelligence history.",
        "priority": NotificationPriority.MEDIUM,
        "cta_link": "/intelligence",
        "cta_label": "Back to Intelligence",
        "icon": "sparkles",
    },
    NotificationType.LIVE_VOICE_ENDED: {
        "title": "Live voice session ended",
        "message": "Your live session with Sonia has ended. Review the transcript anytime under Recent Conversations in Intelligence.",
        "priority": NotificationPriority.LOW,
        "cta_link": "/intelligence",
        "cta_label": "View conversations",
        "icon": "sparkles",
    },
    NotificationType.LIVE_VOICE_TURN_SAVED: {
        "title": "Live turn saved",
        "message": "Your latest live voice exchange was saved to {conversation_title}.",
        "priority": NotificationPriority.LOW,
        "cta_link": "/intelligence",
        "cta_label": "Continue",
        "icon": "sparkles",
    },
    NotificationType.AI_RESPONSE_READY: {
        "title": "Intelligence response ready",
        "message": "Your AI answer is ready in {conversation_title}. Open Intelligence to read the full response and sources.",
        "priority": NotificationPriority.LOW,
        "cta_link": "/intelligence",
        "cta_label": "Open chat",
        "icon": "sparkles",
    },
    NotificationType.CHAT_SHARED_IMPORTED: {
        "title": "Shared chat added to Intelligence",
        "message": "A shared conversation was imported into your workspace. Continue the thread in Intelligence.",
        "priority": NotificationPriority.MEDIUM,
        "cta_link": "/intelligence",
        "cta_label": "Open Intelligence",
        "icon": "sparkles",
    },
}


def notification_icon_for_type(notif_type: NotificationType) -> str:
    template = NOTIFICATION_TEMPLATES.get(notif_type, {})
    return str(template.get("icon") or "info")


class NotificationService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_notification(
        self,
        user_id: uuid.UUID,
        org_id: Optional[uuid.UUID],
        notif_type: NotificationType,
        channel: NotificationChannel = NotificationChannel.IN_APP,
        template_vars: Optional[Dict[str, str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        scheduled_for: Optional[datetime] = None,
        *,
        allow_duplicate_within_minutes: int = 0,
    ) -> Optional[Notification]:
        if notif_type not in WORKING_IN_APP_NOTIFICATION_TYPES:
            return None
        if channel != NotificationChannel.IN_APP:
            return None

        if allow_duplicate_within_minutes > 0:
            cutoff = datetime.utcnow() - timedelta(minutes=allow_duplicate_within_minutes)
            existing = await self.db.execute(
                select(Notification).where(
                    and_(
                        Notification.user_id == user_id,
                        Notification.type == notif_type,
                        Notification.channel == NotificationChannel.IN_APP,
                        Notification.created_at >= cutoff,
                        Notification.dismissed_at.is_(None),
                    )
                ).limit(1)
            )
            if existing.scalar_one_or_none():
                return None

        template = NOTIFICATION_TEMPLATES.get(
            notif_type,
            {
                "title": "Requi Health",
                "message": "You have a new update in Intelligence.",
                "priority": NotificationPriority.MEDIUM,
                "icon": "info",
            },
        )

        title = template["title"]
        message = template["message"]
        cta_link = template.get("cta_link")
        if template_vars:
            for key, val in template_vars.items():
                token = f"{{{key}}}"
                title = title.replace(token, str(val))
                message = message.replace(token, str(val))
                if cta_link and token in cta_link:
                    cta_link = cta_link.replace(token, str(val))

        notification = Notification(
            id=uuid.uuid4(),
            user_id=user_id,
            organization_id=org_id,
            type=notif_type,
            status=NotificationStatus.DELIVERED,
            priority=template.get("priority", NotificationPriority.MEDIUM),
            title=title,
            message=message,
            cta_link=cta_link,
            cta_label=template.get("cta_label"),
            channel=channel,
            metadata_json=json.dumps(metadata) if metadata else None,
            scheduled_for=scheduled_for,
            delivered_at=datetime.utcnow(),
        )

        self.db.add(notification)
        await self.db.commit()
        await self.db.refresh(notification)
        return notification

    async def get_user_notifications(
        self,
        user_id: uuid.UUID,
        limit: int = 50,
        offset: int = 0,
        unread_only: bool = False,
    ) -> List[Notification]:
        query = (
            select(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.channel == NotificationChannel.IN_APP,
                Notification.type.in_(WORKING_IN_APP_NOTIFICATION_TYPES),
                Notification.dismissed_at.is_(None),
            )
            .order_by(desc(Notification.created_at))
        )
        if unread_only:
            query = query.where(Notification.opened_at.is_(None))
        result = await self.db.execute(query.limit(limit).offset(offset))
        return list(result.scalars().all())

    async def get_unread_count(self, user_id: uuid.UUID) -> int:
        result = await self.db.execute(
            select(Notification.id).where(
                and_(
                    Notification.user_id == user_id,
                    Notification.channel == NotificationChannel.IN_APP,
                    Notification.type.in_(WORKING_IN_APP_NOTIFICATION_TYPES),
                    Notification.opened_at.is_(None),
                    Notification.dismissed_at.is_(None),
                )
            )
        )
        return len(result.scalars().all())

    async def mark_as_read(self, notification_id: uuid.UUID, user_id: uuid.UUID) -> None:
        await self.db.execute(
            update(Notification)
            .where(
                Notification.id == notification_id,
                Notification.user_id == user_id,
            )
            .values(status=NotificationStatus.OPENED, opened_at=datetime.utcnow())
        )
        await self.db.commit()

    async def mark_all_as_read(self, user_id: uuid.UUID) -> None:
        await self.db.execute(
            update(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.opened_at.is_(None),
                Notification.channel == NotificationChannel.IN_APP,
            )
            .values(status=NotificationStatus.OPENED, opened_at=datetime.utcnow())
        )
        await self.db.commit()

    async def dismiss(self, notification_id: uuid.UUID, user_id: uuid.UUID) -> None:
        await self.db.execute(
            update(Notification)
            .where(
                Notification.id == notification_id,
                Notification.user_id == user_id,
            )
            .values(status=NotificationStatus.DISMISSED, dismissed_at=datetime.utcnow())
        )
        await self.db.commit()

    async def create_welcome_sequence(
        self, user_id: uuid.UUID, org_id: Optional[uuid.UUID]
    ) -> List[Notification]:
        created: List[Notification] = []
        for notif_type in (NotificationType.WELCOME, NotificationType.TRIAL_STARTED):
            n = await self.create_notification(user_id, org_id, notif_type)
            if n:
                created.append(n)
        return created

    async def notify_prompt_usage(
        self,
        user_id: uuid.UUID,
        org_id: Optional[uuid.UUID],
        *,
        used: int,
        limit: int,
    ) -> None:
        remaining = max(0, limit - used)
        if used >= limit:
            await self.create_notification(
                user_id,
                org_id,
                NotificationType.PROMPT_LIMIT_REACHED,
                template_vars={
                    "used": str(used),
                    "limit": str(limit),
                    "remaining": "0",
                },
                allow_duplicate_within_minutes=120,
            )
        elif remaining == 1:
            await self.create_notification(
                user_id,
                org_id,
                NotificationType.PROMPT_NEAR_LIMIT,
                template_vars={
                    "used": str(used),
                    "limit": str(limit),
                    "remaining": str(remaining),
                },
                allow_duplicate_within_minutes=60,
            )
