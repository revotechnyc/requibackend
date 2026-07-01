"""Stripe coupons and promotion codes for platform admin / sales."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import stripe
from fastapi import HTTPException, status

from app.core.config import settings
from app.db.models import PlanType
from app.services.billing import BillingService

stripe.api_key = settings.stripe_secret_key


def _stripe_ts_to_iso(ts: Optional[int]) -> Optional[str]:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _product_ids_for_plans(plan_types: list[PlanType]) -> list[str]:
    product_ids: set[str] = set()
    for plan_type in plan_types:
        price_id = BillingService.PLAN_PRICE_MAP.get(plan_type)
        if not price_id:
            continue
        price = stripe.Price.retrieve(price_id)
        product_id = price.get("product") if isinstance(price, dict) else price.product
        if isinstance(product_id, dict):
            product_id = product_id.get("id")
        if product_id:
            product_ids.add(str(product_id))
    return sorted(product_ids)


def _serialize_promotion_code(promo: Any) -> dict:
    coupon = promo.coupon if hasattr(promo, "coupon") else promo.get("coupon")
    if coupon and not isinstance(coupon, dict):
        coupon = coupon.to_dict() if hasattr(coupon, "to_dict") else dict(coupon)

    discount_label = "—"
    if coupon:
        if coupon.get("percent_off") is not None:
            discount_label = f"{coupon['percent_off']}% off"
        elif coupon.get("amount_off") is not None:
            currency = (coupon.get("currency") or "usd").upper()
            amount = coupon["amount_off"] / 100
            discount_label = f"{currency} {amount:,.2f} off"

    metadata = promo.metadata if hasattr(promo, "metadata") else promo.get("metadata") or {}
    plans = metadata.get("plan_types") or "all"

    return {
        "id": promo.id,
        "code": promo.code,
        "active": bool(promo.active),
        "coupon_id": coupon.get("id") if coupon else None,
        "discount_label": discount_label,
        "duration": coupon.get("duration") if coupon else None,
        "duration_in_months": coupon.get("duration_in_months") if coupon else None,
        "max_redemptions": promo.max_redemptions,
        "times_redeemed": promo.times_redeemed or 0,
        "expires_at": _stripe_ts_to_iso(promo.expires_at),
        "created_at": _stripe_ts_to_iso(promo.created),
        "plan_types": plans,
    }


class StripePromotionsService:
    @staticmethod
    def list_promotion_codes(*, limit: int = 100) -> list[dict]:
        if not settings.stripe_secret_key:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Stripe is not configured",
            )
        try:
            result = stripe.PromotionCode.list(limit=limit, expand=["data.coupon"])
            codes = result.data if hasattr(result, "data") else result.get("data", [])
            return [_serialize_promotion_code(item) for item in codes]
        except stripe.error.StripeError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to list promotion codes: {exc.user_message or str(exc)}",
            ) from exc

    @staticmethod
    def create_promotion_code(
        *,
        code: str,
        discount_type: str,
        discount_value: float,
        duration: str = "once",
        duration_in_months: Optional[int] = None,
        max_redemptions: Optional[int] = None,
        expires_at: Optional[datetime] = None,
        plan_types: Optional[list[str]] = None,
        created_by_email: Optional[str] = None,
    ) -> dict:
        if not settings.stripe_secret_key:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Stripe is not configured",
            )

        normalized_code = code.strip().upper()
        if len(normalized_code) < 3:
            raise HTTPException(status_code=400, detail="Promo code must be at least 3 characters")

        coupon_params: dict[str, Any] = {
            "duration": duration,
            "name": f"Requi promo {normalized_code}",
        }
        if discount_type == "percent":
            if discount_value <= 0 or discount_value > 100:
                raise HTTPException(status_code=400, detail="Percent discount must be between 1 and 100")
            coupon_params["percent_off"] = discount_value
        elif discount_type == "amount":
            if discount_value <= 0:
                raise HTTPException(status_code=400, detail="Amount discount must be greater than zero")
            coupon_params["amount_off"] = int(round(discount_value * 100))
            coupon_params["currency"] = "usd"
        else:
            raise HTTPException(status_code=400, detail="discount_type must be percent or amount")

        if duration == "repeating":
            if not duration_in_months or duration_in_months < 1:
                raise HTTPException(
                    status_code=400,
                    detail="duration_in_months is required for repeating coupons",
                )
            coupon_params["duration_in_months"] = duration_in_months

        selected_plans: list[PlanType] = []
        if plan_types:
            for raw in plan_types:
                try:
                    selected_plans.append(PlanType(raw.strip().lower()))
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=f"Invalid plan type: {raw}") from exc
            product_ids = _product_ids_for_plans(selected_plans)
            if product_ids:
                coupon_params["applies_to"] = {"products": product_ids}

        promo_metadata: dict[str, str] = {
            "source": "requi_admin",
            "plan_types": ",".join(p.value for p in selected_plans) if selected_plans else "all",
        }
        if created_by_email:
            promo_metadata["created_by"] = created_by_email

        try:
            coupon = stripe.Coupon.create(**coupon_params)
            promo_params: dict[str, Any] = {
                "coupon": coupon.id,
                "code": normalized_code,
                "metadata": promo_metadata,
            }
            if max_redemptions is not None and max_redemptions > 0:
                promo_params["max_redemptions"] = max_redemptions
            if expires_at:
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                promo_params["expires_at"] = int(expires_at.timestamp())

            promo = stripe.PromotionCode.create(**promo_params)
            expanded = stripe.PromotionCode.retrieve(promo.id, expand=["coupon"])
            return _serialize_promotion_code(expanded)
        except stripe.error.StripeError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to create promotion code: {exc.user_message or str(exc)}",
            ) from exc

    @staticmethod
    def lookup_for_checkout(code: str, plan_type: PlanType) -> dict:
        """Resolve a customer-facing code for Stripe Checkout (raises if invalid)."""
        if not settings.stripe_secret_key:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Stripe is not configured",
            )

        normalized = code.strip().upper()
        if len(normalized) < 3:
            raise HTTPException(status_code=400, detail="Invalid promotion code")

        try:
            result = stripe.PromotionCode.list(
                code=normalized,
                active=True,
                limit=1,
                expand=["data.coupon"],
            )
            promos = result.data if hasattr(result, "data") else result.get("data", [])
            if not promos:
                raise HTTPException(status_code=400, detail="Invalid or expired promotion code")

            promo = promos[0]
            if not promo.active:
                raise HTTPException(status_code=400, detail="Promotion code is not active")

            now_ts = int(datetime.now(tz=timezone.utc).timestamp())
            if promo.expires_at and promo.expires_at < now_ts:
                raise HTTPException(status_code=400, detail="Promotion code has expired")

            if promo.max_redemptions and (promo.times_redeemed or 0) >= promo.max_redemptions:
                raise HTTPException(
                    status_code=400,
                    detail="Promotion code has reached its redemption limit",
                )

            metadata = promo.metadata if hasattr(promo, "metadata") else promo.get("metadata") or {}
            plan_types_raw = metadata.get("plan_types") or "all"
            if plan_types_raw and plan_types_raw != "all":
                allowed = {p.strip().lower() for p in str(plan_types_raw).split(",") if p.strip()}
                if plan_type.value not in allowed:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Promotion code is not valid for the {plan_type.value} plan",
                    )

            coupon = promo.coupon
            if coupon and not isinstance(coupon, dict):
                coupon = coupon.to_dict() if hasattr(coupon, "to_dict") else dict(coupon)
            applies_to = (coupon or {}).get("applies_to") or {}
            restricted_products = applies_to.get("products") or []
            if restricted_products:
                plan_product_ids = _product_ids_for_plans([plan_type])
                if plan_product_ids and not set(plan_product_ids).intersection(restricted_products):
                    raise HTTPException(
                        status_code=400,
                        detail="Promotion code is not valid for this plan",
                    )

            payload = _serialize_promotion_code(promo)
            payload["id"] = promo.id
            return payload
        except HTTPException:
            raise
        except stripe.error.StripeError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Could not validate promotion code: {exc.user_message or str(exc)}",
            ) from exc

    @staticmethod
    def deactivate_promotion_code(promotion_code_id: str) -> dict:
        if not settings.stripe_secret_key:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Stripe is not configured",
            )
        try:
            promo = stripe.PromotionCode.modify(promotion_code_id, active=False)
            expanded = stripe.PromotionCode.retrieve(promo.id, expand=["coupon"])
            return _serialize_promotion_code(expanded)
        except stripe.error.StripeError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to deactivate promotion code: {exc.user_message or str(exc)}",
            ) from exc
