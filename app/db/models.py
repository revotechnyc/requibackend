"""
Requi Health Database Models
Multi-tenant SaaS with Stripe billing and knowledge management
"""

import uuid
from datetime import date, datetime
from enum import Enum as PyEnum
from typing import List, Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.db.user_role_type import UserRoleType


class Base(DeclarativeBase):
    """Base class for all models"""
    pass


# Association tables
organization_users = Table(
    "organization_users",
    Base.metadata,
    Column("organization_id", UUID(as_uuid=True), ForeignKey("organizations.id"), primary_key=True),
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True),
    Column("role", String(50), nullable=False, default="viewer"),
    Column("created_at", DateTime, default=datetime.utcnow),
)


class UserRole(str, PyEnum):
    """User roles within organization (enterprise team + legacy hierarchy)"""
    ENTERPRISE_ADMIN = "enterprise_admin"
    ADMIN = "admin"
    REVIEWER = "reviewer"
    APPROVER = "approver"
    CONTRIBUTOR = "contributor"
    ANALYST = "analyst"
    PRESIDENT = "president"
    VICE_PRESIDENT = "vice_president"
    DIRECTOR = "director"
    MANAGER = "manager"
    TEAM_LEAD = "team_lead"
    SPECIALIST = "specialist"
    ASSISTANT = "assistant"
    VIEWER = "viewer"
    SEO = "seo"


class SubscriptionStatus(str, PyEnum):
    """Stripe subscription statuses"""
    ACTIVE = "active"
    CANCELED = "canceled"
    INCOMPLETE = "incomplete"
    INCOMPLETE_EXPIRED = "incomplete_expired"
    PAST_DUE = "past_due"
    PAUSED = "paused"
    TRIALING = "trialing"
    UNPAID = "unpaid"


class PlanType(str, PyEnum):
    """Pricing plan types"""
    STANDARD = "standard"
    PRO = "pro"
    ENTERPRISE = "enterprise"


class KnowledgeStatus(str, PyEnum):
    """Knowledge record status"""
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    STALE = "stale"
    ARCHIVED = "archived"


class GapTaskStatus(str, PyEnum):
    """Gap detection task status"""
    DETECTED = "detected"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    REJECTED = "rejected"


class SourceType(str, PyEnum):
    """Knowledge source types"""
    REGULATION = "regulation"
    GUIDANCE = "guidance"
    POLICY = "policy"
    ARTICLE = "article"
    INTERNAL = "internal"


# ============================================
# USER & ORGANIZATION MODELS
# ============================================

class User(Base):
    """User accounts"""
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False)
    
    # Stripe
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, unique=True)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    
    # Relationships
    owned_organizations: Mapped[List["Organization"]] = relationship(
        "Organization", back_populates="owner", foreign_keys="Organization.owner_id"
    )
    memberships: Mapped[List["Organization"]] = relationship(
        "Organization", secondary=organization_users, back_populates="members"
    )
    seats: Mapped[List["Seat"]] = relationship("Seat", back_populates="user")
    audit_logs: Mapped[List["AuditLog"]] = relationship("AuditLog", back_populates="user")
    blog_posts: Mapped[List["BlogPost"]] = relationship("BlogPost", back_populates="author")
    
    def __repr__(self) -> str:
        return f"<User {self.email}>"


class PlatformAdminRole(str, PyEnum):
    """Roles for the standalone SaaS admin portal (separate from org UserRole)."""
    SUPER_ADMIN = "super_admin"
    BLOG_WRITER = "blog_writer"
    BLOG_EDITOR = "blog_editor"
    BLOG_ADMIN = "blog_admin"


