"""Paid seat counting and subscription billing sync for team invites."""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import (
    Organization,
    PlanType,
    Seat,
    Subscription,
    UserRole,
    WorkspaceInvitation,
    WorkspaceInvitationStatus,
)
from app.services.billing import BillingService
from app.services.enterprise_roles import (
    PAID_INVITE_ROLES,
    ROLE_SEAT_PRICE_CENTS,
    STANDARD_ENTERPRISE_SEAT_CENTS,
    seat_price_cents_for_member,
)

PAID_ROLES = frozenset(
    UserRole(r) for r in PAID_INVITE_ROLES if r != UserRole.VIEWER.value
) | frozenset({UserRole.SEO, UserRole.ENTERPRISE_ADMIN})


def is_paid_role(role: UserRole | str) -> bool:
    if isinstance(role, UserRole):
        return role in PAID_ROLES
    try:
        return UserRole(str(role).lower()) in PAID_ROLES
    except ValueError:
        return False


async def count_paid_seats_allocated(
    organization_id: UUID,
    db: AsyncSession,
    *,
    exclude_invitation_id: Optional[UUID] = None,
) -> int:
    """Active paid seats + pending paid invitations (each reserves a billed seat)."""
    seat_count = await db.scalar(
        select(func.count())
        .select_from(Seat)
        .where(
            Seat.organization_id == organization_id,
            Seat.is_active == True,
            Seat.role.in_(tuple(PAID_ROLES)),
        )
    )

    inv_query = select(func.count()).select_from(WorkspaceInvitation).where(
        WorkspaceInvitation.organization_id == organization_id,
        WorkspaceInvitation.status == WorkspaceInvitationStatus.PENDING.value,
        WorkspaceInvitation.role.in_([r.value for r in PAID_ROLES]),
    )
    if exclude_invitation_id:
        inv_query = inv_query.where(WorkspaceInvitation.id != exclude_invitation_id)
    inv_count = await db.scalar(inv_query)

    return int(seat_count or 0) + int(inv_count or 0)


async def _set_subscription_seat_quantity(
    db: AsyncSession,
    subscription: Subscription,
    new_quantity: int,
) -> Subscription:
    limits = settings.get_plan_limits(subscription.plan_type.value)
    if new_quantity < limits["min"] or new_quantity > limits["max"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Seat quantity must be between {limits['min']} and {limits['max']}",
        )

    stripe_id = subscription.stripe_subscription_id or ""
    if BillingService._is_stripe_billed_subscription(stripe_id):
        if subscription.plan_type == PlanType.ENTERPRISE:
            return await BillingService.update_enterprise_total_seats(
                db, subscription, new_quantity
            )
        return await BillingService.update_subscription(
            db, subscription, new_seat_quantity=new_quantity
        )

    subscription.seat_quantity = new_quantity
    await db.flush()
    return subscription


async def reserve_paid_seat(
    org: Organization,
    db: AsyncSession,
) -> dict:
    """
    Reserve one paid seat for a new paid-role invite.
    Increments Stripe/local subscription when purchased capacity is exceeded.
    """
    sub = org.subscription
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Active subscription required to allocate paid seats",
        )

    allocated = await count_paid_seats_allocated(org.id, db)
    needed = allocated + 1
    previous = sub.seat_quantity

    if needed > previous:
        await _set_subscription_seat_quantity(db, sub, needed)
        await db.refresh(sub)
        return {
            "seat_added": True,
            "previous_seat_quantity": previous,
            "new_seat_quantity": sub.seat_quantity,
            "allocated_paid_seats": needed,
        }

    return {
        "seat_added": False,
        "previous_seat_quantity": previous,
        "new_seat_quantity": previous,
        "allocated_paid_seats": needed,
    }


async def release_paid_seat_if_unused(
    org: Organization,
    db: AsyncSession,
) -> Optional[dict]:
    """
    After revoking a paid invite or demoting to viewer, lower billed seats
    when capacity exceeds allocation (never below plan minimum).
    """
    sub = org.subscription
    if not sub:
        return None

    allocated = await count_paid_seats_allocated(org.id, db)
    limits = settings.get_plan_limits(sub.plan_type.value)
    target = max(allocated, limits["min"])
    previous = sub.seat_quantity

    if target >= previous:
        return {
            "seat_removed": False,
            "previous_seat_quantity": previous,
            "new_seat_quantity": previous,
            "allocated_paid_seats": allocated,
        }

    await _set_subscription_seat_quantity(db, sub, target)
    await db.refresh(sub)
    return {
        "seat_removed": True,
        "previous_seat_quantity": previous,
        "new_seat_quantity": sub.seat_quantity,
        "allocated_paid_seats": allocated,
    }


async def _estimated_monthly_cents_for_org(
    org: Organization,
    db: AsyncSession,
) -> int:
    """Sum per-member seat prices (owner admin = $3,500, other paid = $500)."""
    owner_id = org.owner_id
    total = 0

    seat_result = await db.execute(
        select(Seat).where(
            Seat.organization_id == org.id,
            Seat.is_active == True,
            Seat.role.in_(tuple(PAID_ROLES)),
        )
    )
    for seat in seat_result.scalars().all():
        total += seat_price_cents_for_member(
            seat.role, user_id=seat.user_id, owner_id=owner_id
        )

    inv_result = await db.execute(
        select(WorkspaceInvitation).where(
            WorkspaceInvitation.organization_id == org.id,
            WorkspaceInvitation.status == WorkspaceInvitationStatus.PENDING.value,
            WorkspaceInvitation.role.in_([r.value for r in PAID_ROLES]),
        )
    )
    for inv in inv_result.scalars().all():
        try:
            UserRole(str(inv.role).lower())
        except ValueError:
            continue
        total += STANDARD_ENTERPRISE_SEAT_CENTS

    return total


def seat_billing_summary(
    subscription: Optional[Subscription],
    allocated_paid_seats: int,
    plan: PlanType,
    *,
    estimated_monthly_cents: int = 0,
) -> dict:
    billed = subscription.seat_quantity if subscription else 0
    default_cents = settings.get_plan_price(plan.value)
    return {
        "seat_quantity": billed,
        "allocated_paid_seats": allocated_paid_seats,
        "available_paid_seats": max(0, billed - allocated_paid_seats),
        "price_per_seat_cents": default_cents,
        "price_per_seat_display": default_cents / 100,
        "standard_seat_price_cents": STANDARD_ENTERPRISE_SEAT_CENTS,
        "standard_seat_price_display": STANDARD_ENTERPRISE_SEAT_CENTS / 100,
        "enterprise_owner_seat_price_cents": ROLE_SEAT_PRICE_CENTS["enterprise_admin"],
        "enterprise_owner_seat_price_display": ROLE_SEAT_PRICE_CENTS["enterprise_admin"] / 100,
        "estimated_monthly_cents": estimated_monthly_cents,
        "estimated_monthly_display": estimated_monthly_cents / 100,
    }


async def billing_snapshot_for_org(
    org: Organization,
    db: AsyncSession,
) -> dict:
    plan = (
        org.subscription.plan_type
        if org.subscription
        else PlanType.STANDARD
    )
    allocated = await count_paid_seats_allocated(org.id, db)
    estimated = await _estimated_monthly_cents_for_org(org, db)
    return seat_billing_summary(
        org.subscription, allocated, plan, estimated_monthly_cents=estimated
    )
