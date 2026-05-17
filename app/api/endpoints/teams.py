"""
Team management endpoints
Handles teams, members, invitations, and role management
"""

import secrets
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.endpoints.auth import get_current_active_user
from app.core.permissions import (
    PermissionChecker,
    PLAN_FEATURES,
    ROLE_HIERARCHY,
    FeatureGate,
)
from app.db.database import get_db
from app.db.models import (
    Organization,
    PlanType,
    Seat,
    SubscriptionStatus,
    User,
    UserRole,
)

router = APIRouter()


# Pydantic models
class TeamCreate(BaseModel):
    name: str
    description: Optional[str] = None


class TeamUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class InviteCreate(BaseModel):
    email: EmailStr
    role: str = "viewer"


class RoleUpdate(BaseModel):
    role: str


class TeamMemberOut(BaseModel):
    id: str
    name: str
    email: str
    role: str
    status: str
    joined_at: Optional[str] = None
    avatar: Optional[str] = None


class TeamOut(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    member_count: int
    members: List[TeamMemberOut]
    created_at: str


class InviteOut(BaseModel):
    id: str
    email: str
    role: str
    status: str
    expires_at: str


# ==========================
# HELPERS
# ==========================

async def get_user_org_and_seat(
    user: User,
    db: AsyncSession,
) -> tuple[Organization, Seat]:
    """Get user's primary active organization and seat"""
    result = await db.execute(
        select(Seat)
        .where(Seat.user_id == user.id, Seat.is_active == True)
        .options(selectinload(Seat.organization).selectinload(Organization.subscription))
    )
    seat = result.scalar_one_or_none()
    if not seat:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No active organization membership"
        )
    return seat.organization, seat


def serialize_user(user: User, role: str, status: str = "active", joined_at: Optional[datetime] = None) -> dict:
    """Serialize user as team member"""
    return {
        "id": str(user.id),
        "name": f"{user.first_name} {user.last_name}",
        "email": user.email,
        "role": role,
        "status": status,
        "joined_at": joined_at.isoformat() if joined_at else None,
        "avatar": None,
    }


# ==========================
# ENDPOINTS
# ==========================

