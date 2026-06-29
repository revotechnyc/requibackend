"""Password-change requirement flags (separate table — avoids ALTER on users)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UserPasswordFlag


async def user_must_change_password(user_id: UUID, db: AsyncSession) -> bool:
    result = await db.execute(
        select(UserPasswordFlag.must_change_password).where(
            UserPasswordFlag.user_id == user_id
        )
    )
    value = result.scalar_one_or_none()
    return bool(value)


async def users_requiring_password_change(
    user_ids: list[UUID],
    db: AsyncSession,
) -> set[UUID]:
    if not user_ids:
        return set()
    result = await db.execute(
        select(UserPasswordFlag.user_id).where(
            UserPasswordFlag.user_id.in_(user_ids),
            UserPasswordFlag.must_change_password.is_(True),
        )
    )
    return set(result.scalars().all())


async def set_user_must_change_password(
    user_id: UUID,
    required: bool,
    db: AsyncSession,
) -> None:
    if not required:
        await clear_user_must_change_password(user_id, db)
        return
    result = await db.execute(
        select(UserPasswordFlag).where(UserPasswordFlag.user_id == user_id)
    )
    row = result.scalar_one_or_none()
    if row:
        row.must_change_password = True
        return
    db.add(UserPasswordFlag(user_id=user_id, must_change_password=True))


async def clear_user_must_change_password(user_id: UUID, db: AsyncSession) -> None:
    await db.execute(
        UserPasswordFlag.__table__.delete().where(UserPasswordFlag.user_id == user_id)
    )
