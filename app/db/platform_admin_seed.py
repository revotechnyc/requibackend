"""
Ensure the configured platform owner exists (SaaS admin portal).
"""

import logging

from sqlalchemy import select

from app.core.config import settings
from app.core.platform_admin_security import hash_platform_admin_password
from app.db.database import AsyncSessionLocal
from app.core.platform_admin_roles import PlatformAdminRole
from app.db.models import PlatformAdmin, User

logger = logging.getLogger(__name__)


async def ensure_platform_admin_seed() -> None:
    email = settings.platform_admin_seed_email.strip().lower()
    password = (settings.platform_admin_seed_password or "").strip()
    if not email or not password:
        logger.info(
            "Platform admin seed skipped (set PLATFORM_ADMIN_SEED_EMAIL and PLATFORM_ADMIN_SEED_PASSWORD)"
        )
        return

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(PlatformAdmin).where(PlatformAdmin.email == email))
        admin = result.scalar_one_or_none()
        hashed = hash_platform_admin_password(password)

        if admin is None:
            admin = PlatformAdmin(
                email=email,
                hashed_password=hashed,
                first_name=settings.platform_admin_seed_first_name,
                last_name=settings.platform_admin_seed_last_name,
                role=PlatformAdminRole.SUPER_ADMIN.value,  # noqa: same enum values as models
                is_active=True,
            )
            db.add(admin)
            logger.info("Created platform admin: %s", email)
        else:
            admin.hashed_password = hashed
            admin.first_name = settings.platform_admin_seed_first_name
            admin.last_name = settings.platform_admin_seed_last_name
            admin.role = PlatformAdminRole.SUPER_ADMIN.value
            admin.is_active = True
            logger.info("Updated platform admin credentials: %s", email)

        # Keep customer User.is_superuser in sync when the same email exists (legacy /admin routes).
        user_result = await db.execute(select(User).where(User.email == email))
        user = user_result.scalar_one_or_none()
        if user is not None:
            user.is_superuser = True

        await db.commit()
