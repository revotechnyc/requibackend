"""Per-member tab permissions (overrides role defaults when set on seat/invitation)."""

from __future__ import annotations

from typing import Any, Optional

from app.db.models import PlanType, UserRole
from app.services.enterprise_roles import has_full_access

# Local copy avoids circular import (permissions.py imports auth).
PLAN_FEATURES: dict[PlanType, list[str]] = {
    PlanType.STANDARD: [
        "intelligence",
        "documents",
        "blog",
        "settings",
    ],
    PlanType.PRO: [
        "intelligence",
        "dashboard",
        "workflow",
        "tasks",
        "compliance",
        "calendar",
        "documents",
        "news",
        "teams",
        "blog",
        "settings",
        "scoring",
        "frameworks",
        "evidence",
    ],
    PlanType.ENTERPRISE: [
        "intelligence",
        "dashboard",
        "workflow",
        "tasks",
        "compliance",
        "calendar",
        "documents",
        "news",
        "teams",
        "blog",
        "integrations",
        "settings",
        "admin",
        "audit_logs",
        "multiple_admins",
        "scoring",
        "frameworks",
        "evidence",
        "audit",
        "reporting",
        "ai_agents",
        "analytics",
    ],
}

MANAGEABLE_FEATURE_KEYS = (
    "intelligence",
    "dashboard",
    "workflow",
    "tasks",
    "compliance",
    "calendar",
    "documents",
    "news",
    "blog",
    "teams",
    "analytics",
    "integrations",
    "settings",
)

ROLE_FEATURE_ACCESS: dict[str, tuple[str, ...]] = {
    # Enterprise Admin + Admin: full module access (billing gated separately)
    "intelligence": ("enterprise_admin", "admin", "reviewer", "contributor", "analyst", "seo"),
    "dashboard": ("enterprise_admin", "admin", "reviewer", "contributor", "viewer", "analyst", "seo"),
    "workflow": ("enterprise_admin", "admin", "reviewer", "approver", "contributor", "viewer", "seo"),
    "tasks": ("enterprise_admin", "admin", "reviewer", "approver", "contributor", "viewer", "seo"),
    "compliance": ("enterprise_admin", "admin", "reviewer", "approver", "contributor", "viewer", "analyst", "seo"),
    "calendar": ("enterprise_admin", "admin", "reviewer", "contributor", "viewer", "seo"),
    "documents": ("enterprise_admin", "admin", "reviewer", "contributor", "seo"),
    "news": ("enterprise_admin", "admin", "reviewer", "contributor", "seo"),
    "blog": ("enterprise_admin", "admin", "seo"),
    "teams": ("enterprise_admin", "admin"),
    "analytics": ("enterprise_admin", "admin", "reviewer", "analyst"),
    "integrations": ("enterprise_admin", "admin"),
    "settings": ("enterprise_admin", "admin"),
}


def _plan_feature_set(plan: PlanType) -> set[str]:
    return set(PLAN_FEATURES.get(plan, []))


def role_default_allows(feature: str, role: UserRole) -> bool:
    if has_full_access(role):
        return True
    allowed = ROLE_FEATURE_ACCESS.get(feature)
    if not allowed:
        return True
    return role.value in allowed


def default_feature_permissions(plan: PlanType, role: UserRole) -> dict[str, bool]:
    plan_feats = _plan_feature_set(plan)
    return {
        key: key in plan_feats and role_default_allows(key, role)
        for key in MANAGEABLE_FEATURE_KEYS
    }


def role_default_permissions_snapshot(
    plan: PlanType,
    role: UserRole,
) -> dict[str, bool]:
    """Persisted permission map for a new invite (role defaults, sanitized for plan)."""
    return sanitize_permissions_payload(
        default_feature_permissions(plan, role),
        plan,
        role,
    )


def normalize_stored_permissions(raw: Any) -> Optional[dict[str, bool]]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return None
    out: dict[str, bool] = {}
    for key in MANAGEABLE_FEATURE_KEYS:
        if key in raw:
            out[key] = bool(raw[key])
    return out or None


def sanitize_permissions_payload(
    raw: Optional[dict[str, bool]],
    plan: PlanType,
    role: UserRole,
) -> dict[str, bool]:
    """Persisted map: only manageable keys; cannot enable features not on plan."""
    plan_feats = _plan_feature_set(plan)
    stored = normalize_stored_permissions(raw) or {}
    if has_full_access(role):
        return {key: key in plan_feats for key in MANAGEABLE_FEATURE_KEYS}
    result: dict[str, bool] = {}
    for key in MANAGEABLE_FEATURE_KEYS:
        if key not in plan_feats:
            result[key] = False
        else:
            result[key] = bool(stored.get(key, False))
    return result


def effective_feature_permissions(
    plan: PlanType,
    role: UserRole,
    stored: Any,
) -> dict[str, bool]:
    normalized = normalize_stored_permissions(stored)
    if has_full_access(role):
        return default_feature_permissions(plan, role)
    if normalized is not None:
        return sanitize_permissions_payload(normalized, plan, role)
    return default_feature_permissions(plan, role)


def member_can_access_feature(
    feature: str,
    plan: PlanType,
    role: UserRole,
    stored: Any,
) -> bool:
    if feature not in _plan_feature_set(plan):
        return False
    if has_full_access(role):
        return True
    perms = effective_feature_permissions(plan, role, stored)
    normalized = normalize_stored_permissions(stored)
    if plan == PlanType.ENTERPRISE and normalized is not None:
        if feature in perms:
            return perms[feature]
        if feature in MANAGEABLE_FEATURE_KEYS:
            return False
    if feature in perms:
        return perms[feature]
    return role_default_allows(feature, role)
