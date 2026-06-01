"""
Feature gating and permission system
Controls access based on subscription plan and user role
"""

from enum import Enum
from functools import wraps
from typing import Callable, Optional

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import get_db
from app.db.models import (
    Organization,
    PlanType,
    Seat,
    Subscription,
    SubscriptionStatus,
    User,
    UserRole,
)


# ==========================
# ROLE HIERARCHY
# ==========================

ROLE_HIERARCHY = {
    UserRole.ADMIN: 100,
    UserRole.APPROVER: 75,
    UserRole.REVIEWER: 70,
    UserRole.CONTRIBUTOR: 50,
    UserRole.PRESIDENT: 90,
    UserRole.VICE_PRESIDENT: 80,
    UserRole.DIRECTOR: 70,
    UserRole.MANAGER: 60,
    UserRole.TEAM_LEAD: 50,
    UserRole.SPECIALIST: 40,
    UserRole.SEO: 35,
    UserRole.ASSISTANT: 30,
    UserRole.VIEWER: 20,
}

# ==========================
# FEATURE KEYS (API / dependencies)
# ==========================

class Feature(str, Enum):
    """Feature keys used by endpoint dependencies (values match PLAN_FEATURES)."""
    AI_QA = "intelligence"
    KNOWLEDGE_STORAGE = "documents"
    GAP_DETECTION = "compliance"
    GAP_RESOLUTION = "compliance"
    CUSTOM_SOURCES = "integrations"
    AUDIT_LOGS = "audit_logs"
    ADMIN_DASHBOARD = "admin"


# ==========================
# PLAN FEATURE ACCESS
# ==========================

# Frontend feature keys mapped to plan availability
PLAN_FEATURES = {
    PlanType.STANDARD: [
        "intelligence",
        "documents",
        "blog",
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
        "blog",
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
        "blog",
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
        "change_role", "view_audit", "publish_blog", "manage_blog",
    ],
    UserRole.REVIEWER: [
        "view", "create", "edit", "manage", "approve",
        "reports", "view_audit", "publish_blog",
    ],
    UserRole.APPROVER: [
        "view", "approve", "reports", "view_audit",
    ],
    UserRole.CONTRIBUTOR: [
        "view", "create", "edit", "submit",
        "work_modules", "view_assigned", "publish_blog",
    ],
    UserRole.PRESIDENT: [
        "view", "create", "edit", "manage", "approve",
        "reports", "view_all_teams", "view_audit", "publish_blog", "manage_blog",
    ],
    UserRole.VICE_PRESIDENT: [
        "view", "create", "edit", "manage", "reports",
        "view_department", "approve_workflow", "publish_blog", "manage_blog",
    ],
    UserRole.DIRECTOR: [
        "view", "create", "edit", "manage", "assign",
        "view_team", "review_performance", "publish_blog", "manage_blog",
    ],
    UserRole.MANAGER: [
        "view", "create", "edit", "assign",
        "view_team_activity", "operational_reports", "publish_blog", "manage_blog",
    ],
    UserRole.TEAM_LEAD: [
        "view", "create", "edit", "coordinate",
        "view_limited_activity", "submit_recommendations", "publish_blog",
    ],
    UserRole.SPECIALIST: [
        "view", "create", "submit",
        "work_modules", "view_assigned", "publish_blog",
    ],
    UserRole.SEO: [
        "view", "create", "edit", "delete", "publish_blog", "manage_blog",
    ],
    UserRole.ASSISTANT: [
        "view", "create", "assist",
        "enter_data", "view_assigned", "suggest",
    ],
    UserRole.VIEWER: ["view", "read_only"],
}


class FeatureGate:
    """Feature gating service"""
    
    @staticmethod
    def has_feature(plan_type: PlanType, feature_key: str) -> bool:
        """Check if a plan has access to a feature"""
        allowed = PLAN_FEATURES.get(plan_type, [])
        return feature_key in allowed
    
    @staticmethod
    def require_feature(feature_key: str):
        """Decorator to require a feature for an endpoint"""
        def decorator(func: Callable):
            @wraps(func)
            async def wrapper(*args, **kwargs):
                org = kwargs.get('organization')
                if not org:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Organization context required"
                    )
                
                subscription = getattr(org, 'subscription', None)
                if not subscription or subscription.status not in [
                    SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING
                ]:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Active subscription required"
                    )
                
                if not FeatureGate.has_feature(subscription.plan_type, feature_key):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=f"Feature '{feature_key}' not available on your plan"
                    )
                
                return await func(*args, **kwargs)
            return wrapper
        return decorator


