"""Workspace viewer invitations — create, accept, revoke."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

from fastapi import HTTPException, status
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

INVITE_TTL_DAYS = 7
VIEWER_PLANS = {PlanType.PRO, PlanType.ENTERPRISE}


def generate_invite_token() -> str:
    return secrets.token_urlsafe(32)


def invite_accept_url(token: str) -> str:
    base = (settings.frontend_url or "http://localhost:5173").rstrip("/")
    return f"{base}/accept-invite?token={token}"


def _plan_allows_viewers(plan_type: PlanType) -> bool:
    return plan_type in VIEWER_PLANS


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
        if seat.role == UserRole.ADMIN:
            org = seat.organization
            sub = org.subscription
            if sub and sub.status in (
                SubscriptionStatus.ACTIVE,
                SubscriptionStatus.TRIALING,
            ):
                if _plan_allows_viewers(sub.plan_type):
                    return org, seat

    raise HTTPException(
        status_code=403,
        detail="Only workspace admins on Pro or Enterprise can invite viewers",
    )


async def assert_viewer_invite_allowed(org: Organization) -> None:
    sub = org.subscription
    if not sub or sub.status not in (
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.TRIALING,
    ):
        raise HTTPException(status_code=403, detail="Active subscription required")
    if not _plan_allows_viewers(sub.plan_type):
        raise HTTPException(
            status_code=403,
            detail="View-only invites are available on Pro and Enterprise plans only",
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
        inv.status == WorkspaceInvitationStatus.PENDING
        and inv.expires_at < datetime.utcnow()
    ):
        inv.status = WorkspaceInvitationStatus.EXPIRED


async def list_viewers_for_org(org_id: UUID, db: AsyncSession) -> dict:
    inv_result = await db.execute(
        select(WorkspaceInvitation)
        .where(
            WorkspaceInvitation.organization_id == org_id,
            WorkspaceInvitation.role == UserRole.VIEWER,
        )
        .order_by(WorkspaceInvitation.created_at.desc())
    )
    invitations = inv_result.scalars().all()

    seat_result = await db.execute(
        select(Seat)
        .where(
            Seat.organization_id == org_id,
            Seat.role == UserRole.VIEWER,
            Seat.is_active == True,
        )
        .options(selectinload(Seat.user))
        .order_by(Seat.created_at.desc())
    )
    seats = seat_result.scalars().all()

    viewers = []
    seen_emails = set()

    for inv in invitations:
        _expire_if_needed(inv)
        if inv.status == WorkspaceInvitationStatus.REVOKED:
            continue
        email = inv.email.lower()
        if email in seen_emails:
            continue
        seen_emails.add(email)
        viewers.append(
            {
                "id": str(inv.id),
                "email": inv.email,
                "first_name": inv.first_name,
                "last_name": inv.last_name,
                "status": inv.status.value,
                "role": "viewer",
                "invited_at": inv.created_at.isoformat() if inv.created_at else None,
                "expires_at": inv.expires_at.isoformat() if inv.expires_at else None,
                "accepted_at": inv.accepted_at.isoformat() if inv.accepted_at else None,
                "invitation_id": str(inv.id),
                "seat_id": None,
            }
        )

    for seat in seats:
        email = seat.user.email.lower()
        if email in seen_emails:
            continue
        seen_emails.add(email)
        name = f"{seat.user.first_name} {seat.user.last_name}".strip() or email
        viewers.append(
            {
                "id": str(seat.id),
                "email": seat.user.email,
                "first_name": seat.user.first_name,
                "last_name": seat.user.last_name,
                "name": name,
                "status": "active",
                "role": "viewer",
                "invited_at": seat.created_at.isoformat() if seat.created_at else None,
                "expires_at": None,
                "accepted_at": seat.created_at.isoformat() if seat.created_at else None,
                "invitation_id": None,
                "seat_id": str(seat.id),
            }
        )

    counts = {
        "total": len(viewers),
        "active": len([v for v in viewers if v["status"] == "active"]),
        "pending": len([v for v in viewers if v["status"] == "pending"]),
        "revoked": len([v for v in viewers if v["status"] == "revoked"]),
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
