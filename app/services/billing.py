"""
Stripe billing service
Handles subscriptions, payments, and seat-based billing
"""

from typing import Optional
from uuid import UUID

import stripe
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Organization, PlanType, Seat, Subscription, SubscriptionStatus, User

# Initialize Stripe
stripe.api_key = settings.stripe_secret_key


class BillingService:
    """Stripe billing service"""
    
    PLAN_PRICE_MAP = {
        PlanType.STANDARD: settings.stripe_price_standard,
        PlanType.PRO: settings.stripe_price_pro,
        PlanType.ENTERPRISE: settings.stripe_price_enterprise,
    }
    
    @staticmethod
    async def create_customer(
        user: User,
        organization: Organization,
    ) -> str:
        """Create Stripe customer"""
        try:
            customer = stripe.Customer.create(
                email=user.email,
                name=organization.name,
                metadata={
                    "organization_id": str(organization.id),
                    "user_id": str(user.id),
                },
            )
            return customer.id
        except stripe.error.StripeError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to create Stripe customer: {str(e)}"
            )
    
    @staticmethod
    async def create_subscription(
        db: AsyncSession,
        organization: Organization,
        plan_type: PlanType,
        seat_quantity: int,
        payment_method_id: Optional[str] = None,
    ) -> Subscription:
        """Create new subscription"""
        # Validate seat quantity
        limits = settings.get_plan_limits(plan_type.value)
        if seat_quantity < limits["min"] or seat_quantity > limits["max"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Seat quantity must be between {limits['min']} and {limits['max']}"
            )
        
        # Get or create Stripe customer
        if not organization.owner.stripe_customer_id:
            customer_id = await BillingService.create_customer(
                organization.owner, organization
            )
            organization.owner.stripe_customer_id = customer_id
            await db.commit()
        else:
            customer_id = organization.owner.stripe_customer_id
        
        # Attach payment method if provided
        if payment_method_id:
            try:
                stripe.PaymentMethod.attach(
                    payment_method_id,
                    customer=customer_id,
                )
                stripe.Customer.modify(
                    customer_id,
                    invoice_settings={
                        "default_payment_method": payment_method_id,
                    },
                )
            except stripe.error.StripeError as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Failed to attach payment method: {str(e)}"
                )
        
        # Get price ID
        price_id = BillingService.PLAN_PRICE_MAP.get(plan_type)
        if not price_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid plan type"
            )
        
        # Create Stripe subscription
        try:
            stripe_subscription = stripe.Subscription.create(
                customer=customer_id,
                items=[{
                    "price": price_id,
                    "quantity": seat_quantity,
                }],
                payment_behavior="default_incomplete",
                expand=["latest_invoice.payment_intent"],
                metadata={
                    "organization_id": str(organization.id),
                    "plan_type": plan_type.value,
                },
            )
        except stripe.error.StripeError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to create subscription: {str(e)}"
            )
        
        # Create local subscription record
        subscription = Subscription(
            organization_id=organization.id,
            plan_type=plan_type,
            stripe_subscription_id=stripe_subscription.id,
            stripe_price_id=price_id,
            stripe_customer_id=customer_id,
            status=SubscriptionStatus(stripe_subscription.status),
            seat_quantity=seat_quantity,
            current_period_start=stripe_subscription.current_period_start,
            current_period_end=stripe_subscription.current_period_end,
            trial_start=stripe_subscription.trial_start,
            trial_end=stripe_subscription.trial_end,
        )
        
        db.add(subscription)
        await db.commit()
        await db.refresh(subscription)
        
        return subscription
    
    @staticmethod
    async def update_subscription(
        db: AsyncSession,
        subscription: Subscription,
        new_plan_type: Optional[PlanType] = None,
        new_seat_quantity: Optional[int] = None,
    ) -> Subscription:
        """Update existing subscription"""
        items = []
        
        # Handle plan change
        if new_plan_type and new_plan_type != subscription.plan_type:
            new_price_id = BillingService.PLAN_PRICE_MAP.get(new_plan_type)
            if not new_price_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid plan type"
                )
            
            # Get current subscription item
            stripe_sub = stripe.Subscription.retrieve(subscription.stripe_subscription_id)
            current_item = stripe_sub["items"]["data"][0]
            
            items.append({
                "id": current_item["id"],
                "price": new_price_id,
                "quantity": new_seat_quantity or subscription.seat_quantity,
            })
            
            subscription.plan_type = new_plan_type
            subscription.stripe_price_id = new_price_id
        
        # Handle seat quantity change only
        elif new_seat_quantity and new_seat_quantity != subscription.seat_quantity:
            # Validate limits
            limits = settings.get_plan_limits(subscription.plan_type.value)
            if new_seat_quantity < limits["min"] or new_seat_quantity > limits["max"]:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Seat quantity must be between {limits['min']} and {limits['max']}"
                )
            
            stripe_sub = stripe.Subscription.retrieve(subscription.stripe_subscription_id)
            current_item = stripe_sub["items"]["data"][0]
            
            items.append({
                "id": current_item["id"],
                "quantity": new_seat_quantity,
            })
        
        if items:
            try:
                stripe.Subscription.modify(
                    subscription.stripe_subscription_id,
                    items=items,
                    proration_behavior="create_prorations",
                )
            except stripe.error.StripeError as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Failed to update subscription: {str(e)}"
                )
            
            subscription.seat_quantity = new_seat_quantity or subscription.seat_quantity
            await db.commit()
            await db.refresh(subscription)
        
        return subscription
    
    @staticmethod
    async def cancel_subscription(
        db: AsyncSession,
        subscription: Subscription,
        immediately: bool = False,
    ) -> Subscription:
        """Cancel subscription"""
        try:
            if immediately:
                stripe.Subscription.delete(subscription.stripe_subscription_id)
                subscription.status = SubscriptionStatus.CANCELED
            else:
                stripe.Subscription.modify(
                    subscription.stripe_subscription_id,
                    cancel_at_period_end=True,
                )
                subscription.cancel_at_period_end = True
            
            await db.commit()
            await db.refresh(subscription)
            
            return subscription
        except stripe.error.StripeError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to cancel subscription: {str(e)}"
            )
    
    @staticmethod
    async def handle_webhook(
        db: AsyncSession,
        event_type: str,
        event_data: dict,
    ) -> None:
        """Handle Stripe webhook events"""
        if event_type == "customer.subscription.created":
            await BillingService._handle_subscription_created(db, event_data)
        elif event_type == "customer.subscription.updated":
            await BillingService._handle_subscription_updated(db, event_data)
        elif event_type == "customer.subscription.deleted":
            await BillingService._handle_subscription_deleted(db, event_data)
        elif event_type == "invoice.paid":
            await BillingService._handle_invoice_paid(db, event_data)
        elif event_type == "invoice.payment_failed":
            await BillingService._handle_invoice_failed(db, event_data)
    
    @staticmethod
    async def _handle_subscription_created(
        db: AsyncSession,
        data: dict,
    ) -> None:
        """Handle subscription.created webhook"""
        # Subscription already created via API, just log
        pass
    
    @staticmethod
    async def _handle_subscription_updated(
        db: AsyncSession,
        data: dict,
    ) -> None:
        """Handle subscription.updated webhook"""
        stripe_sub_id = data["id"]
        
        result = await db.execute(
            select(Subscription).where(
                Subscription.stripe_subscription_id == stripe_sub_id
            )
        )
        subscription = result.scalar_one_or_none()
        
        if subscription:
            subscription.status = SubscriptionStatus(data["status"])
            subscription.current_period_start = data["current_period_start"]
            subscription.current_period_end = data["current_period_end"]
            subscription.cancel_at_period_end = data.get("cancel_at_period_end", False)
            await db.commit()
    
    @staticmethod
    async def _handle_subscription_deleted(
        db: AsyncSession,
        data: dict,
    ) -> None:
        """Handle subscription.deleted webhook"""
        stripe_sub_id = data["id"]
        
        result = await db.execute(
            select(Subscription).where(
                Subscription.stripe_subscription_id == stripe_sub_id
            )
        )
        subscription = result.scalar_one_or_none()
        
        if subscription:
            subscription.status = SubscriptionStatus.CANCELED
            await db.commit()
    
    @staticmethod
    async def _handle_invoice_paid(
        db: AsyncSession,
        data: dict,
    ) -> None:
        """Handle invoice.paid webhook"""
        # Payment successful, ensure subscription is active
        subscription_id = data.get("subscription")
        if subscription_id:
            result = await db.execute(
                select(Subscription).where(
                    Subscription.stripe_subscription_id == subscription_id
                )
            )
            subscription = result.scalar_one_or_none()
            if subscription:
                subscription.status = SubscriptionStatus.ACTIVE
                await db.commit()
    
    @staticmethod
    async def _handle_invoice_failed(
        db: AsyncSession,
        data: dict,
    ) -> None:
        """Handle invoice.payment_failed webhook"""
        subscription_id = data.get("subscription")
        if subscription_id:
            result = await db.execute(
                select(Subscription).where(
                    Subscription.stripe_subscription_id == subscription_id
                )
            )
            subscription = result.scalar_one_or_none()
            if subscription:
                subscription.status = SubscriptionStatus.PAST_DUE
                await db.commit()
    
    @staticmethod
    async def get_checkout_session(
        organization: Organization,
        plan_type: PlanType,
        seat_quantity: int,
        success_url: str,
        cancel_url: str,
    ) -> dict:
        """Create Stripe checkout session"""
        price_id = BillingService.PLAN_PRICE_MAP.get(plan_type)
        if not price_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid plan type"
            )
        
        try:
            session = stripe.checkout.Session.create(
                customer=organization.owner.stripe_customer_id,
                payment_method_types=["card"],
                line_items=[{
                    "price": price_id,
                    "quantity": seat_quantity,
                }],
                mode="subscription",
                success_url=success_url,
                cancel_url=cancel_url,
                metadata={
                    "organization_id": str(organization.id),
                    "plan_type": plan_type.value,
                },
            )
            return {"session_id": session.id, "url": session.url}
        except stripe.error.StripeError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to create checkout session: {str(e)}"
            )
    
    @staticmethod
    async def get_portal_session(
        organization: Organization,
        return_url: str,
    ) -> dict:
        """Create Stripe customer portal session"""
        if not organization.owner.stripe_customer_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No Stripe customer found"
            )
        
        try:
            session = stripe.billing_portal.Session.create(
                customer=organization.owner.stripe_customer_id,
                return_url=return_url,
            )
            return {"url": session.url}
        except stripe.error.StripeError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to create portal session: {str(e)}"
            )
