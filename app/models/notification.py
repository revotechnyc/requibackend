"""Re-export notification models from the primary SQLAlchemy metadata."""

from app.db.models import (
    Notification,
    NotificationChannel,
    NotificationPreference,
    NotificationPriority,
    NotificationStatus,
    NotificationType,
    WORKING_IN_APP_NOTIFICATION_TYPES,
)

__all__ = [
    "Notification",
    "NotificationChannel",
    "NotificationPreference",
    "NotificationPriority",
    "NotificationStatus",
    "NotificationType",
    "WORKING_IN_APP_NOTIFICATION_TYPES",
]