class PlatformAdmin(Base):
    """Platform operators — SaaS admin portal only (not customer app users)."""
    __tablename__ = "platform_admins"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    last_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    role: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=PlatformAdminRole.SUPER_ADMIN.value,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    invited_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("platform_admins.id"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    invited_by: Mapped[Optional["PlatformAdmin"]] = relationship(
        "PlatformAdmin",
        remote_side="PlatformAdmin.id",
        foreign_keys=[invited_by_id],
    )

    def __repr__(self) -> str:
        return f"<PlatformAdmin {self.email} {self.role}>"


class PlatformBlogStatus(str, PyEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    SCHEDULED = "scheduled"
    ARCHIVED = "archived"


class PlatformBlogCategory(str, PyEnum):
    BLOG = "blog"
    GUIDES = "guides"
    RESOURCES = "resources"


class PlatformBlogPost(Base):
    """Marketing/blog content managed from the SaaS admin portal (platform-wide)."""
    __tablename__ = "platform_blog_posts"

    __table_args__ = (
        Index("idx_platform_blog_status", "status"),
        Index("idx_platform_blog_category", "category"),
        Index("idx_platform_blog_slug", "slug", unique=True),
        Index("idx_platform_blog_author", "author_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    author_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_admins.id"), nullable=False)

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    cover_image_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    category: Mapped[PlatformBlogCategory] = mapped_column(Enum(PlatformBlogCategory), nullable=False, default=PlatformBlogCategory.BLOG)
    status: Mapped[PlatformBlogStatus] = mapped_column(Enum(PlatformBlogStatus), nullable=False, default=PlatformBlogStatus.DRAFT)

    tags: Mapped[list] = mapped_column(JSON, default=list)
    meta_title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    meta_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    scheduled_for: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    view_count: Mapped[int] = mapped_column(Integer, default=0)
    read_time_minutes: Mapped[int] = mapped_column(Integer, default=5)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    last_edited_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_admins.id"), nullable=True)
    published_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_admins.id"), nullable=True)

    author: Mapped["PlatformAdmin"] = relationship("PlatformAdmin", foreign_keys=[author_id])
    last_edited_by: Mapped[Optional["PlatformAdmin"]] = relationship("PlatformAdmin", foreign_keys=[last_edited_by_id])
    published_by: Mapped[Optional["PlatformAdmin"]] = relationship("PlatformAdmin", foreign_keys=[published_by_id])


class Organization(Base):
    """Multi-tenant organizations"""
    __tablename__ = "organizations"
    
    __table_args__ = (
        Index('idx_org_slug', 'slug', unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    # Owner
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    
    # Settings
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    owner: Mapped["User"] = relationship("User", back_populates="owned_organizations", foreign_keys=[owner_id])
    members: Mapped[List["User"]] = relationship("User", secondary=organization_users, back_populates="memberships")
    subscription: Mapped[Optional["Subscription"]] = relationship("Subscription", back_populates="organization", uselist=False)
    seats: Mapped[List["Seat"]] = relationship("Seat", back_populates="organization")
    sources: Mapped[List["Source"]] = relationship("Source", back_populates="organization")
    documents: Mapped[List["Document"]] = relationship("Document", back_populates="organization")
    knowledge_records: Mapped[List["KnowledgeRecord"]] = relationship("KnowledgeRecord", back_populates="organization")
    gap_tasks: Mapped[List["GapTask"]] = relationship("GapTask", back_populates="organization")
    audit_logs: Mapped[List["AuditLog"]] = relationship("AuditLog", back_populates="organization")
    blog_posts: Mapped[List["BlogPost"]] = relationship("BlogPost", back_populates="organization")
    
    def __repr__(self) -> str:
        return f"<Organization {self.name}>"


# ============================================
# BILLING MODELS
# ============================================

class Subscription(Base):
    """Stripe subscriptions"""
    __tablename__ = "subscriptions"
    
    __table_args__ = (
        Index('idx_sub_stripe_id', 'stripe_subscription_id', unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Organization
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), unique=True, nullable=False)
    
    # Plan
    plan_type: Mapped[PlanType] = mapped_column(Enum(PlanType), nullable=False)
    
    # Stripe
    stripe_subscription_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    stripe_price_id: Mapped[str] = mapped_column(String(100), nullable=False)
    stripe_customer_id: Mapped[str] = mapped_column(String(100), nullable=False)
    
    # Status
    status: Mapped[SubscriptionStatus] = mapped_column(Enum(SubscriptionStatus), nullable=False)
    
    # Seats
    seat_quantity: Mapped[int] = mapped_column(Integer, default=1)
    
    # Billing cycle
    current_period_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    current_period_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    
    # Trial
    trial_start: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    trial_end: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    organization: Mapped["Organization"] = relationship("Organization", back_populates="subscription")
    
    def is_active(self) -> bool:
        return self.status in [SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING]
    
    def __repr__(self) -> str:
        return f"<Subscription {self.stripe_subscription_id} {self.status}>"


class Seat(Base):
    """Seat assignments for billing"""
    __tablename__ = "seats"
    
    __table_args__ = (
        UniqueConstraint('organization_id', 'user_id', name='uq_org_user_seat'),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    
    # Role (VARCHAR lowercase — reviewer, admin, viewer, …)
    role: Mapped[UserRole] = mapped_column(
        UserRoleType(),
        nullable=False,
        default=UserRole.VIEWER,
    )
    
    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Optional per-member tab overrides (null = use role defaults)
    feature_permissions: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    organization: Mapped["Organization"] = relationship("Organization", back_populates="seats")
    user: Mapped["User"] = relationship("User", back_populates="seats")
    
    def __repr__(self) -> str:
        return f"<Seat {self.organization_id}:{self.user_id}>"


class WorkspaceInvitationStatus(str, PyEnum):
    """Workspace member invitation lifecycle."""
    PENDING = "pending"
    ACCEPTED = "accepted"
    REVOKED = "revoked"
    EXPIRED = "expired"


class WorkspaceInvitation(Base):
    """Email invitations to join an organization (viewers on Pro/Enterprise)."""
    __tablename__ = "workspace_invitations"

    __table_args__ = (
        Index("idx_workspace_invite_org_email", "organization_id", "email"),
        Index("idx_workspace_invite_token", "token", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    invited_by_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )

    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # VARCHAR (not PG enum) — values are lowercase: viewer, reviewer, admin, …
    role: Mapped[str] = mapped_column(String(50), nullable=False, default=UserRole.VIEWER.value)
    token: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)

    status: Mapped[str] = mapped_column(
        String(32),
        default=WorkspaceInvitationStatus.PENDING.value,
        nullable=False,
    )

    first_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    feature_permissions: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    organization: Mapped["Organization"] = relationship("Organization")
    invited_by: Mapped["User"] = relationship("User", foreign_keys=[invited_by_id])


class WorkspaceTaskStatus(str, PyEnum):
    """Compliance task workflow statuses."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUBMITTED_FOR_REVIEW = "submitted_for_review"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    REJECTED = "rejected"
    COMPLETED = "completed"


class WorkspaceTask(Base):
    """Organization compliance tasks (Pro single-owner, Enterprise workflow)."""
    __tablename__ = "workspace_tasks"

    __table_args__ = (
        Index("idx_workspace_tasks_org_status", "organization_id", "status"),
        Index("idx_workspace_tasks_org_created", "organization_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    creator_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    assignee_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    reviewer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    approver_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    document_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id"), nullable=True
    )
    document_ids: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    workflow_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace_workflows.id"), nullable=True
    )

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=WorkspaceTaskStatus.PENDING.value
    )
    priority: Mapped[str] = mapped_column(String(20), nullable=False, default="medium")
    category: Mapped[str] = mapped_column(String(100), nullable=False, default="General")
    due_date: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    tags: Mapped[Optional[list]] = mapped_column(JSON, default=list)

    comments: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    history: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    resolution_result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    resolution_history: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    approval_ai_reviews: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    resolution_document_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id"), nullable=True
    )
    execution_conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    organization: Mapped["Organization"] = relationship("Organization")
    creator: Mapped["User"] = relationship("User", foreign_keys=[creator_id])
    assignee: Mapped[Optional["User"]] = relationship("User", foreign_keys=[assignee_id])
    reviewer: Mapped[Optional["User"]] = relationship("User", foreign_keys=[reviewer_id])
    approver: Mapped[Optional["User"]] = relationship("User", foreign_keys=[approver_id])
    document: Mapped[Optional["Document"]] = relationship("Document", foreign_keys=[document_id])
    resolution_document: Mapped[Optional["Document"]] = relationship(
        "Document", foreign_keys=[resolution_document_id]
    )
    workflow: Mapped[Optional["WorkspaceWorkflow"]] = relationship(
        "WorkspaceWorkflow", back_populates="tasks"
    )


class WorkspaceWorkflowStatus(str, PyEnum):
    OPEN = "open"
    IN_REVIEW = "in_review"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class WorkspaceWorkflow(Base):
    """Case/ticket container linking tasks, documents, and activity."""
    __tablename__ = "workspace_workflows"

    __table_args__ = (
        Index("idx_workspace_workflows_org_status", "organization_id", "status"),
        Index("idx_workspace_workflows_org_created", "organization_id", "created_at"),
        Index(
            "idx_workspace_workflows_org_reference",
            "organization_id",
            "reference_code",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    creator_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    reference_code: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=WorkspaceWorkflowStatus.OPEN.value
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    external_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    priority: Mapped[str] = mapped_column(String(20), nullable=False, default="medium")
    due_date: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    category: Mapped[str] = mapped_column(String(100), nullable=False, default="General")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    creator: Mapped["User"] = relationship("User", foreign_keys=[creator_id])
    owner: Mapped["User"] = relationship("User", foreign_keys=[owner_id])
    tasks: Mapped[List["WorkspaceTask"]] = relationship(
        "WorkspaceTask", back_populates="workflow"
    )


class WorkflowActivity(Base):
    """Audit feed for workspace workflows."""
    __tablename__ = "workflow_activities"

    __table_args__ = (
        Index("idx_workflow_activities_workflow", "workflow_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace_workflows.id"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    actor: Mapped["User"] = relationship("User")


class WorkflowFinding(Base):
    """AI or manual findings saved for a workflow case."""
    __tablename__ = "workflow_findings"

    __table_args__ = (
        Index("idx_workflow_findings_workflow", "workflow_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace_workflows.id"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace_tasks.id"), nullable=True
    )
    created_by_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    findings: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    risk_level: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    recommendations: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    evidence_refs: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    raw_payload: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    workflow: Mapped["WorkspaceWorkflow"] = relationship("WorkspaceWorkflow")
    created_by: Mapped["User"] = relationship("User")


# ============================================
# COMPLIANCE DASHBOARD MODELS
# ============================================

class ComplianceFramework(Base):
    """Active regulatory framework tracked per organization (Pro: max 3, Enterprise: unlimited)."""
    __tablename__ = "compliance_frameworks"

    __table_args__ = (
        Index("idx_compliance_frameworks_org_slug", "organization_id", "slug", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    score: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    organization: Mapped["Organization"] = relationship("Organization")


class ComplianceGap(Base):
    """Open compliance gaps linked to a framework."""
    __tablename__ = "compliance_gaps"

    __table_args__ = (
        Index("idx_compliance_gaps_org_status", "organization_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    framework_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="medium")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    category: Mapped[str] = mapped_column(String(100), nullable=False, default="General")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    organization: Mapped["Organization"] = relationship("Organization")


class ComplianceScoreSnapshot(Base):
    """Persisted AI / aggregated compliance scores (Intelligence → Dashboard contract)."""
    __tablename__ = "compliance_score_snapshots"

    __table_args__ = (
        Index("idx_compliance_scores_org_calc", "organization_id", "calculated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    framework_scores: Mapped[dict] = mapped_column(JSON, default=dict)
    overall_score: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    risk_level: Mapped[str] = mapped_column(String(20), default="medium")
    gaps_found: Mapped[list] = mapped_column(JSON, default=list)
    recommendations: Mapped[list] = mapped_column(JSON, default=list)
    source_type: Mapped[str] = mapped_column(String(40), default="aggregation")
    calculated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    organization: Mapped["Organization"] = relationship("Organization")


# ============================================
# KNOWLEDGE SOURCE MODELS
# ============================================

class Source(Base):
    """Approved knowledge sources"""
    __tablename__ = "sources"
    
    __table_args__ = (
        Index('idx_source_org_url', 'organization_id', 'url', unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    
    # Source info
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    source_type: Mapped[SourceType] = mapped_column(Enum(SourceType), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    # Authority
    authority_score: Mapped[float] = mapped_column(Numeric(3, 2), default=1.0)  # 0.0 - 1.0
    is_official: Mapped[bool] = mapped_column(Boolean, default=False)
    
    # Ingestion settings
    ingest_frequency: Mapped[str] = mapped_column(String(50), default="daily")  # daily, weekly, manual
    last_ingested_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    
    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    organization: Mapped["Organization"] = relationship("Organization", back_populates="sources")
    documents: Mapped[List["Document"]] = relationship("Document", back_populates="source")
    
    def __repr__(self) -> str:
        return f"<Source {self.name}>"


class Document(Base):
    """Ingested documents"""
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    source_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("sources.id"), nullable=True)
    workflow_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace_workflows.id"), nullable=True
    )

    # Document info
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # SHA-256
    
    # Content (stored or referenced)
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    storage_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    
    # Metadata
    document_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    
    # Versioning
    version: Mapped[int] = mapped_column(Integer, default=1)
    
    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    organization: Mapped["Organization"] = relationship("Organization", back_populates="documents")
    source: Mapped[Optional["Source"]] = relationship("Source", back_populates="documents")
    chunks: Mapped[List["DocumentChunk"]] = relationship("DocumentChunk", back_populates="document")
    
    def __repr__(self) -> str:
        return f"<Document {self.title}>"


class DocumentChunk(Base):
    """Document chunks with embeddings"""
    __tablename__ = "document_chunks"
    
    __table_args__ = (
        Index('idx_chunk_doc', 'document_id'),
        Index('idx_chunk_embedding', 'embedding', postgresql_using='ivfflat'),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False)
    
    # Chunk content
    content: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    
    # Embedding (pgvector)
    embedding: Mapped[Optional[List[float]]] = mapped_column(Vector(1536), nullable=True)
    
    # Metadata
    chunk_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    # Relationships
    document: Mapped["Document"] = relationship("Document", back_populates="chunks")
    
    def __repr__(self) -> str:
        return f"<DocumentChunk {self.document_id}:{self.chunk_index}>"


# ============================================
# KNOWLEDGE RECORD MODELS
# ============================================

class KnowledgeRecord(Base):
    """Canonical knowledge records"""
    __tablename__ = "knowledge_records"
    
    __table_args__ = (
        Index('idx_knowledge_org_status', 'organization_id', 'status'),
        Index('idx_knowledge_topic', 'topic'),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    
    # Knowledge content
    topic: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    
    # Quality scores
    confidence_score: Mapped[float] = mapped_column(Numeric(3, 2), default=0.0)
    relevance_score: Mapped[float] = mapped_column(Numeric(3, 2), default=0.0)
    recency_score: Mapped[float] = mapped_column(Numeric(3, 2), default=0.0)
    trust_score: Mapped[float] = mapped_column(Numeric(3, 2), default=0.0)
    
    # Status
    status: Mapped[KnowledgeStatus] = mapped_column(Enum(KnowledgeStatus), default=KnowledgeStatus.DRAFT)
    
    # Versioning
    version: Mapped[int] = mapped_column(Integer, default=1)
    previous_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("knowledge_records.id"), nullable=True)
    
    # Review
    reviewed_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    review_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    # Staleness tracking
    last_validated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    organization: Mapped["Organization"] = relationship("Organization", back_populates="knowledge_records")
    citations: Mapped[List["Citation"]] = relationship("Citation", back_populates="knowledge_record")
    
    def __repr__(self) -> str:
        return f"<KnowledgeRecord {self.topic}>"


class Citation(Base):
    """Citations linking knowledge to source chunks"""
    __tablename__ = "citations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    knowledge_record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("knowledge_records.id"), nullable=False)
    document_chunk_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("document_chunks.id"), nullable=False)
    
    # Citation info
    relevance_score: Mapped[float] = mapped_column(Numeric(3, 2), default=0.0)
    excerpt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    # Relationships
    knowledge_record: Mapped["KnowledgeRecord"] = relationship("KnowledgeRecord", back_populates="citations")
    document_chunk: Mapped["DocumentChunk"] = relationship("DocumentChunk")
    
    def __repr__(self) -> str:
        return f"<Citation {self.knowledge_record_id}:{self.document_chunk_id}>"


# ============================================
# GAP DETECTION MODELS
# ============================================

class GapTask(Base):
    """Knowledge gap detection and resolution tasks"""
    __tablename__ = "gap_tasks"
    
    __table_args__ = (
        Index('idx_gap_org_status', 'organization_id', 'status'),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    
    # Original query that triggered gap
    original_query: Mapped[str] = mapped_column(Text, nullable=False)
    
    # Gap analysis
    gap_description: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_score: Mapped[float] = mapped_column(Numeric(3, 2), default=0.0)
    
    # Status
    status: Mapped[GapTaskStatus] = mapped_column(Enum(GapTaskStatus), default=GapTaskStatus.DETECTED)
    
    # Resolution
    proposed_knowledge_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("knowledge_records.id"), nullable=True)
    resolved_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    resolution_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    organization: Mapped["Organization"] = relationship("Organization", back_populates="gap_tasks")
    proposed_knowledge: Mapped[Optional["KnowledgeRecord"]] = relationship("KnowledgeRecord")
    
    def __repr__(self) -> str:
        return f"<GapTask {self.status}>"


# ============================================
# AUDIT LOG MODELS
# ============================================

class AuditLog(Base):
    """Compliance audit logs"""
    __tablename__ = "audit_logs"
    
    __table_args__ = (
        Index('idx_audit_org_time', 'organization_id', 'created_at'),
        Index('idx_audit_action', 'action'),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    
    # Action details
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)  # knowledge, source, user, etc.
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    
    # Data
    previous_state: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    new_state: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    
    # Context
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    # Relationships
    organization: Mapped["Organization"] = relationship("Organization", back_populates="audit_logs")
    user: Mapped[Optional["User"]] = relationship("User", back_populates="audit_logs")
    
    def __repr__(self) -> str:
        return f"<AuditLog {self.action}:{self.resource_type}>"


# ============================================
# USAGE TRACKING (trial daily AI prompt limits)
# ============================================

class UsageRecord(Base):
    """Daily AI prompt usage per user (trial rate limiting)."""
    __tablename__ = "usage_records"

    __table_args__ = (
        Index("idx_usage_user_date", "user_id", "date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    prompt_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user: Mapped["User"] = relationship("User")

    def __repr__(self) -> str:
        return f"<UsageRecord {self.user_id} {self.date.date()} prompts={self.prompt_count}>"


# ============================================
# CONVERSATION MODELS (for AI Q&A)
# ============================================

class Conversation(Base):
    """User conversations with AI"""
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    
    # Conversation info
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    workflow_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace_workflows.id"), nullable=True
    )
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace_tasks.id"), nullable=True
    )

    # Imported from a public share link (User 2 continuing a shared chat)
    is_shared_import: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    shared_from_token: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    messages: Mapped[List["Message"]] = relationship("Message", back_populates="conversation", order_by="Message.created_at")


class Message(Base):
    """Individual messages in conversations"""
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False)
    
    # Message content
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user, assistant, system
    content: Mapped[str] = mapped_column(Text, nullable=False)
    
    # AI response metadata
    citations: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    confidence_score: Mapped[Optional[float]] = mapped_column(Numeric(3, 2), nullable=True)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    # Relationships
    conversation: Mapped["Conversation"] = relationship("Conversation", back_populates="messages")


class ConversationShare(Base):
    """Public read-only snapshot of a conversation for sharing via link."""
    __tablename__ = "conversation_shares"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    share_token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False,
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False,
    )

    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    snapshot_messages: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ============================================
# BLOG MODELS
# ============================================

class BlogPostStatus(str, PyEnum):
    """Blog post publication status"""
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class BlogPost(Base):
    """Blog posts for SEO team and content management"""
    __tablename__ = "blog_posts"
    
    __table_args__ = (
        Index('idx_blog_org_status', 'organization_id', 'status'),
        Index('idx_blog_slug', 'slug', unique=True),
        Index('idx_blog_author', 'author_id'),
    )
    
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    author_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    
    # Content
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    
    # SEO
    meta_title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    meta_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    
    # Status
    status: Mapped[BlogPostStatus] = mapped_column(Enum(BlogPostStatus), default=BlogPostStatus.DRAFT)
    
    # Metrics
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    read_time_minutes: Mapped[int] = mapped_column(Integer, default=5)
    
    # Publishing
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    organization: Mapped["Organization"] = relationship("Organization", back_populates="blog_posts")
    author: Mapped["User"] = relationship("User", back_populates="blog_posts")
    
    def __repr__(self) -> str:
        return f"<BlogPost {self.slug} {self.status}>"


# ==========================
# IN-APP NOTIFICATIONS
# ==========================


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
    WELCOME = "welcome"
    TRIAL_STARTED = "trial_started"
    TRIAL_3_DAYS_LEFT = "trial_3_days_left"
    TRIAL_1_DAY_LEFT = "trial_1_day_left"
    TRIAL_EXPIRED = "trial_expired"
    PROMPT_NEAR_LIMIT = "prompt_near_limit"
    PROMPT_LIMIT_REACHED = "prompt_limit_reached"
    LIVE_VOICE_CONNECTED = "live_voice_connected"
    LIVE_VOICE_ENDED = "live_voice_ended"
    LIVE_VOICE_TURN_SAVED = "live_voice_turn_saved"
    AI_RESPONSE_READY = "ai_response_ready"
    CHAT_SHARED_IMPORTED = "chat_shared_imported"
    TASK_DUE_SOON = "task_due_soon"
    TASK_DUE_TODAY = "task_due_today"
    TASK_OVERDUE = "task_overdue"


class NotificationChannel(str, PyEnum):
    EMAIL = "email"
    IN_APP = "in_app"
    PUSH = "push"
    SMS = "sms"


# Types surfaced in the product (working modules only — Intelligence + trial usage).
WORKING_IN_APP_NOTIFICATION_TYPES: frozenset[NotificationType] = frozenset({
    NotificationType.WELCOME,
    NotificationType.TRIAL_STARTED,
    NotificationType.TRIAL_3_DAYS_LEFT,
    NotificationType.TRIAL_1_DAY_LEFT,
    NotificationType.TRIAL_EXPIRED,
    NotificationType.PROMPT_NEAR_LIMIT,
    NotificationType.PROMPT_LIMIT_REACHED,
    NotificationType.LIVE_VOICE_CONNECTED,
    NotificationType.LIVE_VOICE_ENDED,
    NotificationType.LIVE_VOICE_TURN_SAVED,
    NotificationType.AI_RESPONSE_READY,
    NotificationType.CHAT_SHARED_IMPORTED,
    NotificationType.TASK_DUE_SOON,
    NotificationType.TASK_DUE_TODAY,
    NotificationType.TASK_OVERDUE,
})


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=True
    )

    type: Mapped[NotificationType] = mapped_column(Enum(NotificationType), nullable=False, index=True)
    status: Mapped[NotificationStatus] = mapped_column(
        Enum(NotificationStatus), default=NotificationStatus.QUEUED, nullable=False
    )
    priority: Mapped[NotificationPriority] = mapped_column(
        Enum(NotificationPriority), default=NotificationPriority.MEDIUM
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    cta_link: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    cta_label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    channel: Mapped[NotificationChannel] = mapped_column(Enum(NotificationChannel), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    scheduled_for: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    opened_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    dismissed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    email_subject: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    email_template_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    related_entity_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    related_entity_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class NotificationPreference(Base):
    __tablename__ = "notification_preferences"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, unique=True)
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=True
    )

    email_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    in_app_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    push_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    trial_notifications: Mapped[bool] = mapped_column(Boolean, default=True)
    team_notifications: Mapped[bool] = mapped_column(Boolean, default=True)
    billing_notifications: Mapped[bool] = mapped_column(Boolean, default=True)
    security_notifications: Mapped[bool] = mapped_column(Boolean, default=True)
    workspace_notifications: Mapped[bool] = mapped_column(Boolean, default=True)
    ai_notifications: Mapped[bool] = mapped_column(Boolean, default=True)
    system_notifications: Mapped[bool] = mapped_column(Boolean, default=True)

    digest_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    digest_frequency: Mapped[str] = mapped_column(String(16), default="daily")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ==========================
# CLM (Contract Lifecycle Management) — Enterprise
# ==========================


class ClmContractStatus(str, PyEnum):
    PROCESSING = "processing"
    ACTIVE = "active"
    EXPIRING = "expiring"
    EXPIRED = "expired"
    ARCHIVED = "archived"


class ClmSubLocation(Base):
    """User-managed facility / sub-location hierarchy for contract storage."""
    __tablename__ = "clm_sub_locations"

    __table_args__ = (
        Index("idx_clm_sub_locations_org", "organization_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    parent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clm_sub_locations.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    organization: Mapped["Organization"] = relationship("Organization")
    parent: Mapped[Optional["ClmSubLocation"]] = relationship(
        "ClmSubLocation", remote_side="ClmSubLocation.id"
    )


class ClmVendor(Base):
    """Vendor registry — manual or auto-created from contract upload."""
    __tablename__ = "clm_vendors"

    __table_args__ = (
        Index("idx_clm_vendors_org_active", "organization_id", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    sub_location_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clm_sub_locations.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    contact_phone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    organization: Mapped["Organization"] = relationship("Organization")
    sub_location: Mapped[Optional["ClmSubLocation"]] = relationship("ClmSubLocation")


class ClmContract(Base):
    """CLM contract record — file lives in Documents; this is structured metadata."""
    __tablename__ = "clm_contracts"

    __table_args__ = (
        Index("idx_clm_contracts_org_status", "organization_id", "status"),
        Index("idx_clm_contracts_org_exp", "organization_id", "expiration_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False
    )
    vendor_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clm_vendors.id"), nullable=True
    )
    sub_location_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clm_sub_locations.id"), nullable=True
    )
    owner_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    created_by_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    contract_number: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ClmContractStatus.PROCESSING.value
    )
    effective_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    expiration_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    renewal_clause: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    contract_value: Mapped[Optional[float]] = mapped_column(nullable=True)
    risk_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ai_extraction: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    renewal_task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace_tasks.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    organization: Mapped["Organization"] = relationship("Organization")
    document: Mapped["Document"] = relationship("Document")
    vendor: Mapped[Optional["ClmVendor"]] = relationship("ClmVendor")
    sub_location: Mapped[Optional["ClmSubLocation"]] = relationship("ClmSubLocation")


class ClmObligation(Base):
    """Contractual obligation extracted by AI — linked to compliance gaps when created."""
    __tablename__ = "clm_obligations"

    __table_args__ = (
        Index("idx_clm_obligations_contract", "contract_id"),
        Index("idx_clm_obligations_org_status", "organization_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clm_contracts.id"), nullable=False
    )
    compliance_gap_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("compliance_gaps.id"), nullable=True
    )
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace_tasks.id"), nullable=True
    )

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    obligation_type: Mapped[str] = mapped_column(String(64), nullable=False, default="other")
    due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="medium")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    contract: Mapped["ClmContract"] = relationship("ClmContract")
