"""
Platform admin — customer users and organizations (SaaS admin portal).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.platform_admin_security import get_current_platform_admin
from app.db.database import get_db
from app.db.models import (
    Organization,
    PlanType,
    PlatformAdmin,
    Seat,
    Subscription,
    SubscriptionStatus,
    User,
    WorkspaceInvitation,
    WorkspaceInvitationStatus,
)
from app.services.seat_allocation import _estimated_monthly_cents_for_org

router = APIRouter()

ROLE_LABELS = {
    "enterprise_admin": "Enterprise Admin",
    "admin": "Admin",
    "reviewer": "Reviewer",
    "approver": "Approver",
    "contributor": "Contributor",
    "analyst": "Analyst",
    "viewer": "Viewer",
    "seo": "SEO",
}

PLAN_LABELS = {
    "standard": "Standard",
    "pro": "Pro",
    "enterprise": "Enterprise",
}


def _require_super_admin(admin: PlatformAdmin) -> None:
    from app.core.platform_admin_roles import PlatformAdminRole

    if admin.role != PlatformAdminRole.SUPER_ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only Super Admin can view customer users and organizations",
        )


def _role_label(role: str) -> str:
    return ROLE_LABELS.get((role or "").lower(), (role or "—").replace("_", " ").title())


def _plan_label(plan_type: Optional[PlanType]) -> tuple[Optional[str], Optional[str]]:
    if not plan_type:
        return None, None
    key = plan_type.value
    return key, PLAN_LABELS.get(key, key.title())


def _org_status(subscription: Optional[Subscription]) -> str:
    if not subscription:
        return "inactive"
    raw = subscription.status.value if subscription.status else "inactive"
    if raw == SubscriptionStatus.TRIALING.value:
        return "trial"
    return raw


def _user_display_name(
    first_name: Optional[str],
    last_name: Optional[str],
    email: str,
) -> str:
    name = f"{first_name or ''} {last_name or ''}".strip()
    return name or email.split("@")[0]


def _format_date(dt) -> Optional[str]:
    if not dt:
        return None
    return dt.strftime("%Y-%m-%d")


async def _estimate_org_mrr_cents(org: Organization, db: AsyncSession) -> int:
    sub = org.subscription
    if not sub:
        return 0
    if sub.plan_type == PlanType.ENTERPRISE:
        return await _estimated_monthly_cents_for_org(org, db)
    price = settings.get_plan_price(sub.plan_type.value)
    seats = sub.seat_quantity or settings.get_plan_limits(sub.plan_type.value)["min"]
    return price * max(1, seats)


def _matches_search(*, q: str, name: str, email: str, org_name: str) -> bool:
    if not q:
        return True
    needle = q.lower()
    return (
        needle in name.lower()
        or needle in email.lower()
        or needle in org_name.lower()
    )


def _matches_plan(plan_key: Optional[str], plan_filter: str) -> bool:
    if plan_filter == "all":
        return True
    return (plan_key or "").lower() == plan_filter.lower()


@router.get("/users-and-orgs")
async def list_users_and_organizations(
    search: Optional[str] = Query(None, alias="q"),
    plan: str = Query("all"),
    admin: PlatformAdmin = Depends(get_current_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """List customer users, organizations, and summary stats for the admin portal."""
    _require_super_admin(admin)

    q = (search or "").strip()

    seat_result = await db.execute(
        select(Seat).options(
            selectinload(Seat.user),
            selectinload(Seat.organization).selectinload(Organization.subscription),
        )
    )
    seats = seat_result.scalars().all()

    inv_result = await db.execute(
        select(WorkspaceInvitation)
        .where(WorkspaceInvitation.status == WorkspaceInvitationStatus.PENDING.value)
        .options(
            selectinload(WorkspaceInvitation.organization).selectinload(
                Organization.subscription
            ),
        )
    )
    pending_invites = inv_result.scalars().all()

    active_emails: set[str] = set()
    users: list[dict] = []

    for seat in seats:
        user = seat.user
        org = seat.organization
        if not user or not org:
            continue

        email = user.email.lower()
        active_emails.add(email)
        name = _user_display_name(user.first_name, user.last_name, user.email)
        plan_key, plan_label = _plan_label(
            org.subscription.plan_type if org.subscription else None
        )

        if seat.is_active and user.is_active:
            row_status = "active"
        else:
            row_status = "suspended"

        if not _matches_search(q=q, name=name, email=user.email, org_name=org.name):
            continue
        if not _matches_plan(plan_key, plan):
            continue

        users.append(
            {
                "id": str(seat.id),
                "user_id": str(user.id),
                "name": name,
                "email": user.email,
                "org": org.name,
                "organization_id": str(org.id),
                "plan": plan_label,
                "plan_key": plan_key,
                "role": _role_label(
                    seat.role.value if hasattr(seat.role, "value") else str(seat.role)
                ),
                "status": row_status,
                "joined": _format_date(seat.created_at),
                "last_login": _format_date(user.last_login),
                "mfa": False,
            }
        )

    for inv in pending_invites:
        org = inv.organization
        if not org:
            continue
        email = inv.email.lower()
        if email in active_emails:
            continue

        name = _user_display_name(inv.first_name, inv.last_name, inv.email)
        plan_key, plan_label = _plan_label(
            org.subscription.plan_type if org.subscription else None
        )

        if not _matches_search(q=q, name=name, email=inv.email, org_name=org.name):
            continue
        if not _matches_plan(plan_key, plan):
            continue

        users.append(
            {
                "id": f"inv-{inv.id}",
                "user_id": None,
                "name": name,
                "email": inv.email,
                "org": org.name,
                "organization_id": str(org.id),
                "plan": plan_label,
                "plan_key": plan_key,
                "role": _role_label(inv.role),
                "status": "pending",
                "joined": _format_date(inv.created_at),
                "last_login": None,
                "mfa": False,
            }
        )

    users.sort(key=lambda row: (row["name"] or "").lower())

    org_result = await db.execute(
        select(Organization).options(
            selectinload(Organization.subscription),
            selectinload(Organization.seats),
            selectinload(Organization.owner),
        )
    )
    organizations: list[dict] = []
    for org in org_result.scalars().all():
        plan_key, plan_label = _plan_label(
            org.subscription.plan_type if org.subscription else None
        )
        if plan != "all" and (plan_key or "").lower() != plan.lower():
            continue
        owner_email = org.owner.email if org.owner else ""
        if q and not (
            q.lower() in org.name.lower()
            or q.lower() in owner_email.lower()
        ):
            continue

        active_members = len([s for s in org.seats if s.is_active])
        mrr_cents = await _estimate_org_mrr_cents(org, db)
        settings_json = org.settings if isinstance(org.settings, dict) else {}

        organizations.append(
            {
                "id": str(org.id),
                "name": org.name,
                "plan": plan_label,
                "plan_key": plan_key,
                "users": active_members,
                "mrr": mrr_cents / 100,
                "mrr_cents": mrr_cents,
                "status": _org_status(org.subscription),
                "industry": settings_json.get("industry") or "—",
                "state": settings_json.get("state") or "—",
                "created_at": _format_date(org.created_at),
            }
        )

    organizations.sort(key=lambda row: (row["name"] or "").lower())

    stats = {
        "total_users": len(users),
        "active": sum(1 for u in users if u["status"] == "active"),
        "pending": sum(1 for u in users if u["status"] == "pending"),
        "suspended": sum(1 for u in users if u["status"] == "suspended"),
        "total_organizations": len(organizations),
    }

    return {
        "stats": stats,
        "users": users,
        "organizations": organizations,
    }
