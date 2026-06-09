"""Enterprise team role catalog — labels, seat pricing, invite rules."""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from app.db.models import PlanType, UserRole

# Seat prices in cents (client framework)
ENTERPRISE_OWNER_SEAT_CENTS = 350_000  # $3,500 — Enterprise Admin seat
STANDARD_ENTERPRISE_SEAT_CENTS = 50_000  # $500 — other paid roles

ROLE_SEAT_PRICE_CENTS: dict[str, int] = {
    UserRole.ENTERPRISE_ADMIN.value: ENTERPRISE_OWNER_SEAT_CENTS,
    "enterprise_admin": ENTERPRISE_OWNER_SEAT_CENTS,
    UserRole.ADMIN.value: STANDARD_ENTERPRISE_SEAT_CENTS,
    UserRole.APPROVER.value: STANDARD_ENTERPRISE_SEAT_CENTS,
    UserRole.REVIEWER.value: STANDARD_ENTERPRISE_SEAT_CENTS,
    UserRole.CONTRIBUTOR.value: STANDARD_ENTERPRISE_SEAT_CENTS,
    UserRole.ANALYST.value: STANDARD_ENTERPRISE_SEAT_CENTS,
    UserRole.SEO.value: STANDARD_ENTERPRISE_SEAT_CENTS,
    UserRole.VIEWER.value: 0,
}

# Roles Enterprise workspace admins may assign when inviting
# Enterprise Admin is signup-only (account owner) — not invitable.
INVITEABLE_ENTERPRISE_ROLES: tuple[str, ...] = (
    UserRole.ADMIN.value,
    UserRole.APPROVER.value,
    UserRole.REVIEWER.value,
    UserRole.CONTRIBUTOR.value,
    UserRole.ANALYST.value,
    UserRole.VIEWER.value,
)

PAID_INVITE_ROLES: frozenset[str] = frozenset(
    r for r in INVITEABLE_ENTERPRISE_ROLES if r != UserRole.VIEWER.value
)

WORKSPACE_ADMIN_ROLES: frozenset[UserRole] = frozenset({
    UserRole.ADMIN,
    UserRole.ENTERPRISE_ADMIN,
})

FULL_ACCESS_ROLES: frozenset[UserRole] = frozenset({
    UserRole.ADMIN,
    UserRole.ENTERPRISE_ADMIN,
})

ROLE_DISPLAY: dict[str, dict] = {
    UserRole.ENTERPRISE_ADMIN.value: {
        "label": "Enterprise Admin",
        "description": "Account owner at signup — billing, teams, security, and full platform access.",
        "price_cents": ENTERPRISE_OWNER_SEAT_CENTS,
        "invitable": False,
    },
    UserRole.ADMIN.value: {
        "label": "Admin",
        "description": "Department management, workflows, tasks, and team oversight.",
        "price_cents": STANDARD_ENTERPRISE_SEAT_CENTS,
        "invitable": True,
    },
    UserRole.APPROVER.value: {
        "label": "Approver",
        "description": "Review and approve compliance actions, workflows, and reports.",
        "price_cents": STANDARD_ENTERPRISE_SEAT_CENTS,
        "invitable": True,
    },
    UserRole.REVIEWER.value: {
        "label": "Reviewer",
        "description": "Audit documentation, comment on records, and analyze reports.",
        "price_cents": STANDARD_ENTERPRISE_SEAT_CENTS,
        "invitable": True,
    },
    UserRole.CONTRIBUTOR.value: {
        "label": "Contributor",
        "description": "Create records, upload documents, and complete assigned tasks.",
        "price_cents": STANDARD_ENTERPRISE_SEAT_CENTS,
        "invitable": True,
    },
    UserRole.ANALYST.value: {
        "label": "Analyst",
        "description": "Reporting, analytics, KPI dashboards, and trend analysis.",
        "price_cents": STANDARD_ENTERPRISE_SEAT_CENTS,
        "invitable": True,
    },
    UserRole.VIEWER.value: {
        "label": "Viewer",
        "description": "Read-only access to assigned dashboards and reports.",
        "price_cents": 0,
        "invitable": True,
    },
}


