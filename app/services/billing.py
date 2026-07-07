"""
Stripe billing service
Handles subscriptions, payments, and seat-based billing
"""

import logging
from datetime import datetime, timezone
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
logger = logging.getLogger(__name__)


class BillingService:
    """Stripe billing service"""

    ENTERPRISE_BASE_SEAT_COUNT = 1
    
    PLAN_PRICE_MAP = {
        PlanType.STANDARD: settings.stripe_price_standard,
        PlanType.PRO: settings.stripe_price_pro,
        PlanType.ENTERPRISE: settings.stripe_price_enterprise,
    }

    @staticmethod
    def _enterprise_base_price_id() -> str:
        return settings.stripe_price_enterprise

    @staticmethod
    def _enterprise_additional_price_id() -> str:
        return settings.get_enterprise_additional_price_id()

    @staticmethod
    def _subscription_items(stripe_sub: dict) -> list:
        items = stripe_sub.get("items")
        if items is None:
            return []
        data = items.get("data") if hasattr(items, "get") else getattr(items, "data", None)
        return list(data or [])

    @staticmethod
    def _find_subscription_item_by_price(stripe_sub: dict, price_id: str) -> Optional[dict]:
        for item in BillingService._subscription_items(stripe_sub):
            item_price = item.get("price") or {}
            if item_price.get("id") == price_id:
                return item
        return None

    @staticmethod
    def _parse_enterprise_total_seats_from_stripe(stripe_sub: dict) -> int:
        """
        Enterprise billing: 1 base seat ($3,500) + N additional seats (per-seat price).
        Legacy subs with a single line item use that item's quantity as total.
        """
        base_price = BillingService._enterprise_base_price_id()
        add_price = BillingService._enterprise_additional_price_id()
        base_item = BillingService._find_subscription_item_by_price(stripe_sub, base_price)
        add_item = BillingService._find_subscription_item_by_price(stripe_sub, add_price)

        if base_item or add_item:
            base_qty = int((base_item or {}).get("quantity") or 0)
            add_qty = int((add_item or {}).get("quantity") or 0)
            if base_qty <= 0 and add_qty > 0:
                return BillingService.ENTERPRISE_BASE_SEAT_COUNT + add_qty
            return max(base_qty, BillingService.ENTERPRISE_BASE_SEAT_COUNT) + add_qty

        items = BillingService._subscription_items(stripe_sub)
        if items:
            return int(items[0].get("quantity") or 1)
        return 1
    
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
            if subscription.plan_type == PlanType.ENTERPRISE:
                updated, _charge = await BillingService.update_enterprise_total_seats(
                    db, subscription, new_seat_quantity
                )
                return updated
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
    async def update_enterprise_total_seats(
        db: AsyncSession,
        subscription: Subscription,
        total_seat_quantity: int,
    ) -> tuple[Subscription, int]:
        """
        Enterprise: keep 1 base seat at $3,500 and bill extras at additional seat price.
        total_seat_quantity = 1 (owner) + additional paid users.
        Charges immediately when adding seats (prorated); credits on removal.
        """
        limits = settings.get_plan_limits(PlanType.ENTERPRISE.value)
        if total_seat_quantity < limits["min"] or total_seat_quantity > limits["max"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Seat quantity must be between {limits['min']} and {limits['max']}",
            )

        previous_qty = subscription.seat_quantity or BillingService.ENTERPRISE_BASE_SEAT_COUNT
        is_adding_seats = total_seat_quantity > previous_qty

        additional_qty = max(0, total_seat_quantity - BillingService.ENTERPRISE_BASE_SEAT_COUNT)
        base_price_id = BillingService._enterprise_base_price_id()
        add_price_id = BillingService._enterprise_additional_price_id()

        try:
            stripe_sub = stripe.Subscription.retrieve(subscription.stripe_subscription_id)
        except stripe.error.StripeError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to load subscription: {str(e)}",
            )

        base_item = BillingService._find_subscription_item_by_price(stripe_sub, base_price_id)
        add_item = BillingService._find_subscription_item_by_price(stripe_sub, add_price_id)
        modify_items: list[dict] = []

        if base_item:
            modify_items.append({
                "id": base_item["id"],
                "quantity": BillingService.ENTERPRISE_BASE_SEAT_COUNT,
            })
        elif BillingService._subscription_items(stripe_sub):
            first = BillingService._subscription_items(stripe_sub)[0]
            modify_items.append({
                "id": first["id"],
                "quantity": BillingService.ENTERPRISE_BASE_SEAT_COUNT,
            })
        else:
            modify_items.append({
                "price": base_price_id,
                "quantity": BillingService.ENTERPRISE_BASE_SEAT_COUNT,
            })

        if additional_qty > 0:
            if add_item:
                modify_items.append({"id": add_item["id"], "quantity": additional_qty})
            else:
                modify_items.append({"price": add_price_id, "quantity": additional_qty})
        elif add_item:
            modify_items.append({"id": add_item["id"], "deleted": True})

        modify_kwargs: dict = {
            "items": modify_items,
            "proration_behavior": "always_invoice" if is_adding_seats else "create_prorations",
        }
        if is_adding_seats:
            modify_kwargs["payment_behavior"] = "error_if_incomplete"

        try:
            updated_sub = stripe.Subscription.modify(
                subscription.stripe_subscription_id,
                expand=["latest_invoice"],
                **modify_kwargs,
            )
        except stripe.error.StripeError as e:
            status_code = (
                status.HTTP_402_PAYMENT_REQUIRED
                if is_adding_seats
                else status.HTTP_400_BAD_REQUEST
            )
            raise HTTPException(
                status_code=status_code,
                detail=f"Failed to update enterprise seats: {str(e)}",
            ) from e

        amount_charged_cents = 0
        if is_adding_seats:
            latest_invoice = updated_sub.get("latest_invoice")
            if isinstance(latest_invoice, dict):
                amount_charged_cents = int(
                    latest_invoice.get("amount_paid")
                    or latest_invoice.get("amount_due")
                    or 0
                )

        subscription.seat_quantity = total_seat_quantity
        subscription.stripe_price_id = base_price_id
        await db.commit()
        await db.refresh(subscription)
        return subscription, amount_charged_cents
    
    @staticmethod
    def _is_stripe_billed_subscription(stripe_subscription_id: str) -> bool:
        """True when subscription is managed in Stripe (not local/trial placeholders)."""
        return bool(stripe_subscription_id) and stripe_subscription_id.startswith("sub_")

    @staticmethod
    async def resolve_stripe_subscription_id(
        db: AsyncSession,
        subscription: Subscription,
    ) -> Optional[str]:
        """
        Return a real Stripe subscription id (sub_...).
        Repairs pending_/local_ placeholders using the stored Stripe customer id.
        """
        sid = subscription.stripe_subscription_id or ""
        if BillingService._is_stripe_billed_subscription(sid):
            return sid

        customer_id = subscription.stripe_customer_id
        if not customer_id or customer_id.startswith("local"):
            return None

        try:
            listed = stripe.Subscription.list(
                customer=customer_id,
                status="active",
                limit=1,
            )
            if listed.data:
                real_id = listed.data[0].id
                subscription.stripe_subscription_id = real_id
                await db.flush()
                logger.info(
                    "Resolved Stripe subscription %s for org subscription %s",
                    real_id,
                    subscription.id,
                )
                return real_id
            listed_all = stripe.Subscription.list(customer=customer_id, limit=1)
            if listed_all.data:
                real_id = listed_all.data[0].id
                subscription.stripe_subscription_id = real_id
                await db.flush()
                return real_id
        except stripe.error.StripeError as e:
            logger.warning("Could not list Stripe subscriptions for %s: %s", customer_id, e)
        return None

    @staticmethod
    async def cancel_subscription(
        db: AsyncSession,
        subscription: Subscription,
        immediately: bool = False,
    ) -> Subscription:
        """Cancel subscription (Stripe cancel at period end, or local trial end)."""
        stripe_sub_id = subscription.stripe_subscription_id
        if not BillingService._is_stripe_billed_subscription(stripe_sub_id or ""):
            resolved = await BillingService.resolve_stripe_subscription_id(db, subscription)
            if resolved:
                stripe_sub_id = resolved

        if BillingService._is_stripe_billed_subscription(stripe_sub_id or ""):
            try:
                if immediately:
                    stripe.Subscription.delete(stripe_sub_id)
                    subscription.status = SubscriptionStatus.CANCELED
                    subscription.cancel_at_period_end = False
                else:
                    stripe.Subscription.modify(
                        stripe_sub_id,
                        cancel_at_period_end=True,
                    )
                    subscription.cancel_at_period_end = True
            except stripe.error.StripeError as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Failed to cancel subscription: {str(e)}",
                )
        else:
            # Paid in app but Stripe id not resolved (sync/webhook gap)
            if subscription.status in (
                SubscriptionStatus.ACTIVE,
                SubscriptionStatus.PAST_DUE,
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "Could not find your Stripe subscription to cancel. "
                        "Please contact support or try again after refreshing the page."
                    ),
                )
            # Free trial / local placeholders — no Stripe call
            if immediately:
                subscription.status = SubscriptionStatus.CANCELED
                subscription.cancel_at_period_end = False
            else:
                subscription.cancel_at_period_end = True
                if subscription.status == SubscriptionStatus.TRIALING:
                    subscription.status = SubscriptionStatus.CANCELED
            logger.info(
                "Canceled local subscription record %s (stripe_id=%s)",
                subscription.id,
                stripe_sub_id,
            )

        await db.commit()
        await db.refresh(subscription)
        return subscription
    
    @staticmethod
    def _stripe_ts_to_dt(ts: Optional[int]) -> datetime:
        if not ts:
            return datetime.now(timezone.utc).replace(tzinfo=None)
        return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)

    @staticmethod
    def _period_timestamps_from_subscription(data) -> tuple[Optional[int], Optional[int]]:
        """
        Billing period bounds from a Stripe Subscription object.
        Newer API versions may omit top-level current_period_* on webhook payloads;
        fall back to the first subscription item when needed.
        """
        start = data.get("current_period_start") if hasattr(data, "get") else None
        end = data.get("current_period_end") if hasattr(data, "get") else None
        if start is not None and end is not None:
            return start, end

        items = data.get("items") if hasattr(data, "get") else None
        if items is not None:
            item_data = items.get("data") if hasattr(items, "get") else getattr(items, "data", None)
            if item_data:
                first = item_data[0]
                if hasattr(first, "get"):
                    start = start or first.get("current_period_start")
                    end = end or first.get("current_period_end")
        return start, end

    @staticmethod
    async def sync_subscription_from_stripe(
        db: AsyncSession,
        organization_id: UUID,
        stripe_subscription_id: str,
        stripe_customer_id: Optional[str] = None,
        plan_type_hint: Optional[PlanType] = None,
    ) -> Subscription:
        """Upsert local subscription from a Stripe subscription object."""
        stripe_sub = stripe.Subscription.retrieve(stripe_subscription_id)
        item_price_ids = [
            (item.get("price") or {}).get("id")
            for item in BillingService._subscription_items(stripe_sub)
        ]
        price_id = item_price_ids[0] if item_price_ids else ""
        plan_type = plan_type_hint
        if not plan_type:
            base_ent = BillingService._enterprise_base_price_id()
            add_ent = BillingService._enterprise_additional_price_id()
            if base_ent in item_price_ids or add_ent in item_price_ids:
                plan_type = PlanType.ENTERPRISE
            else:
                for pt, pid in BillingService.PLAN_PRICE_MAP.items():
                    if pid in item_price_ids:
                        plan_type = pt
                        break
        if not plan_type:
            meta_plan = (stripe_sub.get("metadata") or {}).get("plan_type")
            if meta_plan:
                plan_type = PlanType(meta_plan.lower())
        if not plan_type:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Could not determine plan type from Stripe subscription",
            )

        customer_id = stripe_customer_id or stripe_sub.get("customer")
        if isinstance(customer_id, dict):
            customer_id = customer_id.get("id")

        result = await db.execute(
            select(Subscription).where(Subscription.organization_id == organization_id)
        )
        subscription = result.scalar_one_or_none()
        status_value = SubscriptionStatus(stripe_sub["status"])
        period_start_ts, period_end_ts = BillingService._period_timestamps_from_subscription(
            stripe_sub
        )
        period_start = BillingService._stripe_ts_to_dt(period_start_ts)
        period_end = BillingService._stripe_ts_to_dt(period_end_ts)
        if plan_type == PlanType.ENTERPRISE:
            seat_qty = BillingService._parse_enterprise_total_seats_from_stripe(stripe_sub)
            price_id = BillingService._enterprise_base_price_id()
        else:
            price_id = stripe_sub["items"]["data"][0]["price"]["id"]
            seat_qty = int(stripe_sub["items"]["data"][0].get("quantity") or 1)

        if subscription:
            subscription.stripe_subscription_id = stripe_subscription_id
            subscription.stripe_price_id = price_id
            subscription.stripe_customer_id = str(customer_id)
            subscription.plan_type = plan_type
            subscription.status = status_value
            subscription.seat_quantity = seat_qty
            subscription.current_period_start = period_start
            subscription.current_period_end = period_end
            subscription.trial_start = None
            subscription.trial_end = None
            subscription.cancel_at_period_end = bool(stripe_sub.get("cancel_at_period_end"))
        else:
            subscription = Subscription(
                organization_id=organization_id,
                plan_type=plan_type,
                stripe_subscription_id=stripe_subscription_id,
                stripe_price_id=price_id,
                stripe_customer_id=str(customer_id),
                status=status_value,
                seat_quantity=seat_qty,
                current_period_start=period_start,
                current_period_end=period_end,
            )
            db.add(subscription)

        await db.commit()
        await db.refresh(subscription)

        customer_id_str = str(customer_id) if customer_id else subscription.stripe_customer_id
        if customer_id_str and BillingService._is_stripe_billed_subscription(stripe_subscription_id):
            await BillingService._cancel_duplicate_stripe_subscriptions(
                customer_id_str,
                stripe_subscription_id,
            )

        return subscription

    @staticmethod
    async def create_signup_checkout_session(
        db: AsyncSession,
        user: User,
        organization: Organization,
        plan_type: PlanType,
        seat_quantity: int,
        success_url: str,
        cancel_url: str,
        promotion_code: Optional[str] = None,
    ) -> dict:
        """Stripe Checkout for new sign-up (paid subscription, no trial)."""
        price_id = BillingService.PLAN_PRICE_MAP.get(plan_type)
        if not price_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid plan type",
            )

        if not user.stripe_customer_id:
            customer_id = await BillingService.create_customer(user, organization)
            user.stripe_customer_id = customer_id
            await db.commit()

        metadata = {
            "organization_id": str(organization.id),
            "user_id": str(user.id),
            "plan_type": plan_type.value,
        }
        promo_stripe_id: Optional[str] = None
        if promotion_code and promotion_code.strip():
            from app.services.stripe_promotions_service import StripePromotionsService

            promo = StripePromotionsService.lookup_for_checkout(
                promotion_code.strip(),
                plan_type,
            )
            promo_stripe_id = promo["id"]
            metadata["promotion_code"] = promo.get("code") or promotion_code.strip().upper()

        try:
            session_params: dict = {
                "customer": user.stripe_customer_id,
                "payment_method_types": ["card"],
                "line_items": [{"price": price_id, "quantity": seat_quantity}],
                "mode": "subscription",
                "success_url": success_url,
                "cancel_url": cancel_url,
                "metadata": metadata,
                "subscription_data": {"metadata": metadata},
            }
            if promo_stripe_id:
                session_params["discounts"] = [{"promotion_code": promo_stripe_id}]

            session = stripe.checkout.Session.create(**session_params)
            return {"session_id": session.id, "url": session.url}
        except stripe.error.StripeError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to create checkout session: {str(e)}",
            )

    @staticmethod
    async def complete_checkout_session(
        db: AsyncSession,
        session_id: str,
    ) -> tuple[User, Organization, Subscription]:
        """Verify Checkout session payment and sync subscription."""
        try:
            session = stripe.checkout.Session.retrieve(
                session_id,
                expand=["subscription", "customer"],
            )
        except stripe.error.StripeError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid checkout session: {str(e)}",
            )

        if session.payment_status not in ("paid", "no_payment_required"):
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="Payment not completed",
            )

        org_id_raw = (session.metadata or {}).get("organization_id")
        if not org_id_raw:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing organization in checkout session",
            )

        org_id = UUID(org_id_raw)
        result = await db.execute(
            select(Organization).where(Organization.id == org_id)
        )
        org = result.scalar_one_or_none()
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")

        user_result = await db.execute(select(User).where(User.id == org.owner_id))
        user = user_result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        customer_id = session.customer
        if isinstance(customer_id, dict):
            customer_id = customer_id.get("id")
        if customer_id and not user.stripe_customer_id:
            user.stripe_customer_id = str(customer_id)
            await db.commit()

        stripe_sub = session.subscription
        if isinstance(stripe_sub, str):
            stripe_sub_id = stripe_sub
        elif stripe_sub:
            stripe_sub_id = stripe_sub.id
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No subscription on checkout session",
            )

        plan_hint = None
        plan_raw = (session.metadata or {}).get("plan_type")
        if plan_raw:
            plan_hint = PlanType(plan_raw.lower())

        subscription = await BillingService.sync_subscription_from_stripe(
            db,
            org_id,
            stripe_sub_id,
            stripe_customer_id=str(customer_id) if customer_id else None,
            plan_type_hint=plan_hint,
        )
        return user, org, subscription

    @staticmethod
    async def handle_webhook(
        db: AsyncSession,
        event_type: str,
        event_data: dict,
    ) -> None:
        """Handle Stripe webhook events"""
        if event_type == "checkout.session.completed":
            await BillingService._handle_checkout_session_completed(db, event_data)
        elif event_type == "customer.subscription.created":
            await BillingService._handle_subscription_created(db, event_data)
        elif event_type == "customer.subscription.updated":
            await BillingService._handle_subscription_updated(db, event_data)
        elif event_type == "customer.subscription.deleted":
            await BillingService._handle_subscription_deleted(db, event_data)
        elif event_type == "invoice.paid":
            await BillingService._handle_invoice_paid(db, event_data)
        elif event_type == "invoice.payment_failed":
            await BillingService._handle_invoice_failed(db, event_data)
        else:
            logger.info("Stripe webhook ignored (no handler): type=%s", event_type)

    @staticmethod
    async def _handle_checkout_session_completed(
        db: AsyncSession,
        data: dict,
    ) -> None:
        """Activate subscription after Stripe Checkout."""
        org_id_raw = (data.get("metadata") or {}).get("organization_id")
        stripe_sub_id = data.get("subscription")
        if not org_id_raw or not stripe_sub_id:
            logger.warning(
                "checkout.session.completed skipped: org_id=%s subscription=%s",
                org_id_raw,
                stripe_sub_id,
            )
            return
        plan_raw = (data.get("metadata") or {}).get("plan_type")
        plan_hint = PlanType(plan_raw.lower()) if plan_raw else None
        customer_id = data.get("customer")
        logger.info(
            "checkout.session.completed: org_id=%s plan=%s subscription=%s",
            org_id_raw,
            plan_raw,
            stripe_sub_id,
        )
        await BillingService.sync_subscription_from_stripe(
            db,
            UUID(org_id_raw),
            stripe_sub_id,
            stripe_customer_id=str(customer_id) if customer_id else None,
            plan_type_hint=plan_hint,
        )
        logger.info("checkout.session.completed: subscription synced for org_id=%s", org_id_raw)
    
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
        stripe_sub_id = data.get("id") if hasattr(data, "get") else data["id"]
        if not stripe_sub_id:
            logger.warning("customer.subscription.updated: missing subscription id")
            return

        result = await db.execute(
            select(Subscription).where(
                Subscription.stripe_subscription_id == stripe_sub_id
            )
        )
        subscription = result.scalar_one_or_none()

        if not subscription:
            logger.warning(
                "customer.subscription.updated: no local subscription for %s",
                stripe_sub_id,
            )
            return

        # Webhook payloads (newer Stripe API versions) may not include period fields;
        # always refresh from the Subscription API for a complete object.
        try:
            stripe_sub = stripe.Subscription.retrieve(stripe_sub_id)
        except stripe.error.StripeError as e:
            logger.error(
                "customer.subscription.updated: retrieve failed for %s: %s",
                stripe_sub_id,
                e,
            )
            raise

        status_raw = stripe_sub.get("status") or data.get("status")
        subscription.status = SubscriptionStatus(status_raw)
        period_start_ts, period_end_ts = BillingService._period_timestamps_from_subscription(
            stripe_sub
        )
        if period_start_ts is not None:
            subscription.current_period_start = BillingService._stripe_ts_to_dt(
                period_start_ts
            )
        if period_end_ts is not None:
            subscription.current_period_end = BillingService._stripe_ts_to_dt(period_end_ts)
        subscription.cancel_at_period_end = bool(
            stripe_sub.get("cancel_at_period_end")
            if stripe_sub.get("cancel_at_period_end") is not None
            else data.get("cancel_at_period_end", False)
        )
        if subscription.plan_type == PlanType.ENTERPRISE:
            subscription.seat_quantity = BillingService._parse_enterprise_total_seats_from_stripe(
                stripe_sub
            )
        await db.commit()
        logger.info(
            "customer.subscription.updated: sub_id=%s status=%s cancel_at_period_end=%s",
            stripe_sub_id,
            status_raw,
            subscription.cancel_at_period_end,
        )
    
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
                logger.info("invoice.paid: subscription %s set active", subscription_id)
    
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
    
    UPGRADE_IN_PLACE_STATUSES = frozenset({
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.PAST_DUE,
    })

    @staticmethod
    def _build_plan_change_modify_items(
        stripe_sub: dict,
        new_plan_type: PlanType,
        seat_quantity: int,
    ) -> list[dict]:
        """Build Stripe Subscription.modify items for a plan change (with proration)."""
        limits = settings.get_plan_limits(new_plan_type.value)
        qty = max(seat_quantity, limits["min"])
        if qty > limits["max"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Seat quantity must be between {limits['min']} and {limits['max']}",
            )

        existing = BillingService._subscription_items(stripe_sub)

        if new_plan_type == PlanType.ENTERPRISE:
            base_price = BillingService._enterprise_base_price_id()
            add_price = BillingService._enterprise_additional_price_id()
            additional_qty = max(0, qty - BillingService.ENTERPRISE_BASE_SEAT_COUNT)
            modify_items: list[dict] = []
            for item in existing:
                modify_items.append({"id": item["id"], "deleted": True})
            modify_items.append({
                "price": base_price,
                "quantity": BillingService.ENTERPRISE_BASE_SEAT_COUNT,
            })
            if additional_qty > 0:
                modify_items.append({"price": add_price, "quantity": additional_qty})
            return modify_items

        new_price_id = BillingService.PLAN_PRICE_MAP.get(new_plan_type)
        if not new_price_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid plan type",
            )

        if not existing:
            return [{"price": new_price_id, "quantity": qty}]

        # Standard / Pro: single line item — replace first, remove extras (e.g. legacy Enterprise lines).
        modify_items = []
        for index, item in enumerate(existing):
            if index == 0:
                modify_items.append({
                    "id": item["id"],
                    "price": new_price_id,
                    "quantity": qty,
                })
            else:
                modify_items.append({"id": item["id"], "deleted": True})
        return modify_items

    @staticmethod
    def _format_usd_cents(cents: int) -> str:
        return f"${abs(cents) / 100:,.2f}"

    @staticmethod
    def _plan_display_label(plan_type: PlanType) -> str:
        labels = {
            PlanType.STANDARD: "Standard",
            PlanType.PRO: "Pro",
            PlanType.ENTERPRISE: "Enterprise",
        }
        return labels.get(plan_type, plan_type.value.title())

    @staticmethod
    def _get_default_card_last4(customer_id: Optional[str]) -> Optional[str]:
        if not customer_id or customer_id.startswith("local"):
            return None
        try:
            customer = stripe.Customer.retrieve(
                customer_id,
                expand=["invoice_settings.default_payment_method"],
            )
            pm = customer.get("invoice_settings", {}).get("default_payment_method")
            if isinstance(pm, dict):
                card = pm.get("card") or {}
                return card.get("last4")
        except stripe.error.StripeError:
            return None
        return None

    @staticmethod
    def _parse_invoice_preview_lines(invoice: dict) -> tuple[list[dict], int, int, int]:
        """Return summary lines, amount_due, total credits, total charges (cents)."""
        summary: list[dict] = []
        credit_total = 0
        charge_total = 0
        lines = invoice.get("lines", {}).get("data", []) if hasattr(invoice, "get") else []

        for line in lines:
            amount = int(line.get("amount") or 0)
            if amount == 0:
                continue
            description = (line.get("description") or "Plan adjustment").strip()
            parent = line.get("parent") or {}
            sub_details = parent.get("subscription_item_details") or {}
            is_proration = bool(sub_details.get("proration"))

            if amount < 0:
                credit_total += abs(amount)
                summary.append({
                    "label": description,
                    "amount_cents": amount,
                    "formatted": f"-{BillingService._format_usd_cents(amount)}",
                    "is_proration": is_proration,
                })
            else:
                charge_total += amount
                summary.append({
                    "label": description,
                    "amount_cents": amount,
                    "formatted": BillingService._format_usd_cents(amount),
                    "is_proration": is_proration,
                })

        amount_due = int(invoice.get("amount_due") or 0)
        return summary, amount_due, credit_total, charge_total

    @staticmethod
    def _preview_subscription_change_invoice(
        customer_id: str,
        subscription_id: str,
        modify_items: list[dict],
        proration_date: int,
    ) -> dict:
        """Preview prorated invoice for a subscription plan change (SDK-compatible)."""
        if hasattr(stripe.Invoice, "create_preview"):
            return stripe.Invoice.create_preview(
                customer=customer_id,
                subscription=subscription_id,
                subscription_details={
                    "items": modify_items,
                    "proration_behavior": "always_invoice",
                    "proration_date": proration_date,
                },
            )

        # stripe==8.x (pinned in requirements): use upcoming invoice preview API.
        return stripe.Invoice.upcoming(
            customer=customer_id,
            subscription=subscription_id,
            subscription_items=modify_items,
            subscription_proration_behavior="always_invoice",
            subscription_proration_date=proration_date,
        )

    @staticmethod
    def _build_checkout_line_items(
        plan_type: PlanType,
        total_seat_quantity: int,
    ) -> list[dict]:
        """Stripe Checkout line items for first paid subscription."""
        if plan_type == PlanType.ENTERPRISE:
            base_price = BillingService._enterprise_base_price_id()
            add_price = BillingService._enterprise_additional_price_id()
            additional_qty = max(
                0, total_seat_quantity - BillingService.ENTERPRISE_BASE_SEAT_COUNT
            )
            items = [{
                "price": base_price,
                "quantity": BillingService.ENTERPRISE_BASE_SEAT_COUNT,
            }]
            if additional_qty > 0:
                items.append({"price": add_price, "quantity": additional_qty})
            return items

        price_id = BillingService.PLAN_PRICE_MAP.get(plan_type)
        if not price_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid plan type",
            )
        return [{"price": price_id, "quantity": total_seat_quantity}]

    @staticmethod
    async def _preview_trial_checkout(
        db: AsyncSession,
        subscription: Subscription,
        new_plan_type: PlanType,
        seat_quantity: int,
    ) -> dict:
        """Preview first paid checkout (trial → paid), including trial team seats."""
        from app.services.seat_allocation import (
            count_paid_seats_allocated,
            resolve_checkout_seat_quantity,
        )

        total_seats = await resolve_checkout_seat_quantity(
            subscription.organization_id,
            subscription,
            new_plan_type,
            seat_quantity,
            db,
        )
        allocated = await count_paid_seats_allocated(subscription.organization_id, db)
        new_label = BillingService._plan_display_label(new_plan_type)

        summary_lines: list[dict] = []
        if new_plan_type == PlanType.ENTERPRISE:
            additional = max(0, total_seats - BillingService.ENTERPRISE_BASE_SEAT_COUNT)
            base_cents = settings.enterprise_plan_price
            add_cents = settings.enterprise_additional_seat_price * additional
            amount_due = base_cents + add_cents
            summary_lines.append({
                "label": "Enterprise owner seat",
                "formatted": BillingService._format_usd_cents(base_cents),
            })
            if additional > 0:
                summary_lines.append({
                    "label": f"Additional team seats × {additional}",
                    "formatted": BillingService._format_usd_cents(add_cents),
                })
            summary_lines.append({
                "label": "Due today (first month)",
                "formatted": BillingService._format_usd_cents(amount_due),
                "emphasis": True,
            })
        else:
            amount_due = settings.get_plan_price(new_plan_type.value)
            summary_lines.append({
                "label": f"{new_label} plan",
                "formatted": BillingService._format_usd_cents(amount_due),
                "emphasis": True,
            })

        return {
            "upgrade_type": "checkout",
            "seat_quantity": total_seats,
            "allocated_paid_seats": allocated,
            "new_plan": new_plan_type.value,
            "new_plan_label": new_label,
            "amount_due_cents": amount_due,
            "amount_due_formatted": BillingService._format_usd_cents(amount_due),
            "summary_lines": summary_lines,
        }

    @staticmethod
    async def preview_upgrade_plan(
        db: AsyncSession,
        subscription: Subscription,
        new_plan_type: PlanType,
        seat_quantity: int,
    ) -> dict:
        """Preview prorated charge for an in-place plan upgrade (Stripe invoice preview)."""
        stripe_sub_id = await BillingService._resolve_billable_stripe_subscription_id(
            db, subscription
        )
        if not stripe_sub_id:
            return await BillingService._preview_trial_checkout(
                db, subscription, new_plan_type, seat_quantity
            )

        if new_plan_type == subscription.plan_type:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"You are already on the {new_plan_type.value} plan",
            )

        stripe_sub = stripe.Subscription.retrieve(stripe_sub_id)
        modify_items = BillingService._build_plan_change_modify_items(
            stripe_sub, new_plan_type, seat_quantity
        )
        proration_date = int(datetime.now(tz=timezone.utc).timestamp())

        try:
            invoice = BillingService._preview_subscription_change_invoice(
                subscription.stripe_customer_id,
                stripe_sub_id,
                modify_items,
                proration_date,
            )
        except stripe.error.StripeError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Could not preview upgrade: {str(exc)}",
            ) from exc

        summary_lines, amount_due, credit_total, charge_total = (
            BillingService._parse_invoice_preview_lines(invoice)
        )
        period_end = subscription.current_period_end
        card_last4 = BillingService._get_default_card_last4(subscription.stripe_customer_id)

        current_label = BillingService._plan_display_label(subscription.plan_type)
        new_label = BillingService._plan_display_label(new_plan_type)

        friendly_lines: list[dict] = []
        if credit_total > 0:
            friendly_lines.append({
                "label": f"Credit for unused {current_label} time",
                "formatted": f"-{BillingService._format_usd_cents(credit_total)}",
            })
        if charge_total > 0:
            friendly_lines.append({
                "label": f"{new_label} plan (remaining billing period)",
                "formatted": BillingService._format_usd_cents(charge_total),
            })
        friendly_lines.append({
            "label": "Due today",
            "formatted": BillingService._format_usd_cents(amount_due),
            "emphasis": True,
        })

        return {
            "upgrade_type": "in_place",
            "current_plan": subscription.plan_type.value,
            "new_plan": new_plan_type.value,
            "current_plan_label": current_label,
            "new_plan_label": new_label,
            "amount_due_cents": amount_due,
            "amount_due_formatted": BillingService._format_usd_cents(amount_due),
            "credit_cents": credit_total,
            "credit_formatted": BillingService._format_usd_cents(credit_total),
            "charge_cents": charge_total,
            "charge_formatted": BillingService._format_usd_cents(charge_total),
            "period_end": period_end.strftime("%Y-%m-%d") if period_end else None,
            "proration_date": proration_date,
            "card_last4": card_last4,
            "summary_lines": friendly_lines,
            "detail_lines": summary_lines,
        }

    @staticmethod
    async def _cancel_duplicate_stripe_subscriptions(
        customer_id: Optional[str],
        keep_subscription_id: str,
    ) -> None:
        """Cancel extra active Stripe subscriptions on the same customer (one plan per org)."""
        if not customer_id or customer_id.startswith("local"):
            return
        for stripe_status in ("active", "past_due", "trialing"):
            try:
                listed = stripe.Subscription.list(
                    customer=customer_id,
                    status=stripe_status,
                    limit=100,
                )
            except stripe.error.StripeError as exc:
                logger.warning(
                    "Could not list %s subscriptions for %s: %s",
                    stripe_status,
                    customer_id,
                    exc,
                )
                continue
            for sub in listed.data:
                if sub.id == keep_subscription_id:
                    continue
                try:
                    stripe.Subscription.cancel(sub.id)
                    logger.info(
                        "Cancelled duplicate Stripe subscription %s (kept %s)",
                        sub.id,
                        keep_subscription_id,
                    )
                except stripe.error.StripeError as exc:
                    logger.warning(
                        "Failed to cancel duplicate subscription %s: %s",
                        sub.id,
                        exc,
                    )

    @staticmethod
    async def _resolve_billable_stripe_subscription_id(
        db: AsyncSession,
        subscription: Optional[Subscription],
    ) -> Optional[str]:
        """Return sub_... id when the org already has a paid Stripe subscription to upgrade in place."""
        if not subscription:
            return None
        if subscription.status not in BillingService.UPGRADE_IN_PLACE_STATUSES:
            return None

        sid = subscription.stripe_subscription_id or ""
        if BillingService._is_stripe_billed_subscription(sid):
            return sid

        return await BillingService.resolve_stripe_subscription_id(db, subscription)

    @staticmethod
    async def upgrade_subscription_plan(
        db: AsyncSession,
        subscription: Subscription,
        new_plan_type: PlanType,
        seat_quantity: int,
        proration_date: Optional[int] = None,
    ) -> tuple[Subscription, int]:
        """
        Change plan on the existing Stripe subscription (prorated, charged immediately).
        Returns (subscription, amount_charged_cents).
        """
        stripe_sub_id = await BillingService._resolve_billable_stripe_subscription_id(
            db, subscription
        )
        if not stripe_sub_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No active Stripe subscription to upgrade",
            )

        if new_plan_type == subscription.plan_type:
            return subscription, 0

        proration_ts = proration_date or int(datetime.now(tz=timezone.utc).timestamp())

        try:
            stripe_sub = stripe.Subscription.retrieve(stripe_sub_id)
            modify_items = BillingService._build_plan_change_modify_items(
                stripe_sub,
                new_plan_type,
                seat_quantity,
            )
            updated_sub = stripe.Subscription.modify(
                stripe_sub_id,
                items=modify_items,
                proration_behavior="always_invoice",
                payment_behavior="error_if_incomplete",
                proration_date=proration_ts,
                metadata={
                    "organization_id": str(subscription.organization_id),
                    "plan_type": new_plan_type.value,
                },
                expand=["latest_invoice"],
            )
        except stripe.error.StripeError as exc:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"Payment failed: {str(exc)}",
            ) from exc

        amount_charged_cents = 0
        latest_invoice = updated_sub.get("latest_invoice")
        if isinstance(latest_invoice, dict):
            amount_charged_cents = int(
                latest_invoice.get("amount_paid")
                or latest_invoice.get("amount_due")
                or 0
            )
        elif latest_invoice:
            try:
                inv = stripe.Invoice.retrieve(str(latest_invoice))
                amount_charged_cents = int(inv.get("amount_paid") or inv.get("amount_due") or 0)
            except stripe.error.StripeError:
                amount_charged_cents = 0

        await BillingService._cancel_duplicate_stripe_subscriptions(
            subscription.stripe_customer_id,
            stripe_sub_id,
        )

        synced = await BillingService.sync_subscription_from_stripe(
            db,
            subscription.organization_id,
            stripe_sub_id,
            stripe_customer_id=subscription.stripe_customer_id,
            plan_type_hint=new_plan_type,
        )
        return synced, amount_charged_cents

    @staticmethod
    async def get_checkout_session(
        db: AsyncSession,
        organization: Organization,
        subscription: Optional[Subscription],
        plan_type: PlanType,
        seat_quantity: int,
        success_url: str,
        cancel_url: str,
    ) -> dict:
        """Start Stripe Checkout for first paid subscription (trial / no active sub)."""
        stripe_sub_id = await BillingService._resolve_billable_stripe_subscription_id(
            db, subscription
        )

        if stripe_sub_id and subscription:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Use upgrade preview to change an active subscription",
            )

        price_id = BillingService.PLAN_PRICE_MAP.get(plan_type)
        if not price_id and plan_type != PlanType.ENTERPRISE:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid plan type",
            )

        limits = settings.get_plan_limits(plan_type.value)
        if plan_type == PlanType.ENTERPRISE:
            seat_quantity = max(
                seat_quantity, limits["min"]
            )
        else:
            seat_quantity = max(seat_quantity, limits["min"])

        line_items = BillingService._build_checkout_line_items(plan_type, seat_quantity)

        try:
            metadata = {
                "organization_id": str(organization.id),
                "plan_type": plan_type.value,
                "seat_quantity": str(seat_quantity),
            }
            session = stripe.checkout.Session.create(
                customer=organization.owner.stripe_customer_id,
                payment_method_types=["card"],
                line_items=line_items,
                mode="subscription",
                success_url=success_url,
                cancel_url=cancel_url,
                metadata=metadata,
                subscription_data={"metadata": metadata},
            )
            return {
                "session_id": session.id,
                "url": session.url,
                "upgraded": False,
                "seat_quantity": seat_quantity,
            }
        except stripe.error.StripeError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to create checkout session: {str(e)}",
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