@router.get("/", response_model=dict)
async def list_teams(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """List teams in user's organization"""
    org, seat = await get_user_org_and_seat(current_user, db)
    
    # Check plan allows teams
    if not FeatureGate.has_feature(
        org.subscription.plan_type if org.subscription else PlanType.STANDARD,
        "teams"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Team feature not available on your plan. Upgrade to Pro or Enterprise."
        )
    
    # Get all active seats (members) in the organization
    result = await db.execute(
        select(Seat)
        .where(Seat.organization_id == org.id, Seat.is_active == True)
        .options(selectinload(Seat.user))
    )
    seats = result.scalars().all()
    
    members = [
        serialize_user(s.user, s.role.value, "active", s.created_at)
        for s in seats
    ]
    
    # Return as a single "default" team for the org
    # In a multi-team setup, this would query Team models
    team = {
        "id": str(org.id),
        "name": org.name,
        "description": "Primary organization team",
        "member_count": len(members),
        "members": members,
        "created_at": org.created_at.isoformat(),
    }
    
    return {"teams": [team]}


@router.get("/{team_id}", response_model=dict)
async def get_team(
    team_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get team details with members"""
    org, seat = await get_user_org_and_seat(current_user, db)
    
    # For now, team_id maps to organization id
    if str(org.id) != team_id:
        raise HTTPException(status_code=404, detail="Team not found")
    
    # Check plan
    if not FeatureGate.has_feature(
        org.subscription.plan_type if org.subscription else PlanType.STANDARD,
        "teams"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Team feature not available on your plan"
        )
    
    result = await db.execute(
        select(Seat)
        .where(Seat.organization_id == org.id, Seat.is_active == True)
        .options(selectinload(Seat.user))
    )
    seats = result.scalars().all()
    
    members = [
        serialize_user(s.user, s.role.value, "active", s.created_at)
        for s in seats
    ]
    
    return {
        "id": str(org.id),
        "name": org.name,
        "description": "Primary organization team",
        "member_count": len(members),
        "members": members,
        "created_at": org.created_at.isoformat(),
        "user_role": seat.role.value,
        "can_manage": PermissionChecker.can_manage_role(seat.role, UserRole.VIEWER),
    }


@router.post("/{team_id}/invite", response_model=dict)
async def invite_member(
    team_id: str,
    data: InviteCreate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Invite a new member to the team/organization"""
    org, seat = await get_user_org_and_seat(current_user, db)
    
    if str(org.id) != team_id:
        raise HTTPException(status_code=404, detail="Team not found")
    
    # Permission check - must be able to invite
    if not PermissionChecker.has_permission(seat.role, "invite"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to invite members"
        )
    
    # Check plan allows teams
    if not FeatureGate.has_feature(
        org.subscription.plan_type if org.subscription else PlanType.STANDARD,
        "teams"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Team feature not available on your plan"
        )
    
    # Validate requested role
    try:
        target_role = UserRole(data.role.lower())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid role")
    
    # Check role hierarchy - can only assign lower roles
    if not PermissionChecker.can_manage_role(seat.role, target_role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You cannot assign the {data.role} role"
        )
    
    # Check if user already exists
    user_result = await db.execute(select(User).where(User.email == data.email))
    existing_user = user_result.scalar_one_or_none()
    
    if existing_user:
        # Check if already a member
        seat_result = await db.execute(
            select(Seat).where(
                Seat.organization_id == org.id,
                Seat.user_id == existing_user.id,
            )
        )
        existing_seat = seat_result.scalar_one_or_none()
        
        if existing_seat and existing_seat.is_active:
            raise HTTPException(status_code=400, detail="User is already a team member")
        
        if existing_seat:
            # Reactivate
            existing_seat.is_active = True
            existing_seat.role = target_role
        else:
            # Create new seat
            new_seat = Seat(
                organization_id=org.id,
                user_id=existing_user.id,
                role=target_role,
                is_active=True,
            )
            db.add(new_seat)
    else:
        # User doesn't exist - in a real system, send email invitation
        # For now, we create a placeholder user
        from app.api.endpoints.auth import get_password_hash
        placeholder = User(
            email=data.email,
            hashed_password=get_password_hash(secrets.token_urlsafe(32)),
            first_name=data.email.split("@")[0],
            last_name="",
            is_active=True,
        )
        db.add(placeholder)
        await db.flush()
        
        new_seat = Seat(
            organization_id=org.id,
            user_id=placeholder.id,
            role=target_role,
            is_active=True,
        )
        db.add(new_seat)
    
    await db.commit()
    
    return {
        "message": f"Invitation sent to {data.email}",
        "email": data.email,
        "role": data.role,
        "status": "pending",
    }


@router.patch("/{team_id}/members/{member_id}/role", response_model=dict)
async def update_member_role(
    team_id: str,
    member_id: str,
    data: RoleUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a team member's role"""
    org, seat = await get_user_org_and_seat(current_user, db)
    
    if str(org.id) != team_id:
        raise HTTPException(status_code=404, detail="Team not found")
    
    # Permission check
    if not PermissionChecker.has_permission(seat.role, "change_role"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to change roles"
        )
    
    # Get target member's seat
    result = await db.execute(
        select(Seat).where(
            Seat.organization_id == org.id,
            Seat.user_id == member_id,
            Seat.is_active == True,
        )
    )
    target_seat = result.scalar_one_or_none()
    
    if not target_seat:
        raise HTTPException(status_code=404, detail="Member not found")
    
    # Can't change own role through this endpoint
    if str(target_seat.user_id) == str(current_user.id):
        raise HTTPException(status_code=400, detail="Cannot change your own role here")
    
    # Validate new role
    try:
        new_role = UserRole(data.role.lower())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid role")
    
    # Check hierarchy - can only manage lower or equal roles
    if not PermissionChecker.can_manage_role(seat.role, target_seat.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot manage this member's role"
        )
    
    # Can't assign higher role than yourself
    if ROLE_HIERARCHY.get(new_role, 0) >= ROLE_HIERARCHY.get(seat.role, 0):
        if seat.role != UserRole.ADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot assign a role equal to or higher than your own"
            )
    
    target_seat.role = new_role
    await db.commit()
    
    return {
        "message": "Role updated",
        "member_id": member_id,
        "new_role": data.role,
    }


@router.delete("/{team_id}/members/{member_id}", response_model=dict)
async def remove_member(
    team_id: str,
    member_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove a member from the team"""
    org, seat = await get_user_org_and_seat(current_user, db)
    
    if str(org.id) != team_id:
        raise HTTPException(status_code=404, detail="Team not found")
    
    # Permission check
    if not PermissionChecker.has_permission(seat.role, "remove_user"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to remove members"
        )
    
    # Get target member
    result = await db.execute(
        select(Seat).where(
            Seat.organization_id == org.id,
            Seat.user_id == member_id,
            Seat.is_active == True,
        )
    )
    target_seat = result.scalar_one_or_none()
    
    if not target_seat:
        raise HTTPException(status_code=404, detail="Member not found")
    
    # Can't remove yourself
    if str(target_seat.user_id) == str(current_user.id):
        raise HTTPException(status_code=400, detail="Cannot remove yourself")
    
    # Can't remove admin
    if target_seat.role == UserRole.ADMIN:
        raise HTTPException(status_code=400, detail="Cannot remove an admin")
    
    # Check hierarchy
    if not PermissionChecker.can_manage_role(seat.role, target_seat.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot remove this member"
        )
    
    target_seat.is_active = False
    await db.commit()
    
    return {"message": "Member removed"}


@router.get("/{team_id}/audit", response_model=dict)
async def get_team_audit(
    team_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get audit logs for team actions"""
    org, seat = await get_user_org_and_seat(current_user, db)
    
    if str(org.id) != team_id:
        raise HTTPException(status_code=404, detail="Team not found")
    
    # Only admin/president/vp can view audit
    if not PermissionChecker.has_permission(seat.role, "view_audit"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied"
        )
    
    from app.db.models import AuditLog
    result = await db.execute(
        select(AuditLog)
        .where(AuditLog.organization_id == org.id)
        .order_by(AuditLog.created_at.desc())
        .limit(50)
    )
    logs = result.scalars().all()
    
    return {
        "logs": [
            {
                "id": str(l.id),
                "action": l.action,
                "resource_type": l.resource_type,
                "previous_state": l.previous_state,
                "new_state": l.new_state,
                "created_at": l.created_at.isoformat(),
            }
            for l in logs
        ]
    }
