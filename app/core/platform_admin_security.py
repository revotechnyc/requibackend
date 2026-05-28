"""
JWT and password utilities for the SaaS admin portal (separate from customer auth).
"""

from datetime import datetime, timedelta
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import get_db
from app.core.platform_admin_roles import PLATFORM_ROLE_LABELS
from app.db.models import PlatformAdmin

PLATFORM_ADMIN_TOKEN_TYPE = "platform_admin"

oauth2_platform_admin_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{settings.api_v1_prefix}/platform-admin/auth/login",
    auto_error=True,
)


def verify_platform_admin_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"),
            hashed_password.encode("utf-8"),
        )
    except (ValueError, TypeError):
        return False


def hash_platform_admin_password(password: str) -> str:
    return bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt(),
    ).decode("utf-8")


def create_platform_admin_access_token(
    admin_id: str,
    expires_delta: Optional[timedelta] = None,
) -> str:
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(
            minutes=settings.platform_admin_access_token_expire_minutes
        )
    payload = {
        "sub": admin_id,
        "exp": expire,
        "type": PLATFORM_ADMIN_TOKEN_TYPE,
    }
    return jwt.encode(
        payload,
        settings.platform_admin_jwt_secret_effective,
        algorithm=settings.jwt_algorithm,
    )


async def get_current_platform_admin(
    token: str = Depends(oauth2_platform_admin_scheme),
    db: AsyncSession = Depends(get_db),
) -> PlatformAdmin:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate admin credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(
            token,
            settings.platform_admin_jwt_secret_effective,
            algorithms=[settings.jwt_algorithm],
        )
        admin_id = payload.get("sub")
        token_type = payload.get("type")
        if admin_id is None or token_type != PLATFORM_ADMIN_TOKEN_TYPE:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    result = await db.execute(select(PlatformAdmin).where(PlatformAdmin.id == admin_id))
    admin = result.scalar_one_or_none()
    if admin is None or not admin.is_active:
        raise credentials_exception
    return admin


def platform_admin_to_dict(admin: PlatformAdmin) -> dict:
    return {
        "id": str(admin.id),
        "email": admin.email,
        "first_name": admin.first_name,
        "last_name": admin.last_name,
        "role": admin.role,
        "role_label": PLATFORM_ROLE_LABELS.get(admin.role, admin.role),
        "display_name": f"{admin.first_name} {admin.last_name}".strip() or admin.email,
        "is_super_admin": admin.role == "super_admin",
    }
