"""Persist UserRole as lowercase VARCHAR (avoids PG enum name/value mismatch)."""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.types import TypeDecorator


class UserRoleType(TypeDecorator):
    """Store role as lowercase string; expose as UserRole in Python."""

    impl = String(50)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        from app.db.models import UserRole

        if isinstance(value, UserRole):
            return value.value
        return str(value).lower()

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        from app.db.models import UserRole

        raw = str(value).strip()
        legacy = {
            "VIEWER": "viewer",
            "ADMIN": "admin",
            "SEO": "seo",
            "REVIEWER": "reviewer",
            "CONTRIBUTOR": "contributor",
        }
        normalized = legacy.get(raw, raw.lower())
        return UserRole(normalized)


def user_role_value(role) -> str:
    from app.db.models import UserRole

    if isinstance(role, UserRole):
        return role.value
    return str(role).lower()
