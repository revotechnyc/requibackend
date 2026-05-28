"""
SaaS admin portal authentication (separate from customer /auth).
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.platform_admin_security import (
    create_platform_admin_access_token,
    get_current_platform_admin,
    platform_admin_to_dict,
    verify_platform_admin_password,
)
from app.db.database import get_db
from app.db.models import PlatformAdmin

router = APIRouter()


class PlatformAdminLoginRequest(BaseModel):
    email: EmailStr
    password: str


class PlatformAdminLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    admin: dict


@router.post("/login", response_model=PlatformAdminLoginResponse)
async def platform_admin_login(
    body: PlatformAdminLoginRequest,
    db: AsyncSession = Depends(get_db),
):
    email = body.email.strip().lower()
    result = await db.execute(select(PlatformAdmin).where(PlatformAdmin.email == email))
    admin = result.scalar_one_or_none()

    if (
        admin is None
        or not admin.is_active
        or not verify_platform_admin_password(body.password, admin.hashed_password)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

    admin.last_login = datetime.utcnow()
    await db.commit()
    await db.refresh(admin)

    token = create_platform_admin_access_token(str(admin.id))
    return PlatformAdminLoginResponse(
        access_token=token,
        admin=platform_admin_to_dict(admin),
    )


@router.get("/me")
async def platform_admin_me(
    admin: PlatformAdmin = Depends(get_current_platform_admin),
):
    return {"admin": platform_admin_to_dict(admin)}