class PermissionChecker:
    """Permission checking service"""
    
    @staticmethod
    def has_permission(user_role: UserRole, permission: str) -> bool:
        """Check if a role has a specific permission"""
        permissions = ROLE_PERMISSIONS.get(user_role, [])
        return permission in permissions or "admin" in permissions
    
    @staticmethod
    def role_level(user_role: UserRole) -> int:
        """Get numeric hierarchy level for a role"""
        return ROLE_HIERARCHY.get(user_role, 0)
    
    @staticmethod
    def can_administrate(user_role: UserRole) -> bool:
        """Admin, President, VP — billing and org administration."""
        return ROLE_HIERARCHY.get(user_role, 0) >= ROLE_HIERARCHY.get(UserRole.VICE_PRESIDENT, 80)

    @staticmethod
    def can_manage_role(manager_role: UserRole, target_role: UserRole) -> bool:
        """Check if manager_role can manage target_role"""
        if manager_role == UserRole.ADMIN:
            return True
        return ROLE_HIERARCHY.get(manager_role, 0) > ROLE_HIERARCHY.get(target_role, 0)
    
    @staticmethod
    def get_assignable_roles(user_role: UserRole) -> list[UserRole]:
        """Get roles that user_role can assign to others"""
        user_level = ROLE_HIERARCHY.get(user_role, 0)
        return [role for role, level in ROLE_HIERARCHY.items() if level < user_level]
    
    @staticmethod
    def require_permission(permission: str):
        """Dependency to require a permission"""
        async def checker(
            current_user: User = Depends(get_current_user),
            db: AsyncSession = Depends(get_db),
        ) -> User:
            from sqlalchemy import select
            
            result = await db.execute(
                select(Seat).where(
                    Seat.user_id == current_user.id,
                    Seat.is_active == True
                )
            )
            seat = result.scalar_one_or_none()
            
            if not seat:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Not a member of this organization"
                )
            
            if not PermissionChecker.has_permission(seat.role, permission):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Permission '{permission}' required"
                )
            
            return current_user
        return checker


# Import here to avoid circular dependency
from app.api.endpoints.auth import get_current_user
from sqlalchemy import select
from sqlalchemy.orm import selectinload


async def check_organization_access(
    user: User,
    organization_id: str,
    db: AsyncSession,
    require_active_subscription: bool = True,
) -> Organization:
    """Check if user has access to organization"""
    
    result = await db.execute(
        select(Organization)
        .where(Organization.id == organization_id)
        .options(
            selectinload(Organization.subscription),
            selectinload(Organization.seats),
        )
    )
    org = result.scalar_one_or_none()
    
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found"
        )
    
    # Check if user is member
    is_member = any(
        seat.user_id == user.id and seat.is_active
        for seat in org.seats
    )
    
    if not is_member and not user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied"
        )
    
    # Check subscription if required
    if require_active_subscription:
        if not org.subscription or org.subscription.status not in [
            SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING
        ]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Active subscription required"
            )
    
    return org


async def check_feature_access(
    organization: Organization,
    feature_key: str,
) -> bool:
    """Check if organization has access to a feature"""
    if not organization.subscription:
        return False
    
    if organization.subscription.status not in [
        SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING
    ]:
        return False
    
    return FeatureGate.has_feature(
        organization.subscription.plan_type,
        feature_key
    )


async def get_user_role_in_org(
    user_id: str,
    organization_id: str,
    db: AsyncSession,
) -> Optional[UserRole]:
    """Get user's role in an organization"""
    result = await db.execute(
        select(Seat).where(
            Seat.user_id == user_id,
            Seat.organization_id == organization_id,
            Seat.is_active == True,
        )
    )
    seat = result.scalar_one_or_none()
    return seat.role if seat else None


def require_feature_dependency(feature_key: str):
    """FastAPI dependency factory: require a plan feature for the user's org."""

    async def _require_feature(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> Organization:
        key = feature_key.value if isinstance(feature_key, Feature) else feature_key
        result = await db.execute(
            select(Seat)
            .where(
                Seat.user_id == current_user.id,
                Seat.is_active == True,
            )
            .options(
                selectinload(Seat.organization).selectinload(Organization.subscription)
            )
        )
        seat = result.scalar_one_or_none()

        if not seat:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No active organization membership",
            )

        org = seat.organization

        if not await check_feature_access(org, key):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Feature '{key}' not available on your plan",
            )

        return org

    return _require_feature
