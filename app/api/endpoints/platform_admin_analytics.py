"""Platform admin — Analytics dashboard metrics."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.platform_admin_security import get_current_platform_admin
from app.db.database import get_db
from app.db.models import PlatformAdmin
from app.services.platform_admin_metrics import build_analytics_payload

router = APIRouter()


def _require_super_admin(admin: PlatformAdmin) -> None:
    from app.core.platform_admin_roles import PlatformAdminRole

    if admin.role != PlatformAdminRole.SUPER_ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only Super Admin can view platform analytics",
        )


@router.get("")
async def get_platform_analytics(
    admin: PlatformAdmin = Depends(get_current_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    _require_super_admin(admin)
    return await build_analytics_payload(db)
