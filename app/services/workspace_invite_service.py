"""Workspace member invitations — viewers (Pro+) and paid seats (Enterprise)."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.db.models import (
    Organization,
    PlanType,
    Seat,
    SubscriptionStatus,
    User,
    UserRole,
    WorkspaceInvitation,
    WorkspaceInvitationStatus,
)
from app.services.enterprise_roles import (
    can_assign_enterprise_admin,
    is_workspace_admin,
    role_display_payload,
)
from app.services.seat_allocation import PAID_ROLES, billing_snapshot_for_org, is_paid_role
from app.services.workspace_permissions import effective_feature_permissions

INVITE_TTL_DAYS = 7
TEAM_PLANS = {PlanType.PRO, PlanType.ENTERPRISE}


def generate_invite_token() -> str:
    return secrets.token_urlsafe(32)


def invite_accept_url(token: str) -> str:
    base = (settings.frontend_url or "http://localhost:5173").rstrip("/")
    return f"{base}/accept-invite?token={token}"


def role_label(role: UserRole | str) -> str:
    if isinstance(role, UserRole):
        return role.value.replace("_", " ").title()
    return str(role).replace("_", " ").title()


def invite_role_value(role: UserRole | str) -> str:
    return role.value if isinstance(role, UserRole) else str(role).lower()


def invite_status_value(status: WorkspaceInvitationStatus | str) -> str:
    if isinstance(status, WorkspaceInvitationStatus):
        return status.value
    return str(status).lower()


async def get_admin_seat(user: User, db: AsyncSession) -> tuple[Organization, Seat]:
    result = await db.execute(
        select(Seat)
        .where(Seat.user_id == user.id, Seat.is_active == True)
        .options(
            selectinload(Seat.organization).selectinload(Organization.subscription),
        )
        .order_by(Seat.created_at.desc())
    )
    seats = result.scalars().all()
    if not seats:
        raise HTTPException(status_code=403, detail="No active workspace")

    for seat in seats:
        if is_workspace_admin(seat.role):
            org = seat.organization
            sub = org.subscription
            if sub and sub.status in (
                SubscriptionStatus.ACTIVE,
                SubscriptionStatus.TRIALING,
            ):
                if sub.plan_type in TEAM_PLANS:
                    return org, seat

    raise HTTPException(
        status_code=403,
        detail="Only workspace admins on Pro or Enterprise can invite members",
    )


def assert_workspace_invite_allowed(
    org: Organization,
    target_role: UserRole,
    inviter_role: Optional[UserRole] = None,
    inviter_user_id: Optional[UUID] = None,
) -> None:
    sub = org.subscription
    if not sub or sub.status not in (
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.TRIALING,
    ):
        raise HTTPException(status_code=403, detail="Active subscription required")

    plan = sub.plan_type
    if plan not in TEAM_PLANS:
        raise HTTPException(
            status_code=403,
            detail="Team invites require a Pro or Enterprise plan",
        )

    if target_role == UserRole.VIEWER:
        pass  # Pro + Enterprise
    elif target_role in PAID_ROLES:
        if plan != PlanType.ENTERPRISE:
            raise HTTPException(
                status_code=403,
                detail="Paid seat invites require an Enterprise plan",
            )
    else:
        raise HTTPException(status_code=400, detail="Invalid role for invitation")

    if target_role == UserRole.ENTERPRISE_ADMIN:
        if plan != PlanType.ENTERPRISE:
            raise HTTPException(
                status_code=403,
                detail="Enterprise Admin seats require an Enterprise plan",
            )
        if inviter_role is not None and inviter_user_id is not None:
            if not can_assign_enterprise_admin(
                inviter_role, inviter_user_id, org.owner_id
            ):
                raise HTTPException(
                    status_code=403,
                    detail="Only the account owner or an Enterprise Admin can invite Enterprise Admin seats",
                )

    if inviter_role is not None:
        from app.core.permissions import PermissionChecker

        if not PermissionChecker.can_manage_role(inviter_role, target_role):
            raise HTTPException(
                status_code=403,
                detail=f"You cannot assign the {target_role.value} role",
            )


async def get_invitation_by_token(
    token: str,
    db: AsyncSession,
) -> WorkspaceInvitation:
    result = await db.execute(
        select(WorkspaceInvitation)
        .where(WorkspaceInvitation.token == token)
        .options(
            selectinload(WorkspaceInvitation.organization).selectinload(
                Organization.subscription
            ),
            selectinload(WorkspaceInvitation.invited_by),
        )
    )
    inv = result.scalar_one_or_none()
    if not inv:
        raise HTTPException(status_code=404, detail="Invitation not found")
    return inv


def _expire_if_needed(inv: WorkspaceInvitation) -> None:
    if (
        invite_status_value(inv.status) == WorkspaceInvitationStatus.PENDING.value
        and inv.expires_at < datetime.utcnow()
    ):
        inv.status = WorkspaceInvitationStatus.EXPIRED.value


def _org_plan_type(org: Organization) -> PlanType:
    if org.subscription and org.subscription.plan_type:
        return org.subscription.plan_type
    return PlanType.STANDARD


async def list_workspace_members_for_org(org_id: UUID, db: AsyncSession) -> dict:
    """Pending invitations (all roles) + active seats (all roles)."""
    org_result = await db.execute(
        select(Organization)
        .where(Organization.id == org_id)
        .options(selectinload(Organization.subscription))
    )
    org = org_result.scalar_one_or_none()
    plan = _org_plan_type(org) if org else PlanType.STANDARD
    owner_id = org.owner_id if org else None

    inv_result = await db.execute(
        select(WorkspaceInvitation)
        .where(WorkspaceInvitation.organization_id == org_id)
        .order_by(WorkspaceInvitation.created_at.desc())
    )
    invitations = inv_result.scalars().all()

    seat_result = await db.execute(
        select(Seat)
        .where(Seat.organization_id == org_id, Seat.is_active == True)
        .options(selectinload(Seat.user))
        .order_by(Seat.created_at.desc())
    )
    seats = seat_result.scalars().all()

    members = []
    seen_emails: set[str] = set()

    for inv in invitations:
        _expire_if_needed(inv)
        st = invite_status_value(inv.status)
        if st == WorkspaceInvitationStatus.REVOKED.value:
            continue
        email = inv.email.lower()
        if email in seen_emails:
            continue
        if st == WorkspaceInvitationStatus.ACCEPTED.value:
            continue
        seen_emails.add(email)
        inv_role = UserRole(invite_role_value(inv.role))
        members.append(
            {
                "id": str(inv.id),
                "email": inv.email,
                "first_name": inv.first_name,
                "last_name": inv.last_name,
                "status": st,
                "role": invite_role_value(inv.role),
                "invited_at": inv.created_at.isoformat() if inv.created_at else None,
                "expires_at": inv.expires_at.isoformat() if inv.expires_at else None,
                "accepted_at": inv.accepted_at.isoformat() if inv.accepted_at else None,
                "invitation_id": str(inv.id),
                "seat_id": None,
                "revocable": st == WorkspaceInvitationStatus.PENDING.value,
                "member_type": "invitation",
                "feature_permissions": inv.feature_permissions,
                "effective_permissions": effective_feature_permissions(
                    plan, inv_role, inv.feature_permissions
                ),
                "seat_allocated": is_paid_role(inv_role),
                "role_display": role_display_payload(inv_role),
            }
        )

    for seat in seats:
        email = seat.user.email.lower()
        if email in seen_emails:
            continue
        seen_emails.add(email)
        name = f"{seat.user.first_name} {seat.user.last_name}".strip() or email
        members.append(
            {
                "id": str(seat.id),
                "email": seat.user.email,
                "first_name": seat.user.first_name,
                "last_name": seat.user.last_name,
                "name": name,
                "status": "active",
                "role": seat.role.value,
                "invited_at": seat.created_at.isoformat() if seat.created_at else None,
                "expires_at": None,
                "accepted_at": seat.created_at.isoformat() if seat.created_at else None,
                "invitation_id": None,
                "seat_id": str(seat.id),
                "revocable": seat.role == UserRole.VIEWER,
                "member_type": "seat",
                "user_id": str(seat.user_id),
                "feature_permissions": seat.feature_permissions,
                "effective_permissions": effective_feature_permissions(
                    plan, seat.role, seat.feature_permissions
                ),
                "seat_allocated": is_paid_role(seat.role),
                "role_display": role_display_payload(
                    seat.role, user_id=seat.user_id, owner_id=owner_id
                ),
            }
        )

    viewers = [m for m in members if m["role"] == "viewer"]
    paid = [m for m in members if m["role"] != "viewer"]
    counts = {
        "total": len(members),
        "active": len([m for m in members if m["status"] == "active"]),
        "pending": len([m for m in members if m["status"] == "pending"]),
        "viewers": len(viewers),
        "paid": len(paid),
    }
    billing = await billing_snapshot_for_org(org, db) if org else {}
    return {"members": members, "viewers": viewers, "counts": counts, "billing": billing}


# Backward-compatible alias for viewer-only list endpoint
async def list_viewers_for_org(org_id: UUID, db: AsyncSession) -> dict:
    payload = await list_workspace_members_for_org(org_id, db)
    viewers = [m for m in payload["members"] if m["role"] == "viewer"]
    counts = {
        "total": len(viewers),
        "active": len([v for v in viewers if v["status"] == "active"]),
        "pending": len([v for v in viewers if v["status"] == "pending"]),
        "revoked": 0,
        "expired": len([v for v in viewers if v["status"] == "expired"]),
    }
    return {"viewers": viewers, "counts": counts}


async def resolve_primary_seat(
    user_id: UUID,
    db: AsyncSession,
    prefer_organization_id: Optional[str] = None,
) -> Optional[Seat]:
    result = await db.execute(
        select(Seat)
        .where(Seat.user_id == user_id, Seat.is_active == True)
        .options(selectinload(Seat.organization).selectinload(Organization.subscription))
        .order_by(Seat.created_at.desc())
    )
    seats = result.scalars().all()
    if not seats:
        return None
    if prefer_organization_id:
        for seat in seats:
            if str(seat.organization_id) == prefer_organization_id:
                return seat
    return seats[0]
