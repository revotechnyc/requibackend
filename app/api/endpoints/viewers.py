"""
Workspace member invites — Pro/Enterprise viewers; Enterprise paid seats.
"""

import uuid as uuid_lib
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.endpoints.auth import get_current_active_user, get_password_hash
from app.core.provisioned_password import generate_temp_password
from app.db.database import get_db
from app.db.models import (
    Organization,
    PlanType,
    Seat,
    User,
    UserRole,
    WorkspaceInvitation,
    WorkspaceInvitationStatus,
)
from app.services.workspace_permissions import (
    effective_feature_permissions,
    role_default_permissions_snapshot,
    sanitize_permissions_payload,
)
from app.services.enterprise_roles import (
    INVITEABLE_ENTERPRISE_ROLES,
    role_catalog_for_api,
    role_display_payload,
)
from app.services.seat_allocation import (
    billing_snapshot_for_org,
    is_paid_role,
    release_paid_seat_if_unused,
    reserve_paid_seat,
)
from app.services.workspace_invite_service import (
    _org_plan_type,
    assert_workspace_invite_allowed,
    clear_seat_provisioned_password,
    get_admin_seat,
    invite_role_value,
    invite_status_value,
    list_viewers_for_org,
    list_workspace_members_for_org,
)
from app.services.user_password_flags import (
    set_user_must_change_password,
    user_must_change_password,
)

router = APIRouter()

INVITEABLE_ROLE_VALUES = frozenset(INVITEABLE_ENTERPRISE_ROLES)


class WorkspaceMemberInvite(BaseModel):
    email: EmailStr
    role: str = UserRole.VIEWER.value
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    message: Optional[str] = None
    password: Optional[str] = None

    @field_validator("role")
    @classmethod
    def normalize_role(cls, v: str) -> str:
        r = (v or UserRole.VIEWER.value).strip().lower()
        if r not in INVITEABLE_ROLE_VALUES:
            raise ValueError(
                "role must be one of: admin, reviewer, approver, contributor, analyst, viewer"
            )
        return r

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class ViewerInvite(WorkspaceMemberInvite):
    """Backward-compatible alias (viewer default)."""
    pass


class WorkspaceMemberUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    role: Optional[str] = None
    feature_permissions: Optional[Dict[str, bool]] = None


