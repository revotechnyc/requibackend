"""
Organization management endpoints
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.endpoints.auth import get_current_active_user
from app.db.database import get_db
from app.db.models import Organization, Seat, Subscription, User, UserRole

router = APIRouter()

# Role hierarchy for permission checks (numeric, higher = more power)
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

def can_manage(user_role: UserRole) -> bool:
    """Check if role can manage organization (Admin, President, VP, Director, Manager)"""
    return ROLE_HIERARCHY.get(user_role, 0) >= 60

def can_administrate(user_role: UserRole) -> bool:
    """Check if role can perform admin operations (Admin, President, VP)"""
    return ROLE_HIERARCHY.get(user_role, 0) >= 80


class OrganizationCreate(BaseModel):
    name: str
    slug: str
    description: Optional[str] = None


class OrganizationUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    settings: Optional[dict] = None


class SeatCreate(BaseModel):
    user_email: str
    role: str  # admin, president, vice_president, director, manager, team_lead, specialist, assistant, viewer


@router.post("/", response_model=dict)
async def create_organization(
    data: OrganizationCreate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Create new organization"""
    # Check if slug exists
    result = await db.execute(
        select(Organization).where(Organization.slug == data.slug)
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Organization slug already exists")
    
    # Create organization
    org = Organization(
        name=data.name,
        slug=data.slug,
        description=data.description,
        owner_id=current_user.id,
    )
    db.add(org)
    await db.commit()
    await db.refresh(org)
    
    # Create admin seat for creator
    seat = Seat(
        organization_id=org.id,
        user_id=current_user.id,
        role=UserRole.ADMIN,
        is_active=True,
    )
    db.add(seat)
    await db.commit()
    
    return {
        "id": str(org.id),
        "name": org.name,
        "slug": org.slug,
        "message": "Organization created successfully",
    }


@router.get("/", response_model=List[dict])
async def list_organizations(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """List user's organizations"""
    result = await db.execute(
        select(Organization)
        .join(Seat)
        .where(
            Seat.user_id == current_user.id,
            Seat.is_active == True,
        )
    )
    orgs = result.scalars().all()
    
    return [
        {
            "id": str(o.id),
            "name": o.name,
            "slug": o.slug,
            "description": o.description,
            "role": next(
                (s.role.value for s in o.seats if s.user_id == current_user.id),
                None
            ),
            "member_count": len(o.seats),
        }
        for o in orgs
    ]


@router.get("/{organization_id}", response_model=dict)
async def get_organization(
    organization_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get organization details"""
    result = await db.execute(
        select(Organization).where(Organization.id == organization_id)
    )
    org = result.scalar_one_or_none()
    
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    
    # Check membership
    seat = next(
        (s for s in org.seats if s.user_id == current_user.id and s.is_active),
        None
    )
    if not seat and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Access denied")
    
    return {
        "id": str(org.id),
        "name": org.name,
        "slug": org.slug,
        "description": org.description,
        "settings": org.settings,
        "subscription": {
            "plan": org.subscription.plan_type.value if org.subscription else None,
            "status": org.subscription.status.value if org.subscription else None,
            "seats": org.subscription.seat_quantity if org.subscription else 0,
        } if org.subscription else None,
        "members": [
            {
                "id": str(s.user.id),
                "email": s.user.email,
                "name": f"{s.user.first_name} {s.user.last_name}",
                "role": s.role.value,
            }
            for s in org.seats if s.is_active
        ],
    }


@router.patch("/{organization_id}", response_model=dict)
async def update_organization(
    organization_id: str,
    data: OrganizationUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Update organization"""
    result = await db.execute(
        select(Organization).where(Organization.id == organization_id)
    )
    org = result.scalar_one_or_none()
    
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    
    # Check permissions (admin or higher)
    seat = next(
        (s for s in org.seats if s.user_id == current_user.id and s.is_active),
        None
    )
    if not seat or not can_administrate(seat.role):
        raise HTTPException(status_code=403, detail="Only administrators can update")
    
    if data.name:
        org.name = data.name
    if data.description is not None:
        org.description = data.description
    if data.settings:
        org.settings.update(data.settings)
    
    await db.commit()
    await db.refresh(org)
    
    return {
        "id": str(org.id),
        "name": org.name,
        "message": "Organization updated",
    }


@router.post("/{organization_id}/seats", response_model=dict)
async def add_seat(
    organization_id: str,
    data: SeatCreate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Add user to organization"""
    result = await db.execute(
        select(Organization).where(Organization.id == organization_id)
    )
    org = result.scalar_one_or_none()
    
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    
    # Check permissions
    seat = next(
        (s for s in org.seats if s.user_id == current_user.id and s.is_active),
        None
    )
    if not seat or not can_manage(seat.role):
        raise HTTPException(status_code=403, detail="Only managers and above can add members")
    
    # Check seat limit
    if org.subscription:
        current_seats = len([s for s in org.seats if s.is_active])
        if current_seats >= org.subscription.seat_quantity:
            raise HTTPException(status_code=400, detail="Seat limit reached")
    
    # Find user by email
    user_result = await db.execute(
        select(User).where(User.email == data.user_email)
    )
    user = user_result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Check if already member
    existing = next(
        (s for s in org.seats if s.user_id == user.id),
        None
    )
    if existing:
        raise HTTPException(status_code=400, detail="User is already a member")
    
    # Create seat
    new_seat = Seat(
        organization_id=org.id,
        user_id=user.id,
        role=UserRole(data.role.lower()),
        is_active=True,
    )
    db.add(new_seat)
    await db.commit()
    
    return {
        "message": f"Added {user.email} to organization",
        "seat_id": str(new_seat.id),
    }


@router.delete("/{organization_id}/seats/{user_id}")
async def remove_seat(
    organization_id: str,
    user_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove user from organization"""
    result = await db.execute(
        select(Seat).where(
            Seat.organization_id == organization_id,
            Seat.user_id == user_id,
        )
    )
    seat = result.scalar_one_or_none()
    
    if not seat:
        raise HTTPException(status_code=404, detail="Seat not found")
    
    # Check permissions
    current_seat = await db.execute(
        select(Seat).where(
            Seat.organization_id == organization_id,
            Seat.user_id == current_user.id,
        )
    )
    current_seat = current_seat.scalar_one_or_none()
    
    if not current_seat or not can_manage(current_seat.role):
        raise HTTPException(status_code=403, detail="Only managers and above can remove members")
    
    # Can't remove admin
    if seat.role == UserRole.ADMIN:
        raise HTTPException(status_code=400, detail="Cannot remove organization admin")
    
    seat.is_active = False
    await db.commit()
    
    return {"message": "Member removed"}
