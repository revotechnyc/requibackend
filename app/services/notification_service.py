"""Core notification service for REQUI."""
import json
import uuid
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, desc, update
from app.models.notification import (
    Notification, NotificationType, NotificationStatus, NotificationPriority,
    NotificationChannel, NotificationPreference
)


# ============================================================
# NOTIFICATION TEMPLATES
# ============================================================

NOTIFICATION_TEMPLATES: Dict[str, Dict[str, Any]] = {
    # === WELCOME ===
    NotificationType.WELCOME: {
        "title": "Welcome to REQUI",
        "message": "Your AI-powered compliance intelligence platform is ready. Start exploring REQUI Intelligence to analyze your compliance posture.",
        "priority": NotificationPriority.HIGH,
        "cta_link": "/dashboard",
        "cta_label": "Get Started",
    },
    NotificationType.TRIAL_STARTED: {
        "title": "Your 7-Day Trial is Active",
        "message": "You now have full access to REQUI Intelligence for 7 days. Ask about HIPAA, FWA, compliance requirements, or upload documents for analysis.",
        "priority": NotificationPriority.HIGH,
        "cta_link": "/intelligence",
        "cta_label": "Start Using AI",
    },
    # === TRIAL REMINDERS ===
    NotificationType.TRIAL_3_DAYS_LEFT: {
        "title": "3 Days Left in Your Trial",
        "message": "Your REQUI trial expires in 3 days. Upgrade to keep your compliance intelligence access.",
        "priority": NotificationPriority.HIGH,
        "cta_link": "/pricing",
        "cta_label": "Upgrade Now",
    },
    NotificationType.TRIAL_1_DAY_LEFT: {
        "title": "Your Trial Ends Tomorrow",
        "message": "Just 1 day remaining. Don't lose access to your compliance AI, tasks, and analytics.",
        "priority": NotificationPriority.CRITICAL,
        "cta_link": "/pricing",
        "cta_label": "Upgrade Now",
    },
    NotificationType.TRIAL_EXPIRED: {
        "title": "Your Trial Has Expired",
        "message": "Your 7-day trial has ended. Upgrade to continue using REQUI's AI compliance intelligence.",
        "priority": NotificationPriority.CRITICAL,
        "cta_link": "/pricing",
        "cta_label": "Choose a Plan",
    },
    # === TEAM INVITES ===
    NotificationType.TEAM_INVITE_RECEIVED: {
        "title": "You've Been Invited to {org_name}",
        "message": "{inviter_name} invited you to join {org_name} as a {role}. Accept to access the workspace.",
        "priority": NotificationPriority.HIGH,
        "cta_link": "/teams/accept?token={invite_token}",
        "cta_label": "Accept Invitation",
    },
    NotificationType.TEAM_INVITE_REMINDER: {
        "title": "Reminder: Team Invitation Pending",
        "message": "You have a pending invitation to join {org_name}. This invitation will expire soon.",
        "priority": NotificationPriority.MEDIUM,
        "cta_link": "/teams/accept?token={invite_token}",
        "cta_label": "Accept Now",
    },
    NotificationType.TEAM_INVITE_ACCEPTED: {
        "title": "Invitation Accepted",
        "message": "{invitee_name} accepted your invitation to join {org_name} as a {role}.",
        "priority": NotificationPriority.MEDIUM,
        "cta_link": "/teams",
        "cta_label": "View Team",
    },
    # === RATE LIMITING ===
    NotificationType.PROMPT_DAILY_WARNING: {
        "title": "AI Prompt Usage: {used}/{limit}",
        "message": "You've used {used} of your {limit} daily AI prompts. Your limit resets in {hours} hours.",
        "priority": NotificationPriority.LOW,
        "cta_link": "/pricing",
        "cta_label": "Upgrade for More",
    },
    NotificationType.PROMPT_NEAR_LIMIT: {
        "title": "Almost at Daily Limit",
        "message": "You've used {used}/{limit} prompts today. 1 prompt remaining. Upgrade for unlimited access.",
        "priority": NotificationPriority.MEDIUM,
        "cta_link": "/pricing",
        "cta_label": "Upgrade",
    },
    NotificationType.PROMPT_LIMIT_REACHED: {
        "title": "Daily Prompt Limit Reached",
        "message": "You've used all {limit} prompts for today. Your limit resets in {hours} hours, or upgrade for unlimited access.",
        "priority": NotificationPriority.HIGH,
        "cta_link": "/pricing",
        "cta_label": "Upgrade Now",
    },
    # === BILLING ===
    NotificationType.PAYMENT_SUCCESS: {
        "title": "Payment Successful",
        "message": "Your payment of ${amount} for {plan} has been processed. Thank you for subscribing to REQUI.",
        "priority": NotificationPriority.MEDIUM,
        "cta_link": "/settings/billing",
        "cta_label": "View Invoice",
    },
    NotificationType.PAYMENT_FAILED: {
        "title": "Payment Failed",
        "message": "We couldn't process your payment of ${amount}. Please update your payment method to avoid service interruption.",
        "priority": NotificationPriority.CRITICAL,
        "cta_link": "/settings/billing",
        "cta_label": "Update Payment",
    },
    NotificationType.INVOICE_AVAILABLE: {
        "title": "Invoice Available",
        "message": "Your invoice for {period} is ready. Amount: ${amount}.",
        "priority": NotificationPriority.LOW,
        "cta_link": "/settings/billing",
        "cta_label": "View Invoice",
    },
    # === SECURITY ===
    NotificationType.NEW_LOGIN_DETECTED: {
        "title": "New Login Detected",
        "message": "A new login was detected from {device} in {location} at {time}. If this wasn't you, please secure your account.",
        "priority": NotificationPriority.HIGH,
        "cta_link": "/settings/security",
        "cta_label": "Review Activity",
    },
    NotificationType.SUSPICIOUS_LOGIN: {
        "title": "Suspicious Login Attempt",
        "message": "We detected a suspicious login attempt to your account from {ip_address}. We blocked this attempt for your security.",
        "priority": NotificationPriority.CRITICAL,
        "cta_link": "/settings/security",
        "cta_label": "Secure Account",
    },
    # === WORKSPACE ===
    NotificationType.TASK_ASSIGNED: {
        "title": "Task Assigned: {task_title}",
        "message": "{assigner_name} assigned you to {task_title}. Due: {due_date}. Priority: {priority}.",
        "priority": NotificationPriority.MEDIUM,
        "cta_link": "/tasks/{task_id}",
        "cta_label": "View Task",
    },
    NotificationType.TASK_OVERDUE: {
        "title": "Task Overdue: {task_title}",
        "message": "{task_title} is now overdue (was due {due_date}). Please update the status or request an extension.",
        "priority": NotificationPriority.HIGH,
        "cta_link": "/tasks/{task_id}",
        "cta_label": "Update Task",
    },
    NotificationType.COMMENT_MENTION: {
        "title": "You Were Mentioned",
        "message": "{author_name} mentioned you in a comment on {task_title}: \"{comment_preview}\"",
        "priority": NotificationPriority.MEDIUM,
        "cta_link": "/tasks/{task_id}",
        "cta_label": "View Comment",
    },
    # === AI ===
    NotificationType.KNOWLEDGE_GAP_DETECTED: {
        "title": "Compliance Gap Detected",
        "message": "Our AI identified a potential compliance gap in {category}. A task has been suggested to address it.",
        "priority": NotificationPriority.HIGH,
        "cta_link": "/tasks?filter=suggested",
        "cta_label": "Review Task",
    },
    NotificationType.NEW_REGULATORY_UPDATE: {
        "title": "New Regulatory Update",
        "message": "A new update from {source} affects your compliance requirements. Review the changes and update your documentation.",
        "priority": NotificationPriority.MEDIUM,
        "cta_link": "/compliance/updates/{update_id}",
        "cta_label": "Review Update",
    },
    # === SYSTEM ===
    NotificationType.MAINTENANCE_SCHEDULED: {
        "title": "Scheduled Maintenance",
        "message": "REQUI will undergo scheduled maintenance on {date} from {start_time} to {end_time} UTC. Expect brief service interruption.",
        "priority": NotificationPriority.MEDIUM,
        "cta_link": "/status",
        "cta_label": "View Status",
    },
    NotificationType.NEW_FEATURE_RELEASED: {
        "title": "New Feature: {feature_name}",
        "message": "{feature_name} is now available. {feature_description}",
        "priority": NotificationPriority.LOW,
        "cta_link": "/updates/{feature_id}",
        "cta_label": "Learn More",
    },
    NotificationType.UPGRADE_SUGGESTED: {
        "title": "Unlock More with {target_plan}",
        "message": "You're using {feature_name}, available on {target_plan}. Upgrade to access {feature_list}.",
        "priority": NotificationPriority.LOW,
        "cta_link": "/pricing",
        "cta_label": "Compare Plans",
    },
}


