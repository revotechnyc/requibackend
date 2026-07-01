"""Platform admin — Stripe promotion codes for sales team."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.core.platform_admin_roles import can_manage_promotions
from app.core.platform_admin_security import get_current_platform_admin
from app.db.models import PlatformAdmin
from app.services.stripe_promotions_service import StripePromotionsService

router = APIRouter()


def _require_promotion_access(admin: PlatformAdmin) -> None:
    if not can_manage_promotions(admin.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to manage promotions",
        )


class PromotionCreateRequest(BaseModel):
    code: str
    discount_type: Literal["percent", "amount"]
    discount_value: float = Field(gt=0)
    duration: Literal["once", "repeating", "forever"] = "once"
    duration_in_months: Optional[int] = None
    max_redemptions: Optional[int] = Field(default=None, ge=1)
    expires_at: Optional[datetime] = None
    plan_types: Optional[list[str]] = None

    @field_validator("code")
    @classmethod
    def normalize_code(cls, v: str) -> str:
        cleaned = v.strip().upper()
        if not cleaned:
            raise ValueError("Promo code is required")
        return cleaned


@router.get("")
async def list_promotions(
    admin: PlatformAdmin = Depends(get_current_platform_admin),
):
    _require_promotion_access(admin)
    promotions = StripePromotionsService.list_promotion_codes()
    return {"promotions": promotions, "total": len(promotions)}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_promotion(
    body: PromotionCreateRequest,
    admin: PlatformAdmin = Depends(get_current_platform_admin),
):
    _require_promotion_access(admin)
    promotion = StripePromotionsService.create_promotion_code(
        code=body.code,
        discount_type=body.discount_type,
        discount_value=body.discount_value,
        duration=body.duration,
        duration_in_months=body.duration_in_months,
        max_redemptions=body.max_redemptions,
        expires_at=body.expires_at,
        plan_types=body.plan_types,
        created_by_email=admin.email,
    )
    return {
        "promotion": promotion,
        "message": "Promotion code created in Stripe",
    }


@router.delete("/{promotion_code_id}")
async def delete_promotion(
    promotion_code_id: str,
    admin: PlatformAdmin = Depends(get_current_platform_admin),
):
    """Deactivate a promotion code (Stripe does not hard-delete redeemed codes)."""
    _require_promotion_access(admin)
    promotion = StripePromotionsService.deactivate_promotion_code(promotion_code_id)
    return {
        "promotion": promotion,
        "message": "Promotion code deactivated",
    }
