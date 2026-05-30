"""
View-Only User Management API — v2.1
Pro & Enterprise: unlimited free viewer invites with email acceptance flow.
"""

import uuid as uuid_lib
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.endpoints.auth import get_current_active_user
from app.db.database import get_db
from app.db.models import (
    Seat,
    User,
    UserRole,
    WorkspaceInvitation,
    WorkspaceInvitationStatus,
)
from app.services.email_service import send_workspace_viewer_invite_email
from app.services.workspace_invite_service import (
    INVITE_TTL_DAYS,
    assert_viewer_invite_allowed,
    generate_invite_token,
    get_admin_seat,
    invite_accept_url,
    list_viewers_for_org,
)

router = APIRouter()


class ViewerInvite(BaseModel):
    email: EmailStr
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    message: Optional[str] = None


@router.post("/invite", response_model=dict, status_code=status.HTTP_201_CREATED)
async def invite_viewer(
    data: ViewerInvite,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Invite a view-only user (Admin on Pro/Enterprise). Sends email with accept link."""
    org, seat = await get_admin_seat(current_user, db)
    await assert_viewer_invite_allowed(org)

    email = data.email.strip().lower()

    existing_user = await db.execute(select(User).where(User.email == email))
    user = existing_user.scalar_one_or_none()
    if user:
        seat_check = await db.execute(
            select(Seat).where(
                Seat.organization_id == org.id,
                Seat.user_id == user.id,
                Seat.is_active == True,
                Seat.role == UserRole.VIEWER,
            )
        )
        if seat_check.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="User is already an active viewer")

    pending = await db.execute(
        select(WorkspaceInvitation).where(
            WorkspaceInvitation.organization_id == org.id,
            WorkspaceInvitation.email == email,
            WorkspaceInvitation.status == WorkspaceInvitationStatus.PENDING,
            WorkspaceInvitation.role == UserRole.VIEWER,
        )
    )
    if pending.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="A pending invitation already exists for this email")

    token = generate_invite_token()
    expires_at = datetime.utcnow() + timedelta(days=INVITE_TTL_DAYS)

    invitation = WorkspaceInvitation(
        organization_id=org.id,
        invited_by_id=current_user.id,
        email=email,
        role=UserRole.VIEWER,
        token=token,
        status=WorkspaceInvitationStatus.PENDING,
        first_name=(data.first_name or "").strip() or None,
        last_name=(data.last_name or "").strip() or None,
        message=(data.message or "").strip() or None,
        expires_at=expires_at,
    )
    db.add(invitation)
    await db.flush()

    inviter_name = (
        f"{current_user.first_name} {current_user.last_name}".strip()
        or current_user.email
    )
    accept_link = invite_accept_url(token)
    email_sent = await send_workspace_viewer_invite_email(
        to_email=email,
        invited_name=data.first_name or email.split("@")[0],
        inviter_name=inviter_name,
        organization_name=org.name,
        accept_url=accept_link,
        custom_message=data.message,
    )

    await db.commit()

    return {
        "viewer": {
            "id": str(invitation.id),
            "email": email,
            "first_name": invitation.first_name,
            "last_name": invitation.last_name,
            "status": "pending",
            "role": "viewer",
            "invited_at": invitation.created_at.isoformat(),
            "expires_at": expires_at.isoformat(),
        },
        "delivery": {
            "email_sent": email_sent,
            "accept_url": accept_link if not email_sent else None,
        },
        "message": (
            "Invitation email sent."
            if email_sent
            else "Invitation created. Email was not sent (check SMTP settings). Use accept_url for testing."
        ),
    }


@router.get("/", response_model=dict)
async def list_viewers(
    status_filter: Optional[str] = None,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """List view-only users and pending invitations. Admin on Pro/Enterprise only."""
    org, _seat = await get_admin_seat(current_user, db)
    payload = await list_viewers_for_org(org.id, db)
    if status_filter:
        payload["viewers"] = [v for v in payload["viewers"] if v["status"] == status_filter]
    payload["workspace_id"] = str(org.id)
    return payload


@router.post("/{viewer_id}/revoke", response_model=dict)
async def revoke_viewer(
    viewer_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke pending invitation or deactivate active viewer seat."""
    org, _seat = await get_admin_seat(current_user, db)

    try:
        inv_uuid = uuid_lib.UUID(viewer_id)
    except ValueError:
        inv_uuid = None

    if inv_uuid:
        inv_result = await db.execute(
            select(WorkspaceInvitation).where(
                WorkspaceInvitation.id == inv_uuid,
                WorkspaceInvitation.organization_id == org.id,
            )
        )
        invitation = inv_result.scalar_one_or_none()
        if invitation:
            if invitation.status == WorkspaceInvitationStatus.REVOKED:
                raise HTTPException(status_code=400, detail="Invitation already revoked")
            invitation.status = WorkspaceInvitationStatus.REVOKED
            await db.commit()
            return {
                "viewer": {"id": str(invitation.id), "email": invitation.email, "status": "revoked"},
                "message": "Invitation revoked.",
            }

    try:
        seat_uuid = uuid_lib.UUID(viewer_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Viewer not found")

    seat_result = await db.execute(
        select(Seat).where(
            Seat.id == seat_uuid,
            Seat.organization_id == org.id,
            Seat.role == UserRole.VIEWER,
        )
    )
    seat = seat_result.scalar_one_or_none()
    if not seat:
        raise HTTPException(status_code=404, detail="Viewer not found")

    seat.is_active = False
    await db.commit()
    return {
        "viewer": {"id": str(seat.id), "email": seat.user.email if seat.user else "", "status": "revoked"},
        "message": "Viewer access revoked.",
    }


@router.get("/me", response_model=dict)
async def viewer_me(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Returns the view-only user's accessible resources."""
    from app.services.workspace_invite_service import resolve_primary_seat

    seat = await resolve_primary_seat(current_user.id, db)
    if not seat or seat.role != UserRole.VIEWER:
        raise HTTPException(status_code=403, detail="Endpoint reserved for View-Only users")

    return {
        "role": "viewer",
        "workspace_id": str(seat.organization_id),
        "accessible_resources": {
            "dashboards": {"read": True, "write": False, "interact": False},
            "tasks": {"read": True, "write": False, "assign": False, "approve": False, "create": False, "comment": False},
            "compliance": {"read": True, "write": False, "interact": False},
            "reminders": {"read": True, "write": False, "interact": False},
        },
        "restricted_modules": [
            "intelligence",
            "documents",
            "news",
            "blog",
            "teams",
            "settings",
            "integrations",
            "admin",
        ],
        "restricted_actions": [
            "use_intelligence",
            "create_tasks",
            "edit_tasks",
            "delete_tasks",
            "assign_tasks",
            "approve_tasks",
            "comment_on_tasks",
            "upload_documents",
            "edit_anything",
            "interact",
            "invite_users",
            "revoke_users",
            "manage_integrations",
            "manage_billing",
            "configure_alerts",
            "export_audit_trail",
        ],
        "note": "View-Only access: read Dashboard, Tasks, Compliance, and Reminders only.",
    }
