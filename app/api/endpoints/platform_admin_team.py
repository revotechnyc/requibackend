"""
Platform admin team — invite and manage blog/content team members.
"""

import secrets
import string
from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.platform_admin_roles import (
    INVITEABLE_PLATFORM_ROLES,
    PLATFORM_ROLE_DESCRIPTIONS,
    PLATFORM_ROLE_LABELS,
    PlatformAdminRole,
)
from app.core.platform_admin_security import (
    get_current_platform_admin,
    hash_platform_admin_password,
    platform_admin_to_dict,
)
from app.core.config import settings
from app.db.database import get_db
from app.db.models import PlatformAdmin

router = APIRouter()


def _require_super_admin(admin: PlatformAdmin) -> None:
    if admin.role != PlatformAdminRole.SUPER_ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only Super Admin can manage the platform team",
        )


def _generate_temp_password(length: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


class TeamInviteRequest(BaseModel):
    email: EmailStr
    first_name: str
    last_name: str = ""
    role: str
    password: Optional[str] = None

    @field_validator("first_name")
    @classmethod
    def first_name_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("First name is required")
        return v.strip()

    @field_validator("role")
    @classmethod
    def role_inviteable(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in INVITEABLE_PLATFORM_ROLES:
            raise ValueError(
                f"role must be one of: {', '.join(INVITEABLE_PLATFORM_ROLES)}"
            )
        return normalized

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class TeamMemberUpdateRequest(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None

    @field_validator("role")
    @classmethod
    def role_inviteable(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        normalized = v.strip().lower()
        if normalized not in INVITEABLE_PLATFORM_ROLES:
            raise ValueError(
                f"role must be one of: {', '.join(INVITEABLE_PLATFORM_ROLES)}"
            )
        return normalized


def _member_payload(admin: PlatformAdmin, include_inviter: bool = True) -> dict:
    inviter_name = None
    if include_inviter and admin.invited_by:
        inviter_name = (
            f"{admin.invited_by.first_name} {admin.invited_by.last_name}".strip()
            or admin.invited_by.email
        )
    return {
        **platform_admin_to_dict(admin),
        "role_label": PLATFORM_ROLE_LABELS.get(admin.role, admin.role),
        "role_description": PLATFORM_ROLE_DESCRIPTIONS.get(admin.role, ""),
        "is_active": admin.is_active,
        "created_at": admin.created_at.isoformat() if admin.created_at else None,
        "last_login": admin.last_login.isoformat() if admin.last_login else None,
        "invited_by_name": inviter_name,
        "can_manage_team": admin.role == PlatformAdminRole.SUPER_ADMIN.value,
    }


@router.get("/roles")
async def list_inviteable_roles(
    admin: PlatformAdmin = Depends(get_current_platform_admin),
):
    """Roles that Super Admin can assign when inviting team members."""
    _require_super_admin(admin)
    return {
        "roles": [
            {
                "id": role,
                "label": PLATFORM_ROLE_LABELS[role],
                "description": PLATFORM_ROLE_DESCRIPTIONS.get(role, ""),
            }
            for role in INVITEABLE_PLATFORM_ROLES
        ]
    }


@router.get("")
async def list_team_members(
    admin: PlatformAdmin = Depends(get_current_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all platform admin portal users."""
    _require_super_admin(admin)
    result = await db.execute(
        select(PlatformAdmin)
        .options(selectinload(PlatformAdmin.invited_by))
        .order_by(PlatformAdmin.created_at.desc())
    )
    members = result.scalars().all()
    return {
        "members": [_member_payload(m) for m in members],
        "total": len(members),
    }


@router.post("/invite")
async def invite_team_member(
    body: TeamInviteRequest,
    admin: PlatformAdmin = Depends(get_current_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """Invite a blog/content team member to the admin portal."""
    _require_super_admin(admin)
    email = body.email.strip().lower()

    existing = await db.execute(select(PlatformAdmin).where(PlatformAdmin.email == email))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A user with this email already exists",
        )

    temp_password = body.password or _generate_temp_password()
    member = PlatformAdmin(
        email=email,
        hashed_password=hash_platform_admin_password(temp_password),
        first_name=body.first_name,
        last_name=(body.last_name or "").strip(),
        role=body.role,
        is_active=True,
        invited_by_id=admin.id,
    )
    db.add(member)
    await db.flush()
    member_id = member.id
    await db.commit()

    reload = await db.execute(
        select(PlatformAdmin)
        .where(PlatformAdmin.id == member_id)
        .options(selectinload(PlatformAdmin.invited_by))
    )
    member = reload.scalar_one()

    payload = _member_payload(member)
    payload["temporary_password"] = temp_password if not body.password else None
    payload["message"] = (
        "Team member created. Share the temporary password securely; "
        "they should change it after first login."
        if not body.password
        else "Team member created with the password you provided."
    )
    payload["email_sent"] = False

    # Send invitation email if SMTP is configured (never blocks creation).
    if settings.smtp_enabled:
        try:
            from app.services.email_service import send_platform_admin_invite_email

            inviter_name = (
                f"{admin.first_name} {admin.last_name}".strip() or admin.email
            )
            invited_name = (
                f"{member.first_name} {member.last_name}".strip() or member.email
            )
            role_label = PLATFORM_ROLE_LABELS.get(member.role, member.role)

            sent = await send_platform_admin_invite_email(
                to_email=member.email,
                invited_name=invited_name,
                inviter_name=inviter_name,
                role_label=role_label,
                admin_portal_url=settings.admin_portal_url_normalized,
                temporary_password=(temp_password if not body.password else None),
            )
            payload["email_sent"] = bool(sent)
            if sent:
                payload["message"] = (
                    "Invitation email sent. The user can sign in using the credentials provided."
                )
        except Exception:
            # Keep API stable even if email send fails.
            payload["email_sent"] = False
    return payload


@router.patch("/{member_id}")
async def update_team_member(
    member_id: UUID,
    body: TeamMemberUpdateRequest,
    admin: PlatformAdmin = Depends(get_current_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    _require_super_admin(admin)
    result = await db.execute(
        select(PlatformAdmin)
        .where(PlatformAdmin.id == member_id)
        .options(selectinload(PlatformAdmin.invited_by))
    )
    member = result.scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Team member not found")

    if member.role == PlatformAdminRole.SUPER_ADMIN.value and member.id != admin.id:
        if body.role is not None or body.is_active is False:
            raise HTTPException(
                status_code=400,
                detail="Cannot change role or deactivate the platform owner",
            )

    if body.first_name is not None:
        member.first_name = body.first_name.strip()
    if body.last_name is not None:
        member.last_name = body.last_name.strip()
    if body.role is not None:
        member.role = body.role
    if body.is_active is not None:
        member.is_active = body.is_active

    member.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(member)
    return {"member": _member_payload(member)}


@router.post("/{member_id}/deactivate")
async def deactivate_team_member(
    member_id: UUID,
    admin: PlatformAdmin = Depends(get_current_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    _require_super_admin(admin)
    if str(member_id) == str(admin.id):
        raise HTTPException(status_code=400, detail="You cannot deactivate your own account")

    result = await db.execute(select(PlatformAdmin).where(PlatformAdmin.id == member_id))
    member = result.scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Team member not found")
    if member.role == PlatformAdminRole.SUPER_ADMIN.value:
        raise HTTPException(status_code=400, detail="Cannot deactivate Super Admin")

    member.is_active = False
    member.updated_at = datetime.utcnow()
    await db.commit()
    return {"success": True, "message": "Team member deactivated"}