# ============================================================
# NOTIFICATION SERVICE
# ============================================================

class NotificationService:
    """Core notification creation, delivery, and management."""

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
    ) -> Notification:
        """Create a new notification from a template."""
        template = NOTIFICATION_TEMPLATES.get(notif_type, {
            "title": "REQUI Notification",
            "message": "You have a new notification.",
            "priority": NotificationPriority.MEDIUM,
        })

        # Render template variables
        title = template["title"]
        message = template["message"]
        if template_vars:
            for key, val in template_vars.items():
                title = title.replace(f"{{{key}}}", str(val))
                message = message.replace(f"{{{key}}}", str(val))

        notification = Notification(
            id=uuid.uuid4(),
            user_id=user_id,
            organization_id=org_id,
            type=notif_type,
            status=NotificationStatus.QUEUED,
            priority=template.get("priority", NotificationPriority.MEDIUM),
            title=title,
            message=message,
            cta_link=template.get("cta_link"),
            cta_label=template.get("cta_label"),
            channel=channel,
            email_subject=title if channel == NotificationChannel.EMAIL else None,
            metadata_json=json.dumps(metadata) if metadata else None,
            scheduled_for=scheduled_for,
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
        """Get notifications for a user, ordered by newest first."""
        query = select(Notification).where(
            Notification.user_id == user_id
        ).order_by(desc(Notification.created_at))

        if unread_only:
            query = query.where(Notification.opened_at.is_(None))

        result = await self.db.execute(query.limit(limit).offset(offset))
        return result.scalars().all()

    async def get_unread_count(self, user_id: uuid.UUID) -> int:
        """Get unread notification count for badge."""
        result = await self.db.execute(
            select(Notification).where(
                and_(
                    Notification.user_id == user_id,
                    Notification.opened_at.is_(None),
                    Notification.dismissed_at.is_(None),
                )
            )
        )
        return len(result.scalars().all())

    async def mark_as_read(self, notification_id: uuid.UUID) -> None:
        """Mark a single notification as opened."""
        await self.db.execute(
            update(Notification).where(Notification.id == notification_id).values(
                status=NotificationStatus.OPENED,
                opened_at=datetime.utcnow(),
            )
        )
        await self.db.commit()

    async def mark_all_as_read(self, user_id: uuid.UUID) -> None:
        """Mark all notifications as opened."""
        await self.db.execute(
            update(Notification).where(
                and_(
                    Notification.user_id == user_id,
                    Notification.opened_at.is_(None),
                )
            ).values(
                status=NotificationStatus.OPENED,
                opened_at=datetime.utcnow(),
            )
        )
        await self.db.commit()

    async def dismiss(self, notification_id: uuid.UUID) -> None:
        """Soft-dismiss a notification."""
        await self.db.execute(
            update(Notification).where(Notification.id == notification_id).values(
                status=NotificationStatus.DISMISSED,
                dismissed_at=datetime.utcnow(),
            )
        )
        await self.db.commit()

    async def create_welcome_sequence(self, user_id: uuid.UUID, org_id: Optional[uuid.UUID]) -> List[Notification]:
        """Create the welcome notification sequence for new users."""
        notifications = []

        # Welcome (immediate)
        welcome = await self.create_notification(
            user_id, org_id, NotificationType.WELCOME,
            channel=NotificationChannel.IN_APP,
        )
        notifications.append(welcome)

        # Welcome email (immediate)
        welcome_email = await self.create_notification(
            user_id, org_id, NotificationType.WELCOME,
            channel=NotificationChannel.EMAIL,
        )
        notifications.append(welcome_email)

        # Trial started (immediate)
        trial = await self.create_notification(
            user_id, org_id, NotificationType.TRIAL_STARTED,
            channel=NotificationChannel.IN_APP,
        )
        notifications.append(trial)

        return notifications

    async def create_trial_sequence(self, user_id: uuid.UUID, org_id: Optional[uuid.UUID]) -> List[Notification]:
        """Create the 7-day trial reminder sequence."""
        now = datetime.utcnow()
        notifications = []

        # 3 days remaining (Day 4)
        n3 = await self.create_notification(
            user_id, org_id, NotificationType.TRIAL_3_DAYS_LEFT,
            channel=NotificationChannel.EMAIL,
            scheduled_for=now + timedelta(days=4),
            template_vars={"days_remaining": "3"},
        )
        notifications.append(n3)

        # 1 day remaining (Day 6)
        n1 = await self.create_notification(
            user_id, org_id, NotificationType.TRIAL_1_DAY_LEFT,
            channel=NotificationChannel.EMAIL,
            scheduled_for=now + timedelta(days=6),
        )
        notifications.append(n1)

        # Expired (Day 7)
        n_exp = await self.create_notification(
            user_id, org_id, NotificationType.TRIAL_EXPIRED,
            channel=NotificationChannel.EMAIL,
            scheduled_for=now + timedelta(days=7),
        )
        notifications.append(n_exp)

        # Expiry reminder (Day 8)
        n_rem = await self.create_notification(
            user_id, org_id, NotificationType.TRIAL_EXPIRED_REMINDER,
            channel=NotificationChannel.EMAIL,
            scheduled_for=now + timedelta(days=8),
        )
        notifications.append(n_rem)

        return notifications

    async def create_team_invite_notification(
        self,
        invitee_user_id: uuid.UUID,
        inviter_name: str,
        org_id: uuid.UUID,
        org_name: str,
        role: str,
        invite_token: str,
    ) -> Notification:
        """Send team invitation to invitee."""
        return await self.create_notification(
            user_id=invitee_user_id,
            org_id=org_id,
            notif_type=NotificationType.TEAM_INVITE_RECEIVED,
            channel=NotificationChannel.EMAIL,
            template_vars={
                "inviter_name": inviter_name,
                "org_name": org_name,
                "role": role,
                "invite_token": invite_token,
            },
            metadata={"invite_token": invite_token, "role": role},
        )

    async def create_rate_limit_notification(
        self,
        user_id: uuid.UUID,
        used: int,
        limit: int,
        hours_remaining: int,
    ) -> Optional[Notification]:
        """Create rate limit notification based on usage."""
        if used < limit - 1:
            return None  # No notification yet
        elif used == limit - 1:
            notif_type = NotificationType.PROMPT_NEAR_LIMIT
        elif used >= limit:
            notif_type = NotificationType.PROMPT_LIMIT_REACHED
        else:
            return None

        return await self.create_notification(
            user_id=user_id,
            org_id=None,
            notif_type=notif_type,
            channel=NotificationChannel.IN_APP,
            template_vars={
                "used": str(used),
                "limit": str(limit),
                "hours": str(hours_remaining),
            },
        )

    async def get_preferences(self, user_id: uuid.UUID) -> Optional[NotificationPreference]:
        """Get notification preferences for a user."""
        result = await self.db.execute(
            select(NotificationPreference).where(NotificationPreference.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def update_preferences(
        self, user_id: uuid.UUID, **kwargs
    ) -> NotificationPreference:
        """Update notification preferences."""
        pref = await self.get_preferences(user_id)
        if not pref:
            pref = NotificationPreference(user_id=user_id)
            self.db.add(pref)

        for key, val in kwargs.items():
            if hasattr(pref, key):
                setattr(pref, key, val)

        await self.db.commit()
        await self.db.refresh(pref)
        return pref
