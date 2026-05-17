"""Scheduled notification jobs for REQUI.
Uses Celery + Redis for async task scheduling.
DevOps: Configure Celery worker and Redis broker.
"""
from datetime import datetime, timedelta
from typing import Optional
import uuid
from app.services.notification_service import NotificationService
from app.services.email_service import get_email_service
from app.models.notification import NotificationType, NotificationChannel


class NotificationScheduler:
    """Manages scheduled notification sequences (trials, reminders, etc.)."""

    def __init__(self, db):
        self.db = db
        self.notification = NotificationService(db)
        self.email = get_email_service()

    # ============================================================
    # TRIAL SEQUENCE
    # ============================================================

    async def schedule_trial_sequence(self, user_id: uuid.UUID, org_id: Optional[uuid.UUID]):
        """Schedule the complete 7-day trial notification sequence."""
        now = datetime.utcnow()

        # Day 0: Welcome + trial started (immediate)
        await self.notification.create_welcome_sequence(user_id, org_id)

        # Day 4: 3 days remaining
        await self.notification.create_notification(
            user_id, org_id,
            NotificationType.TRIAL_3_DAYS_LEFT,
            channel=NotificationChannel.EMAIL,
            scheduled_for=now + timedelta(days=4),
        )

        # Day 6: 1 day remaining
        await self.notification.create_notification(
            user_id, org_id,
            NotificationType.TRIAL_1_DAY_LEFT,
            channel=NotificationChannel.EMAIL,
            scheduled_for=now + timedelta(days=6),
        )

        # Day 7: Trial expired
        await self.notification.create_notification(
            user_id, org_id,
            NotificationType.TRIAL_EXPIRED,
            channel=NotificationChannel.EMAIL,
            scheduled_for=now + timedelta(days=7),
        )

    # ============================================================
    # TEAM INVITE REMINDERS
    # ============================================================

    async def schedule_invite_reminders(
        self,
        invitee_user_id: uuid.UUID,
        inviter_name: str,
        org_id: uuid.UUID,
        org_name: str,
        role: str,
        invite_token: str,
    ):
        """Schedule invitation + reminder sequence."""
        now = datetime.utcnow()

        # Immediate invitation
        await self.notification.create_team_invite_notification(
            invitee_user_id, inviter_name, org_id, org_name, role, invite_token
        )

        # 48-hour reminder
        await self.notification.create_notification(
            user_id=invitee_user_id,
            org_id=org_id,
            notif_type=NotificationType.TEAM_INVITE_REMINDER,
            channel=NotificationChannel.EMAIL,
            scheduled_for=now + timedelta(hours=48),
            template_vars={"org_name": org_name, "invite_token": invite_token},
        )

    # ============================================================
    # OVERDUE TASK REMINDERS
    # ============================================================

    async def schedule_overdue_task_reminder(
        self,
        assignee_id: uuid.UUID,
        task_id: str,
        task_title: str,
        due_date: str,
    ):
        """Send task overdue notification."""
        await self.notification.create_notification(
            user_id=assignee_id,
            org_id=None,
            notif_type=NotificationType.TASK_OVERDUE,
            channel=NotificationChannel.IN_APP,
            template_vars={
                "task_title": task_title,
                "task_id": task_id,
                "due_date": due_date,
            },
        )

    # ============================================================
    # CRON JOBS (called by Celery beat)
    # ============================================================

    async def process_scheduled_notifications(self):
        """Process notifications that are due for delivery.
        Runs every minute via Celery beat schedule.
        """
        from sqlalchemy import select, and_
        from app.models.notification import Notification

        now = datetime.utcnow()

        # Find due notifications
        result = await self.db.execute(
            select(Notification).where(
                and_(
                    Notification.status == "queued",
                    Notification.scheduled_for <= now,
                )
            )
        )
        notifications = result.scalars().all()

        for notif in notifications:
            # Deliver via appropriate channel
            if notif.channel == "email":
                await self._deliver_email(notif)
            elif notif.channel == "in_app":
                await self._deliver_in_app(notif)

            # Update status
            notif.status = "sent"
            notif.sent_at = now

        await self.db.commit()
        return len(notifications)

    async def _deliver_email(self, notif):
        """Send email via EmailService."""
        # TODO: Look up user's email from users table
        user_email = f"user_{notif.user_id}@example.com"
        await self.email.send(
            to_email=user_email,
            subject=notif.title,
            title=notif.title,
            message=notif.message,
            cta_link=notif.cta_link,
            cta_label=notif.cta_label,
        )

    async def _deliver_in_app(self, notif):
        """Mark as delivered for in-app notifications."""
        notif.delivered_at = datetime.utcnow()

    async def check_expiring_trials(self):
        """Check for trials expiring in 3 days, 1 day, today.
        Runs daily at 00:00 UTC.
        """
        from sqlalchemy import select, and_
        from app.models.user import User  # placeholder

        now = datetime.utcnow()

        # Trials expiring in 3 days
        expiry_3d = now + timedelta(days=3)
        # Trials expiring in 1 day
        expiry_1d = now + timedelta(days=1)
        # Trials expiring today
        expiry_0d = now

        # TODO: Query users table for trials expiring soon
        # For each user, create appropriate notification
        # This is a placeholder - implement with actual user model

    async def check_overdue_tasks(self):
        """Check for overdue tasks and send notifications.
        Runs every hour.
        """
        from sqlalchemy import select
        from app.models.task import Task  # placeholder

        now = datetime.utcnow()

        # Find tasks past due date with status not completed/approved
        # Create TASK_OVERDUE notifications for assignees
        # This is a placeholder - implement with actual task model
