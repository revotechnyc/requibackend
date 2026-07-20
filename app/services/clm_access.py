"""CLM feature, role, and location-scope authorization helpers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.endpoints.auth import get_current_active_user
from app.core.permissions import PermissionChecker, require_feature_dependency
from app.db.database import get_db
from app.db.models import (
    ClmSubLocation,
    ClmUserLocationAccess,
    Organization,
    Seat,
    User,
    UserRole,
)
from app.services.workspace_permissions import member_can_access_feature


CLM_FEATURE = "clm"
ACCESS_LEVELS = frozenset(
    {"viewer", "compliance_officer", "facility_manager", "facility_owner"}
)
_ACCESS_RANK = {
    "viewer": 1,
    "compliance_officer": 2,
    "facility_manager": 3,
    "facility_owner": 4,
}
_ADMIN_ROLES = frozenset({UserRole.ENTERPRISE_ADMIN, UserRole.ADMIN})
require_clm_plan = require_feature_dependency(CLM_FEATURE)


@dataclass(frozen=True)
class ClmAccessContext:
    organization: Organization
    user: User
    seat: Seat
    # None means organization-wide. A dict means ACL mode is active and maps
    # accessible locations (including inherited descendants) to access level.
    location_permissions: Optional[dict[uuid.UUID, str]]

    @property
    def has_org_wide_access(self) -> bool:
        return self.location_permissions is None

    @property
    def can_manage_access(self) -> bool:
        return (
            self.user.id == self.organization.owner_id
            or self.seat.role in _ADMIN_ROLES
            or self.user.is_superuser
        )

    @property
    def can_create(self) -> bool:
        return PermissionChecker.has_permission(self.seat.role, "create")

    def can_access_location(self, location_id: Optional[uuid.UUID]) -> bool:
        if self.location_permissions is None:
            return True
        if location_id is None:
            return self.can_manage_access
        return location_id in self.location_permissions

    def can_write_location(self, location_id: Optional[uuid.UUID]) -> bool:
        if not self.can_create:
            return False
        if self.location_permissions is None:
            return True
        if location_id is None:
            return self.can_manage_access
        return _ACCESS_RANK.get(self.location_permissions.get(location_id, ""), 0) >= 2


async def _location_permissions(
    db: AsyncSession,
    organization: Organization,
    user: User,
    seat: Seat,
) -> Optional[dict[uuid.UUID, str]]:
    if (
        user.id == organization.owner_id
        or seat.role in _ADMIN_ROLES
        or user.is_superuser
    ):
        return None

    # Backward-compatible rollout: until an organization configures at least one
    # CLM assignment, existing eligible members keep organization-wide access.
    assignment_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(ClmUserLocationAccess)
                .where(ClmUserLocationAccess.organization_id == organization.id)
            )
        ).scalar_one()
        or 0
    )
    if assignment_count == 0:
        return None

    rows = (
        (
            await db.execute(
                select(ClmUserLocationAccess).where(
                    ClmUserLocationAccess.organization_id == organization.id,
                    ClmUserLocationAccess.user_id == user.id,
                )
            )
        )
        .scalars()
        .all()
    )
    direct = {
        row.sub_location_id: row.access_level
        if row.access_level in ACCESS_LEVELS
        else "viewer"
        for row in rows
    }
    if not direct:
        return {}

    locations = (
        (
            await db.execute(
                select(ClmSubLocation).where(
                    ClmSubLocation.organization_id == organization.id,
                    ClmSubLocation.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    children: dict[Optional[uuid.UUID], list[uuid.UUID]] = {}
    for location in locations:
        children.setdefault(location.parent_id, []).append(location.id)

    inherited = dict(direct)
    for root_id, level in direct.items():
        stack = list(children.get(root_id, []))
        while stack:
            child_id = stack.pop()
            existing = inherited.get(child_id)
            if existing is None or _ACCESS_RANK[level] > _ACCESS_RANK[existing]:
                inherited[child_id] = level
            stack.extend(children.get(child_id, []))
    return inherited


async def get_clm_access_context(
    organization: Organization = Depends(require_clm_plan),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ClmAccessContext:
    seat = (
        await db.execute(
            select(Seat).where(
                Seat.organization_id == organization.id,
                Seat.user_id == current_user.id,
                Seat.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if not seat:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No active organization membership",
        )
    plan = organization.subscription.plan_type
    if not member_can_access_feature(
        CLM_FEATURE, plan, seat.role, seat.feature_permissions
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CLM access is not enabled for this member",
        )
    return ClmAccessContext(
        organization=organization,
        user=current_user,
        seat=seat,
        location_permissions=await _location_permissions(
            db, organization, current_user, seat
        ),
    )


def require_clm_admin(context: ClmAccessContext) -> None:
    if not context.can_manage_access:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CLM administrator access required",
        )


def require_location_access(
    context: ClmAccessContext,
    location_id: Optional[uuid.UUID],
    *,
    write: bool = False,
) -> None:
    allowed = (
        context.can_write_location(location_id)
        if write
        else context.can_access_location(location_id)
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this CLM location",
        )
