"""
Enhanced plan, role, team, and permission system models
"""

import uuid
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Enum, ForeignKey,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.db.models import Base  # Reuse existing Base

# ==========================
# ENUMS
# ==========================

class PlanType(str, PyEnum):
    STANDARD = "standard"
    PRO = "pro"
    ENTERPRISE = "enterprise"

class UserRole(str, PyEnum):
    ADMIN = "admin"
    PRESIDENT = "president"
    VICE_PRESIDENT = "vice_president"
    DIRECTOR = "director"
    MANAGER = "manager"
    TEAM_LEAD = "team_lead"
    SPECIALIST = "specialist"
    ASSISTANT = "assistant"
    VIEWER = "viewer"

class TeamMemberStatus(str, PyEnum):
    ACTIVE = "active"
    PENDING = "pending"
    INACTIVE = "inactive"

class InvitationStatus(str, PyEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    EXPIRED = "expired"
    REVOKED = "revoked"

# ==========================
# ROLE HIERARCHY (numeric for comparison)
# ==========================

ROLE_HIERARCHY = {
    UserRole.ADMIN: 100,
    UserRole.PRESIDENT: 90,
    UserRole.VICE_PRESIDENT: 80,
    UserRole.DIRECTOR: 70,
    UserRole.MANAGER: 60,
    UserRole.TEAM_LEAD: 50,
    UserRole.SPECIALIST: 40,
    UserRole.ASSISTANT: 30,
    UserRole.VIEWER: 20,
}

# ==========================
# PLAN FEATURE ACCESS
# ==========================

PLAN_FEATURES = {
    PlanType.STANDARD: [
        "intelligence",
        "documents",
        "settings",
    ],
    PlanType.PRO: [
        "intelligence",
        "dashboard",
        "tasks",
        "compliance",
        "calendar",
        "documents",
        "news",
        "teams",
        "settings",
    ],
    PlanType.ENTERPRISE: [
        "intelligence",
        "dashboard",
        "tasks",
        "compliance",
        "calendar",
        "documents",
        "news",
        "teams",
        "integrations",
        "settings",
        "admin",
        "audit_logs",
        "multiple_admins",
    ],
}

# ==========================
# ROLE PERMISSIONS
# ==========================

ROLE_PERMISSIONS = {
    UserRole.ADMIN: [
        "view", "create", "edit", "delete", "manage",
        "admin", "billing", "upgrade", "invite", "remove_user",
        "change_role", "view_audit",
    ],
    UserRole.PRESIDENT: [
        "view", "create", "edit", "manage", "approve",
        "reports", "view_all_teams", "view_audit",
    ],
    UserRole.VICE_PRESIDENT: [
        "view", "create", "edit", "manage", "reports",
        "view_department", "approve_workflow",
    ],
    UserRole.DIRECTOR: [
        "view", "create", "edit", "manage", "assign",
        "view_team", "review_performance",
    ],
    UserRole.MANAGER: [
        "view", "create", "edit", "assign",
        "view_team_activity", "operational_reports",
    ],
    UserRole.TEAM_LEAD: [
        "view", "create", "edit", "coordinate",
        "view_limited_activity", "submit_recommendations",
    ],
    UserRole.SPECIALIST: [
        "view", "create", "submit",
        "work_modules", "view_assigned",
    ],
    UserRole.ASSISTANT: [
        "view", "create", "assist",
        "enter_data", "view_assigned", "suggest",
    ],
    UserRole.VIEWER: ["view", "read_only"],
}

# ==========================
# NEW / UPDATED MODELS
# ==========================

class Account(Base):
    """Organization account with plan"""
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    
    plan_type: Mapped[PlanType] = mapped_column(Enum(PlanType), default=PlanType.STANDARD)
    billing_status: Mapped[str] = mapped_column(String(50), default="active")
    
    # Stripe
    stripe_customer_id: Mapped[str] = mapped_column(String(100), nullable=True)
    stripe_subscription_id: Mapped[str] = mapped_column(String(100), nullable=True)
    
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    owner: Mapped["User"] = relationship("User", foreign_keys=[owner_id])
    teams: Mapped[list["Team"]] = relationship("Team", back_populates="account")
    audit_logs: Mapped[list["AuditLog"]] = relationship("AuditLog", back_populates="account")

class Team(Base):
    """Team within an account"""
    __tablename__ = "teams"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False)
    
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    account: Mapped["Account"] = relationship("Account", back_populates="teams")
    members: Mapped[list["TeamMember"]] = relationship("TeamMember", back_populates="team")

class TeamMember(Base):
    """Membership of a user in a team with role"""
    __tablename__ = "team_members"
    
    __table_args__ = (
        UniqueConstraint("team_id", "user_id", name="uq_team_user"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("teams.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.VIEWER)
    status: Mapped[TeamMemberStatus] = mapped_column(Enum(TeamMemberStatus), default=TeamMemberStatus.ACTIVE)
    
    invited_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    # Relationships
    team: Mapped["Team"] = relationship("Team", back_populates="members")
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])

class RolePermission(Base):
    """Granular permission mapping per role"""
    __tablename__ = "role_permissions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), nullable=False)
    
    feature_key: Mapped[str] = mapped_column(String(100), nullable=False)
    
    can_view: Mapped[bool] = mapped_column(Boolean, default=False)
    can_create: Mapped[bool] = mapped_column(Boolean, default=False)
    can_edit: Mapped[bool] = mapped_column(Boolean, default=False)
    can_delete: Mapped[bool] = mapped_column(Boolean, default=False)
    can_manage: Mapped[bool] = mapped_column(Boolean, default=False)

class FeatureAccess(Base):
    """Feature availability per plan"""
    __tablename__ = "feature_access"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_type: Mapped[PlanType] = mapped_column(Enum(PlanType), nullable=False)
    feature_key: Mapped[str] = mapped_column(String(100), nullable=False)
    
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    upgrade_required: Mapped[bool] = mapped_column(Boolean, default=False)
    
    __table_args__ = (
        UniqueConstraint("plan_type", "feature_key", name="uq_plan_feature"),
    )

class Invitation(Base):
    """Team member invitations"""
    __tablename__ = "invitations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    team_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("teams.id"), nullable=False)
    invited_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.VIEWER)
    
    token: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    status: Mapped[InvitationStatus] = mapped_column(Enum(InvitationStatus), default=InvitationStatus.PENDING)
    
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    accepted_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)

class AuditLog(Base):
    """Audit trail for team and permission actions"""
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False)
    team_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("teams.id"), nullable=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    
    action: Mapped[str] = mapped_column(String(100), nullable=False)  # e.g. "user_invited", "role_changed", "plan_upgraded"
    target_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=True)
    
    metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    # Relationships
    account: Mapped["Account"] = relationship("Account", back_populates="audit_logs")
