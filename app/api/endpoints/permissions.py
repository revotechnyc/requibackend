"""
Permissions and feature access endpoints
Server-side enforcement of plan-based feature gating and role permissions
"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.endpoints.auth import get_current_active_user
from app.core.permissions import (
    FeatureGate,
    PermissionChecker,
    PLAN_FEATURES,
    ROLE_PERMISSIONS,
    ROLE_HIERARCHY,
)
from app.db.database import get_db
from app.db.models import Organization, PlanType, Seat, SubscriptionStatus, User, UserRole

router = APIRouter()


# Pydantic models
class FeatureCheckRequest(BaseModel):
    feature_key: str


class PermissionCheckRequest(BaseModel):
    permission: str


class RoleInfoOut(BaseModel):
    role: str
    level: int
    permissions: List[str]
    assignable_roles: List[str]


# ==========================
# FEATURE ACCESS ENDPOINTS
# ==========================

@router.post("/check-feature", response_model=dict)
async def check_feature(
    data: FeatureCheckRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Check if user's plan allows a specific feature"""
    # Get user's primary organization
    result = await db.execute(
        select(Seat)
        .where(Seat.user_id == current_user.id, Seat.is_active == True)
        .options(selectinload(Seat.organization).selectinload(Organization.subscription))
    )
    seat = result.scalar_one_or_none()
    
    if not seat:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No active organization"
        )
    
    org = seat.organization
    plan_type = org.subscription.plan_type if org.subscription else PlanType.STANDARD
    
    has_access = FeatureGate.has_feature(plan_type, data.feature_key)
    
    return {
        "feature_key": data.feature_key,
        "has_access": has_access,
        "plan": plan_type.value,
        "upgrade_required": not has_access,
        "available_on": [
            plan.value
            for plan in PlanType
            if data.feature_key in PLAN_FEATURES.get(plan, [])
        ],
    }


@router.get("/my-features", response_model=dict)
async def list_my_features(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """List all features available to the current user based on plan"""
    result = await db.execute(
        select(Seat)
        .where(Seat.user_id == current_user.id, Seat.is_active == True)
        .options(selectinload(Seat.organization).selectinload(Organization.subscription))
    )
    seat = result.scalar_one_or_none()
    
    if not seat:
        return {"plan": "standard", "features": PLAN_FEATURES[PlanType.STANDARD]}
    
    org = seat.organization
    plan_type = org.subscription.plan_type if org.subscription else PlanType.STANDARD
    
    features = PLAN_FEATURES.get(plan_type, [])
    
    return {
        "plan": plan_type.value,
        "subscription_status": org.subscription.status.value if org.subscription else None,
        "features": features,
        "locked_features": [
            f for f in [
                "intelligence", "dashboard", "tasks", "compliance", "calendar",
                "documents", "news", "teams", "integrations", "settings", "admin"
            ]
            if f not in features
        ],
    }


# ==========================
# ROLE PERMISSION ENDPOINTS
# ==========================

@router.get("/my-role", response_model=dict)
async def get_my_role(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current user's role info with permissions"""
    result = await db.execute(
        select(Seat)
        .where(Seat.user_id == current_user.id, Seat.is_active == True)
        .options(selectinload(Seat.organization))
    )
    seat = result.scalar_one_or_none()
    
    if not seat:
        return {
            "role": "viewer",
            "level": ROLE_HIERARCHY[UserRole.VIEWER],
            "permissions": ROLE_PERMISSIONS[UserRole.VIEWER],
            "assignable_roles": [],
        }
    
    role = seat.role
    assignable = PermissionChecker.get_assignable_roles(role)
    
    return {
        "role": role.value,
        "level": ROLE_HIERARCHY[role],
        "permissions": ROLE_PERMISSIONS.get(role, []),
        "assignable_roles": [r.value for r in assignable],
        "is_admin": role in (UserRole.ADMIN, UserRole.ENTERPRISE_ADMIN),
        "can_manage": PermissionChecker.can_manage_role(role, UserRole.VIEWER),
    }


@router.post("/check-permission", response_model=dict)
async def check_permission(
    data: PermissionCheckRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Check if user has a specific permission"""
    result = await db.execute(
        select(Seat)
        .where(Seat.user_id == current_user.id, Seat.is_active == True)
    )
    seat = result.scalar_one_or_none()
    
    if not seat:
        has_perm = False
        role = "viewer"
    else:
        has_perm = PermissionChecker.has_permission(seat.role, data.permission)
        role = seat.role.value
    
    return {
        "permission": data.permission,
        "has_permission": has_perm,
        "role": role,
    }


# ==========================
# ROLE INFO ENDPOINTS
# ==========================

@router.get("/roles", response_model=dict)
async def list_roles():
    """List all available roles with their hierarchy and permissions"""
    roles = []
    for role in UserRole:
        roles.append({
            "role": role.value,
            "level": ROLE_HIERARCHY[role],
            "permissions": ROLE_PERMISSIONS.get(role, []),
            "assignable_by": [
                r.value for r in PermissionChecker.get_assignable_roles(role)
            ],
        })
    
    return {"roles": roles}


@router.get("/plans", response_model=dict)
async def list_plan_features():
    """List all plans with their available features"""
    return {
        "plans": {
            plan.value: {
                "features": features,
                "feature_count": len(features),
            }
            for plan, features in PLAN_FEATURES.items()
        }
    }