async def _resolve_member_in_org(
    member_id: str,
    org_id,
    db: AsyncSession,
) -> tuple[str, WorkspaceInvitation | Seat, User | None]:
    try:
        member_uuid = uuid_lib.UUID(member_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Member not found") from exc

    inv_result = await db.execute(
        select(WorkspaceInvitation)
        .where(
            WorkspaceInvitation.id == member_uuid,
            WorkspaceInvitation.organization_id == org_id,
        )
        .options(selectinload(WorkspaceInvitation.organization).selectinload(Organization.subscription))
    )
    invitation = inv_result.scalar_one_or_none()
    if invitation:
        return "invitation", invitation, None

    seat_result = await db.execute(
        select(Seat)
        .where(Seat.id == member_uuid, Seat.organization_id == org_id)
        .options(
            selectinload(Seat.user),
            selectinload(Seat.organization).selectinload(Organization.subscription),
        )
    )
    seat = seat_result.scalar_one_or_none()
    if seat:
        return "seat", seat, seat.user

    raise HTTPException(status_code=404, detail="Member not found")


def _member_detail_payload(
    member_type: str,
    invitation: WorkspaceInvitation | None,
    seat: Seat | None,
    user: User | None,
    plan: PlanType,
    requires_password_change: bool = False,
) -> dict:
    if member_type == "invitation" and invitation:
        role = UserRole(invite_role_value(invitation.role))
        return {
            "id": str(invitation.id),
            "member_type": "invitation",
            "email": invitation.email,
            "first_name": invitation.first_name,
            "last_name": invitation.last_name,
            "status": invite_status_value(invitation.status),
            "role": role.value,
            "invited_at": invitation.created_at.isoformat() if invitation.created_at else None,
            "expires_at": invitation.expires_at.isoformat() if invitation.expires_at else None,
            "accepted_at": invitation.accepted_at.isoformat() if invitation.accepted_at else None,
            "revocable": invitation.status == WorkspaceInvitationStatus.PENDING.value,
            "feature_permissions": invitation.feature_permissions,
            "effective_permissions": effective_feature_permissions(
                plan, role, invitation.feature_permissions
            ),
            "editable_fields": ["first_name", "last_name", "role", "feature_permissions"],
        }

    if member_type == "seat" and seat:
        user_email = user.email if user else ""
        return {
            "id": str(seat.id),
            "member_type": "seat",
            "user_id": str(seat.user_id),
            "email": user_email,
            "first_name": user.first_name if user else "",
            "last_name": user.last_name if user else "",
            "status": "active" if seat.is_active else "revoked",
            "role": seat.role.value,
            "invited_at": seat.created_at.isoformat() if seat.created_at else None,
            "accepted_at": seat.created_at.isoformat() if seat.created_at else None,
            "revocable": seat.role == UserRole.VIEWER,
            "feature_permissions": seat.feature_permissions,
            "effective_permissions": effective_feature_permissions(
                plan, seat.role, seat.feature_permissions
            ),
            "editable_fields": ["first_name", "last_name", "role", "feature_permissions"],
            "requires_password_change": requires_password_change,
        }

    raise HTTPException(status_code=404, detail="Member not found")


def _default_invite_permissions(org: Organization, role: UserRole) -> dict:
    plan = _org_plan_type(org)
    return role_default_permissions_snapshot(plan, role)


@router.post("/invite", response_model=dict, status_code=status.HTTP_201_CREATED)
async def invite_workspace_member(
    data: WorkspaceMemberInvite,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Create an active workspace member account (no invitation email)."""
    org, seat = await get_admin_seat(current_user, db)
    try:
        target_role = UserRole(data.role)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid role")

    assert_workspace_invite_allowed(
        org, target_role, seat.role, inviter_user_id=current_user.id
    )

    email = data.email.strip().lower()
    first_name = (data.first_name or "").strip() or email.split("@")[0]
    last_name = (data.last_name or "").strip()

    existing_user_result = await db.execute(select(User).where(User.email == email))
    user = existing_user_result.scalar_one_or_none()

    if user:
        seat_check = await db.execute(
            select(Seat).where(
                Seat.organization_id == org.id,
                Seat.user_id == user.id,
                Seat.is_active == True,
            )
        )
        if seat_check.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="User is already a team member")

    pending_result = await db.execute(
        select(WorkspaceInvitation).where(
            WorkspaceInvitation.organization_id == org.id,
            WorkspaceInvitation.email == email,
            WorkspaceInvitation.status == WorkspaceInvitationStatus.PENDING.value,
        )
    )
    pending_invitation = pending_result.scalar_one_or_none()
    if pending_invitation:
        pending_invitation.status = WorkspaceInvitationStatus.REVOKED.value
        if is_paid_role(invite_role_value(pending_invitation.role)):
            await release_paid_seat_if_unused(org, db)

    seat_billing = None
    if is_paid_role(target_role):
        seat_billing = await reserve_paid_seat(org, db)

    feature_permissions = _default_invite_permissions(org, target_role)
    temporary_password: str | None = None
    created_new_user = False

    if user:
        inactive_seat_result = await db.execute(
            select(Seat).where(
                Seat.organization_id == org.id,
                Seat.user_id == user.id,
            )
        )
        member_seat = inactive_seat_result.scalar_one_or_none()
        if member_seat:
            member_seat.is_active = True
            member_seat.role = target_role
            member_seat.feature_permissions = feature_permissions
        else:
            member_seat = Seat(
                organization_id=org.id,
                user_id=user.id,
                role=target_role,
                is_active=True,
                feature_permissions=feature_permissions,
            )
            db.add(member_seat)
        if data.first_name:
            user.first_name = first_name
        if data.last_name:
            user.last_name = last_name
    else:
        initial_password = (data.password or "").strip() or generate_temp_password()
        temporary_password = initial_password
        created_new_user = True
        user = User(
            email=email,
            hashed_password=get_password_hash(initial_password),
            first_name=first_name,
            last_name=last_name,
            is_active=True,
        )
        db.add(user)
        await db.flush()
        await set_user_must_change_password(user.id, True, db)
        member_seat = Seat(
            organization_id=org.id,
            user_id=user.id,
            role=target_role,
            is_active=True,
            feature_permissions=feature_permissions,
        )
        db.add(member_seat)

    await db.flush()

    billing = await billing_snapshot_for_org(org, db)
    seat_message = ""
    if seat_billing and seat_billing.get("seat_added"):
        seat_message = (
            f" 1 paid seat added to your subscription "
            f"({seat_billing['previous_seat_quantity']} → {seat_billing['new_seat_quantity']})."
        )

    plan = _org_plan_type(org)
    member_payload = _member_detail_payload(
        "seat",
        None,
        member_seat,
        user,
        plan,
        requires_password_change=created_new_user,
    )
    if temporary_password:
        member_payload["temporary_password"] = temporary_password

    await db.commit()
    await db.refresh(member_seat)
    if user:
        await db.refresh(user)

    if created_new_user:
        message = (
            "Team member account created. Copy the temporary password now — "
            "it will not be shown again."
            + seat_message
        )
    else:
        message = (
            "Existing user added to your workspace. They should sign in with their current password."
            + seat_message
        )

    return {
        "member": member_payload,
        "temporary_password": temporary_password,
        "seat_billing": seat_billing,
        "billing": billing,
        "message": message,
    }


@router.get("/", response_model=dict)
async def list_workspace_members(
    members_only: Optional[bool] = None,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """List workspace members (active seats + pending invitations). Admin only."""
    org, _seat = await get_admin_seat(current_user, db)
    if members_only:
        payload = await list_workspace_members_for_org(org.id, db)
        payload["workspace_id"] = str(org.id)
        return payload
    payload = await list_viewers_for_org(org.id, db)
    payload["workspace_id"] = str(org.id)
    return payload


@router.get("/members/{member_id}", response_model=dict)
async def get_workspace_member(
    member_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a single member (active seat or pending invitation) for admin edit."""
    org, _seat = await get_admin_seat(current_user, db)
    member_type, record, user = await _resolve_member_in_org(member_id, org.id, db)
    plan = _org_plan_type(org)
    if member_type == "invitation":
        return {
            "member": _member_detail_payload("invitation", record, None, None, plan),
            "workspace_id": str(org.id),
            "plan": plan.value,
        }
    requires_change = False
    if user:
        requires_change = await user_must_change_password(user.id, db)
    return {
        "member": _member_detail_payload(
            "seat", None, record, user, plan, requires_password_change=requires_change
        ),
        "workspace_id": str(org.id),
        "plan": plan.value,
    }


@router.patch("/members/{member_id}", response_model=dict)
async def update_workspace_member(
    member_id: str,
    data: WorkspaceMemberUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Update member profile, role, and tab permissions."""
    org, admin_seat = await get_admin_seat(current_user, db)
    member_type, record, user = await _resolve_member_in_org(member_id, org.id, db)
    plan = _org_plan_type(org)

    if member_type == "invitation":
        invitation: WorkspaceInvitation = record
        if invitation.status != WorkspaceInvitationStatus.PENDING.value:
            raise HTTPException(status_code=400, detail="Only pending invitations can be edited")

        if data.first_name is not None:
            invitation.first_name = data.first_name.strip() or None
        if data.last_name is not None:
            invitation.last_name = data.last_name.strip() or None
        if data.role is not None:
            try:
                target_role = UserRole(data.role.strip().lower())
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid role") from exc
            assert_workspace_invite_allowed(
                org,
                target_role,
                admin_seat.role,
                inviter_user_id=current_user.id,
            )
            previous_role = UserRole(invite_role_value(invitation.role))
            was_paid = is_paid_role(previous_role)
            will_be_paid = is_paid_role(target_role)
            if will_be_paid and not was_paid:
                await reserve_paid_seat(org, db)
            elif was_paid and not will_be_paid:
                await release_paid_seat_if_unused(org, db)
            invitation.role = target_role.value
            if data.feature_permissions is None:
                invitation.feature_permissions = _default_invite_permissions(
                    org, target_role
                )
        if data.feature_permissions is not None:
            if plan != PlanType.ENTERPRISE:
                raise HTTPException(
                    status_code=403,
                    detail="Custom module permissions require an Enterprise plan",
                )
            role_for_perm = UserRole(invite_role_value(invitation.role))
            invitation.feature_permissions = sanitize_permissions_payload(
                data.feature_permissions, plan, role_for_perm
            )

        await db.commit()
        await db.refresh(invitation)
        return {
            "member": _member_detail_payload("invitation", invitation, None, None, plan),
            "message": "Invitation updated.",
        }

    seat: Seat = record
    if not seat.is_active:
        raise HTTPException(status_code=400, detail="Cannot edit inactive members")

    if data.first_name is not None and user:
        user.first_name = data.first_name.strip() or user.first_name
    if data.last_name is not None and user:
        user.last_name = data.last_name.strip() or user.last_name
    if data.role is not None:
        try:
            target_role = UserRole(data.role.strip().lower())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid role") from exc
        if seat.user_id == current_user.id and admin_seat.role in (
            UserRole.ADMIN,
            UserRole.ENTERPRISE_ADMIN,
        ):
            if target_role not in (UserRole.ADMIN, UserRole.ENTERPRISE_ADMIN):
                raise HTTPException(
                    status_code=400,
                    detail="You cannot change your own admin role here",
                )
        assert_workspace_invite_allowed(
            org,
            target_role,
            admin_seat.role,
            inviter_user_id=current_user.id,
        )
        was_paid = is_paid_role(seat.role)
        will_be_paid = is_paid_role(target_role)
        if will_be_paid and not was_paid:
            await reserve_paid_seat(org, db)
        elif was_paid and not will_be_paid:
            await release_paid_seat_if_unused(org, db)
        seat.role = target_role
    if data.feature_permissions is not None:
        if plan != PlanType.ENTERPRISE:
            raise HTTPException(
                status_code=403,
                detail="Custom module permissions require an Enterprise plan",
            )
        seat.feature_permissions = sanitize_permissions_payload(
            data.feature_permissions, plan, seat.role
        )

    await db.commit()
    if user:
        await db.refresh(user)
    await db.refresh(seat)
    requires_change = await user_must_change_password(seat.user_id, db) if seat.user_id else False
    return {
        "member": _member_detail_payload(
            "seat", None, seat, user, plan, requires_password_change=requires_change
        ),
        "message": "Member updated.",
    }


@router.post("/members/{member_id}/reset-password", response_model=dict)
async def reset_workspace_member_password(
    member_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate a new temporary password for an active member (shown once)."""
    org, _seat = await get_admin_seat(current_user, db)
    member_type, record, user = await _resolve_member_in_org(member_id, org.id, db)
    if member_type != "seat" or not user:
        raise HTTPException(
            status_code=400,
            detail="Only active team members can have their password reset",
        )

    seat: Seat = record
    if user.id == current_user.id:
        raise HTTPException(
            status_code=400,
            detail="Use account settings to change your own password",
        )

    temporary_password = generate_temp_password()
    user.hashed_password = get_password_hash(temporary_password)
    await set_user_must_change_password(user.id, True, db)
    await clear_seat_provisioned_password(seat.id, db)

    await db.commit()
    await db.refresh(user)

    plan = _org_plan_type(org)
    member_payload = _member_detail_payload(
        "seat", None, seat, user, plan, requires_password_change=True
    )
    member_payload["temporary_password"] = temporary_password

    return {
        "member": member_payload,
        "temporary_password": temporary_password,
        "message": (
            "Password reset. Copy the temporary password now — it will not be shown again."
        ),
    }


@router.get("/seat-usage", response_model=dict)
async def get_seat_usage(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Billed vs allocated paid seats (for invite gating UI)."""
    org, _seat = await get_admin_seat(current_user, db)
    billing = await billing_snapshot_for_org(org, db)
    return {
        "workspace_id": str(org.id),
        "plan": _org_plan_type(org).value,
        "billing": billing,
        "role_catalog": role_catalog_for_api(),
    }


@router.get("/members", response_model=dict)
async def list_all_workspace_members(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """List all roles: paid seats + viewers (pending and active)."""
    org, _seat = await get_admin_seat(current_user, db)
    payload = await list_workspace_members_for_org(org.id, db)
    payload["workspace_id"] = str(org.id)
    payload["billing"] = await billing_snapshot_for_org(org, db)
    payload["role_catalog"] = role_catalog_for_api()
    return payload


@router.post("/{member_id}/revoke", response_model=dict)
async def revoke_workspace_member(
    member_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke pending invitation or deactivate viewer seat."""
    org, _seat = await get_admin_seat(current_user, db)

    try:
        inv_uuid = uuid_lib.UUID(member_id)
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
            if invitation.status == WorkspaceInvitationStatus.REVOKED.value:
                raise HTTPException(status_code=400, detail="Invitation already revoked")
            invitation.status = WorkspaceInvitationStatus.REVOKED.value
            seat_billing = None
            if is_paid_role(invite_role_value(invitation.role)):
                seat_billing = await release_paid_seat_if_unused(org, db)
            await db.commit()
            billing = await billing_snapshot_for_org(org, db)
            return {
                "member": {
                    "id": str(invitation.id),
                    "email": invitation.email,
                    "status": "revoked",
                    "role": invite_role_value(invitation.role),
                },
                "seat_billing": seat_billing,
                "billing": billing,
                "message": "Invitation revoked.",
            }

    try:
        seat_uuid = uuid_lib.UUID(member_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Member not found")

    seat_result = await db.execute(
        select(Seat).where(
            Seat.id == seat_uuid,
            Seat.organization_id == org.id,
        )
    )
    seat = seat_result.scalar_one_or_none()
    if not seat:
        raise HTTPException(status_code=404, detail="Member not found")

    if seat.role != UserRole.VIEWER:
        raise HTTPException(
            status_code=400,
            detail="Only viewer seats can be revoked from the team page. Contact support to remove paid seats.",
        )

    seat.is_active = False
    await db.commit()
    return {
        "member": {
            "id": str(seat.id),
            "email": seat.user.email if seat.user else "",
            "status": "revoked",
            "role": seat.role.value,
        },
        "message": "Member access revoked.",
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
        "note": "View-Only access: read Dashboard, Tasks, Compliance, and Reminders only.",
    }
