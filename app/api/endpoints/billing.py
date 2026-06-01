"""
Billing endpoints with Stripe integration
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.endpoints.auth import get_current_active_user
from app.core.config import settings
from app.core.permissions import PermissionChecker, check_feature_access, require_feature_dependency
from app.db.database import get_db
from app.db.models import Organization, PlanType, Seat, Subscription, SubscriptionStatus, User
from app.services.billing import BillingService

router = APIRouter()
public_router = APIRouter()
logger = logging.getLogger(__name__)


# Pydantic models
class SubscriptionCreate(BaseModel):
    plan_type: str  # standard ($500), pro ($1,500), enterprise ($3,500)
    seat_quantity: int
    payment_method_id: Optional[str] = None


class SubscriptionUpdate(BaseModel):
    plan_type: Optional[str] = None
    seat_quantity: Optional[int] = None


class CheckoutSessionCreate(BaseModel):
    plan_type: str
    seat_quantity: int
    success_url: str
    cancel_url: str


@router.post("/subscriptions")
async def create_subscription(
    data: SubscriptionCreate,
    organization_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Create new subscription"""
    # Get organization
    result = await db.execute(
        select(Organization).where(Organization.id == organization_id)
    )
    org = result.scalar_one_or_none()
    
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    
    # Check if user is admin or higher
    seat_result = await db.execute(
        select(Seat).where(
            Seat.organization_id == organization_id,
            Seat.user_id == current_user.id,
        )
    )
    seat = seat_result.scalar_one_or_none()
    
    if not seat or not PermissionChecker.can_administrate(seat.role):
        raise HTTPException(status_code=403, detail="Only administrators can manage billing")
    
    # Check if subscription already exists
    if org.subscription and org.subscription.is_active():
        raise HTTPException(status_code=400, detail="Active subscription already exists")
    
    # Parse plan type
    try:
        plan_type = PlanType(data.plan_type.lower())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid plan type")
    
    # Create subscription
    subscription = await BillingService.create_subscription(
        db, org, plan_type, data.seat_quantity, data.payment_method_id
    )
    
    return {
        "subscription_id": str(subscription.id),
        "stripe_subscription_id": subscription.stripe_subscription_id,
        "status": subscription.status.value,
        "client_secret": None,  # Would be populated from Stripe
    }