def owner_role_for_plan(plan: PlanType) -> UserRole:
    """Role for the account owner created at signup."""
    if plan == PlanType.ENTERPRISE:
        return UserRole.ENTERPRISE_ADMIN
    return UserRole.ADMIN


def is_workspace_admin(role: UserRole | str) -> bool:
    if isinstance(role, UserRole):
        return role in WORKSPACE_ADMIN_ROLES
    try:
        return UserRole(str(role).lower()) in WORKSPACE_ADMIN_ROLES
    except ValueError:
        return False


def has_full_access(role: UserRole | str) -> bool:
    if isinstance(role, UserRole):
        return role in FULL_ACCESS_ROLES
    try:
        return UserRole(str(role).lower()) in FULL_ACCESS_ROLES
    except ValueError:
        return False


def can_assign_enterprise_admin(
    inviter_role: UserRole,
    inviter_user_id: UUID,
    owner_id: UUID,
) -> bool:
    """Only account owner or existing Enterprise Admins may invite Enterprise Admin."""
    if inviter_user_id == owner_id:
        return True
    return inviter_role == UserRole.ENTERPRISE_ADMIN


def is_paid_invite_role(role: UserRole | str) -> bool:
    value = role.value if isinstance(role, UserRole) else str(role).lower()
    return value in PAID_INVITE_ROLES


def seat_price_cents_for_member(
    role: UserRole | str,
    *,
    user_id: Optional[UUID] = None,
    owner_id: Optional[UUID] = None,
) -> int:
    value = role.value if isinstance(role, UserRole) else str(role).lower()
    if value == UserRole.VIEWER.value:
        return 0
    # Only the account owner is billed at $3,500; all other paid users are $500.
    if user_id is not None and owner_id is not None and user_id == owner_id:
        return ENTERPRISE_OWNER_SEAT_CENTS
    return STANDARD_ENTERPRISE_SEAT_CENTS


def display_role_key(
    role: UserRole | str,
    *,
    user_id: Optional[UUID] = None,
    owner_id: Optional[UUID] = None,
) -> str:
    value = role.value if isinstance(role, UserRole) else str(role).lower()
    if value == UserRole.ENTERPRISE_ADMIN.value:
        return UserRole.ENTERPRISE_ADMIN.value
    if (
        value == UserRole.ADMIN.value
        and user_id is not None
        and owner_id is not None
        and user_id == owner_id
    ):
        return UserRole.ENTERPRISE_ADMIN.value
    return value


def role_catalog_for_api() -> list[dict]:
    """Invite picker + reference for frontend."""
    items = []
    for role_id in INVITEABLE_ENTERPRISE_ROLES:
        meta = ROLE_DISPLAY[role_id]
        items.append(
            {
                "id": role_id,
                "label": meta["label"],
                "description": meta["description"],
                "price_cents": meta["price_cents"],
                "price_display": meta["price_cents"] / 100,
                "is_paid": meta["price_cents"] > 0,
                "invitable": meta["invitable"],
            }
        )
    return items


def role_display_payload(
    role: UserRole | str,
    *,
    user_id: Optional[UUID] = None,
    owner_id: Optional[UUID] = None,
) -> dict:
    key = display_role_key(role, user_id=user_id, owner_id=owner_id)
    meta = ROLE_DISPLAY.get(key) or ROLE_DISPLAY.get(
        role.value if isinstance(role, UserRole) else str(role).lower(),
        {"label": str(role), "description": "", "price_cents": 0},
    )
    price = seat_price_cents_for_member(role, user_id=user_id, owner_id=owner_id)
    is_account_owner = bool(
        user_id is not None and owner_id is not None and user_id == owner_id
    )
    return {
        "role": role.value if isinstance(role, UserRole) else str(role).lower(),
        "display_role": key,
        "label": meta["label"],
        "description": meta.get("description", ""),
        "price_cents": price,
        "price_display": price / 100,
        "is_paid": price > 0,
        "is_account_owner": is_account_owner,
    }
