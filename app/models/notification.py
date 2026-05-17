"""Notification database models for REQUI."""
import uuid
from datetime import datetime
from enum import Enum as PyEnum
from sqlalchemy import Column, String, DateTime, Integer, ForeignKey, Text, Boolean
from sqlalchemy.dialects.postgresql import UUID, ENUM
from app.models.base import Base


class NotificationStatus(str, PyEnum):
    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    OPENED = "opened"
    FAILED = "failed"
    EXPIRED = "expired"
    DISMISSED = "dismissed"


class NotificationPriority(str, PyEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class NotificationType(str, PyEnum):
    # User Lifecycle
    WELCOME = "welcome"
    EMAIL_VERIFICATION = "email_verification"
    PASSWORD_RESET = "password_reset"

    # Trial Flow
    TRIAL_STARTED = "trial_started"
    TRIAL_3_DAYS_LEFT = "trial_3_days_left"
    TRIAL_1_DAY_LEFT = "trial_1_day_left"
    TRIAL_EXPIRED = "trial_expired"
    TRIAL_EXPIRED_REMINDER = "trial_expired_reminder"

    # Team Invites
    TEAM_INVITE_RECEIVED = "team_invite_received"
    TEAM_INVITE_ACCEPTED = "team_invite_accepted"
    TEAM_INVITE_REMINDER = "team_invite_reminder"
    TEAM_MEMBER_JOINED = "team_member_joined"

    # Rate Limiting
    PROMPT_DAILY_WARNING = "prompt_daily_warning"
    PROMPT_NEAR_LIMIT = "prompt_near_limit"
    PROMPT_LIMIT_REACHED = "prompt_limit_reached"
    PROMPT_COOLDOWN = "prompt_cooldown"

    # Billing
    PAYMENT_SUCCESS = "payment_success"
    PAYMENT_FAILED = "payment_failed"
    SUBSCRIPTION_RENEWAL = "subscription_renewal"
    SUBSCRIPTION_DOWNGRADE = "subscription_downgrade"
    INVOICE_AVAILABLE = "invoice_available"

    # Security
    NEW_LOGIN_DETECTED = "new_login_detected"
    SUSPICIOUS_LOGIN = "suspicious_login"
    MFA_ENABLED = "mfa_enabled"

    # Workspace Activity
    TASK_ASSIGNED = "task_assigned"
    TASK_OVERDUE = "task_overdue"
    TASK_APPROVED = "task_approved"
    TASK_REJECTED = "task_rejected"
    COMMENT_MENTION = "comment_mention"
    CALENDAR_REMINDER = "calendar_reminder"
    COMPLIANCE_REMINDER = "compliance_reminder"

    # AI Intelligence
    KNOWLEDGE_GAP_DETECTED = "knowledge_gap_detected"
    AI_CONFIDENCE_LOW = "ai_confidence_low"
    MISSING_DOCUMENTATION = "missing_documentation"
    NEW_REGULATORY_UPDATE = "new_regulatory_update"
    SOURCE_VERIFICATION_DONE = "source_verification_done"

    # System
    MAINTENANCE_SCHEDULED = "maintenance_scheduled"
    DOWNTIME_ALERT = "downtime_alert"
    NEW_FEATURE_RELEASED = "new_feature_released"
    AI_MODEL_UPDATE = "ai_model_update"

    # Upgrade
    UPGRADE_SUGGESTED = "upgrade_suggested"


class NotificationChannel(str, PyEnum):
    EMAIL = "email"
    IN_APP = "in_app"
    PUSH = "push"
    SMS = "sms"


class Notification(Base):
    """Core notification entity."""
    __tablename__ = "notifications"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=True)

    type = Column(ENUM(NotificationType), nullable=False, index=True)
    status = Column(ENUM(NotificationStatus), default=NotificationStatus.QUEUED, nullable=False)
    priority = Column(ENUM(NotificationPriority), default=NotificationPriority.MEDIUM)

    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)
    cta_link = Column(String(512), nullable=True)
    cta_label = Column(String(128), nullable=True)

    # Delivery tracking
    channel = Column(ENUM(NotificationChannel), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    scheduled_for = Column(DateTime, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    opened_at = Column(DateTime, nullable=True)
    dismissed_at = Column(DateTime, nullable=True)

    # Email-specific
    email_subject = Column(String(255), nullable=True)
    email_template_id = Column(String(64), nullable=True)

    # Metadata (JSON blob for extensibility)
    metadata_json = Column(Text, nullable=True)

    # Related entity
    related_entity_type = Column(String(64), nullable=True)  # "task", "invitation", "billing_event"
    related_entity_id = Column(String(128), nullable=True)

    # Tracking
    delivery_attempts = Column(Integer, default=0)
    max_attempts = Column(Integer, default=3)
    error_message = Column(Text, nullable=True)

    def __repr__(self):
        return f"<Notification {self.id} type={self.type} status={self.status}>"


class NotificationPreference(Base):
    """Per-user notification preferences."""
    __tablename__ = "notification_preferences"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, unique=True)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=True)

    # Channel toggles
    email_enabled = Column(Boolean, default=True)
    in_app_enabled = Column(Boolean, default=True)
    push_enabled = Column(Boolean, default=False)

    # Category toggles
    trial_notifications = Column(Boolean, default=True)
    team_notifications = Column(Boolean, default=True)
    billing_notifications = Column(Boolean, default=True)
    security_notifications = Column(Boolean, default=True)
    workspace_notifications = Column(Boolean, default=True)
    ai_notifications = Column(Boolean, default=True)
    system_notifications = Column(Boolean, default=True)

    # Digest settings
    digest_enabled = Column(Boolean, default=False)
    digest_frequency = Column(String(16), default="daily")  # daily, weekly

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