@router.patch("/subscriptions/{subscription_id}")
async def update_subscription(
    subscription_id: str,
    data: SubscriptionUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Update subscription (change plan or seats)"""
    # Get subscription
    result = await db.execute(
        select(Subscription).where(Subscription.id == subscription_id)
    )
    subscription = result.scalar_one_or_none()
    
    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")
    
    # Check permissions
    org_result = await db.execute(
        select(Organization).where(Organization.id == subscription.organization_id)
    )
    org = org_result.scalar_one()
    
    seat_result = await db.execute(
        select(Seat).where(
            Seat.organization_id == org.id,
            Seat.user_id == current_user.id,
        )
    )
    seat = seat_result.scalar_one_or_none()
    
    if not seat or not PermissionChecker.can_administrate(seat.role):
        raise HTTPException(status_code=403, detail="Only administrators can manage billing")
    
    # Parse plan type if provided
    plan_type = None
    if data.plan_type:
        try:
            plan_type = PlanType(data.plan_type.lower())
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid plan type")
    
    # Update subscription
    updated = await BillingService.update_subscription(
        db, subscription, plan_type, data.seat_quantity
    )
    
    return {
        "subscription_id": str(updated.id),
        "plan_type": updated.plan_type.value,
        "seat_quantity": updated.seat_quantity,
        "status": updated.status.value,
    }


@router.delete("/subscriptions/{subscription_id}")
async def cancel_subscription(
    subscription_id: str,
    immediately: bool = False,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Cancel subscription"""
    result = await db.execute(
        select(Subscription).where(Subscription.id == subscription_id)
    )
    subscription = result.scalar_one_or_none()
    
    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")
    
    # Check permissions
    org_result = await db.execute(
        select(Organization).where(Organization.id == subscription.organization_id)
    )
    org = org_result.scalar_one()
    
    seat_result = await db.execute(
        select(Seat).where(
            Seat.organization_id == org.id,
            Seat.user_id == current_user.id,
        )
    )
    seat = seat_result.scalar_one_or_none()
    
    if not seat or not PermissionChecker.can_administrate(seat.role):
        raise HTTPException(status_code=403, detail="Only administrators can manage billing")
    
    try:
        cancelled = await BillingService.cancel_subscription(db, subscription, immediately)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("cancel_subscription failed for %s", subscription_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to cancel subscription: {exc}",
        ) from exc

    period_end = cancelled.current_period_end.isoformat() if cancelled.current_period_end else None

    return {
        "subscription_id": str(cancelled.id),
        "status": cancelled.status.value,
        "cancel_at_period_end": cancelled.cancel_at_period_end,
        "current_period_end": period_end,
        "message": (
            "Subscription canceled immediately."
            if immediately
            else "Subscription will cancel at the end of the current billing period."
        ),
    }


@router.post("/checkout-session")
async def create_checkout_session(
    data: CheckoutSessionCreate,
    organization_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Create Stripe checkout session"""
    result = await db.execute(
        select(Organization)
        .options(selectinload(Organization.owner))
        .where(Organization.id == organization_id)
    )
    org = result.scalar_one_or_none()
    
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    seat_result = await db.execute(
        select(Seat).where(
            Seat.organization_id == organization_id,
            Seat.user_id == current_user.id,
            Seat.is_active == True,
        )
    )
    seat = seat_result.scalar_one_or_none()
    if not seat or not PermissionChecker.can_administrate(seat.role):
        raise HTTPException(
            status_code=403,
            detail="Only administrators can manage billing",
        )
    
    # Ensure customer exists
    if not org.owner.stripe_customer_id:
        customer_id = await BillingService.create_customer(org.owner, org)
        org.owner.stripe_customer_id = customer_id
        await db.commit()
    
    try:
        plan_type = PlanType(data.plan_type.lower())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid plan type")
    
    session = await BillingService.get_checkout_session(
        org, plan_type, data.seat_quantity, data.success_url, data.cancel_url
    )
    
    return session


@router.post("/portal-session")
async def create_portal_session(
    organization_id: str,
    return_url: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Create Stripe customer portal session"""
    result = await db.execute(
        select(Organization).where(Organization.id == organization_id)
    )
    org = result.scalar_one_or_none()
    
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    
    session = await BillingService.get_portal_session(org, return_url)
    return session


@public_router.post("/webhook")
async def stripe_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Handle Stripe webhook events (no auth — verified by Stripe signature)."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    if not sig_header:
        logger.warning("Stripe webhook rejected: missing stripe-signature header")
        raise HTTPException(status_code=400, detail="Missing stripe-signature")

    try:
        import stripe
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret
        )
    except ValueError:
        logger.warning("Stripe webhook rejected: invalid payload")
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        logger.warning(
            "Stripe webhook rejected: invalid signature (check STRIPE_WEBHOOK_SECRET matches stripe listen)"
        )
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_type = event["type"]
    event_id = event.get("id", "unknown")
    logger.info("Stripe webhook received: type=%s id=%s", event_type, event_id)

    try:
        await BillingService.handle_webhook(db, event_type, event["data"]["object"])
        logger.info("Stripe webhook handled: type=%s id=%s", event_type, event_id)
    except Exception:
        logger.exception("Stripe webhook handler failed: type=%s id=%s", event_type, event_id)
        raise

    return {"status": "success"}


@router.get("/plans")
async def get_plans():
    """Get available pricing plans — v2.1"""
    return {
        "standard": {
            "name": "Standard",
            "price_per_seat": settings.standard_plan_price,
            "price_display": "$500/month",
            "min_seats": settings.standard_plan_min_seats,
            "max_seats": settings.standard_plan_max_seats,
            "free_trial_days": 14,
            "features": [
                "AI compliance Q&A (unlimited)",
                "Federal guidance + structured answers",
                "Document upload & storage",
                "Saved conversations & prompt templates",
                "14-day free trial",
            ],
        },
        "pro": {
            "name": "Pro",
            "price_per_seat": settings.pro_plan_price,
            "price_display": "$1,500/month",
            "min_seats": settings.pro_plan_min_seats,
            "max_seats": settings.pro_plan_max_seats,
            "free_trial_days": 14,
            "features": [
                "Everything in Standard",
                "Task management (single owner)",
                "Dashboard + compliance/risk/audit scores",
                "AI document analysis (50 pages max)",
                "Framework library (up to 3)",
                "Microsoft 365 + Google Workspace integrations",
                "Unlimited view-only users",
                "Blog (SEO role)",
            ],
        },
        "enterprise": {
            "name": "Enterprise",
            "price_per_seat": settings.enterprise_plan_price,
            "price_display": "$3,500/month",
            "min_seats": settings.enterprise_plan_min_seats,
            "max_seats": settings.enterprise_plan_max_seats,
            "free_trial_days": 14,
            "features": [
                "Everything in Pro",
                "Multi-user: Admin, Reviewer, Contributor",
                "Task assignment + 5-state approval workflows",
                "AI agent swarm (up to 20 agents)",
                "Salesforce integration",
                "Team-level compliance scoring",
                "Immutable audit trail",
                "Unlimited everything",
            ],
        },
    }
