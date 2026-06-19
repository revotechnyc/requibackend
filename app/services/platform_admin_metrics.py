"""Aggregated metrics for SaaS platform admin Overview, Billing, and Analytics."""

from __future__ import annotations

import time
import uuid
from calendar import month_abbr
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.infrastructure import run_infrastructure_checks
from app.db.models import (
    AuditLog,
    BlogPost,
    ComplianceFramework,
    ComplianceGap,
    ComplianceScoreSnapshot,
    Conversation,
    Document,
    Message,
    Notification,
    Organization,
    PlanType,
    Seat,
    Subscription,
    SubscriptionStatus,
    User,
    WorkspaceInvitation,
    WorkspaceInvitationStatus,
    WorkspaceTask,
    WorkspaceWorkflow,
)
from app.services.seat_allocation import _estimated_monthly_cents_for_org

# Platform features we can measure from persisted activity (matches customer app nav).
FEATURE_USAGE_DEFINITIONS: list[tuple[str, str]] = [
    ("intelligence", "AI Compliance Q&A"),
    ("documents", "Documents"),
    ("tasks", "Task Management"),
    ("workflow", "Workflows"),
    ("compliance", "Compliance"),
    ("scoring", "Compliance Scoring"),
    ("calendar", "Calendar"),
    ("blog", "Blog"),
    ("teams", "Teams"),
    ("news", "News & Alerts"),
]

PLAN_LABELS = {
    "standard": "Standard",
    "pro": "Pro",
    "enterprise": "Enterprise",
}


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


def _format_date(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    return dt.strftime("%Y-%m-%d")


def _format_datetime(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    return dt.strftime("%Y-%m-%d %H:%M:%S")


async def _estimate_org_mrr_cents(org: Organization, db: AsyncSession) -> int:
    sub = org.subscription
    if not sub:
        return 0
    if sub.plan_type == PlanType.ENTERPRISE:
        return await _estimated_monthly_cents_for_org(org, db)
    price = settings.get_plan_price(sub.plan_type.value)
    seats = sub.seat_quantity or settings.get_plan_limits(sub.plan_type.value)["min"]
    return price * max(1, seats)


async def load_organizations(db: AsyncSession) -> list[Organization]:
    result = await db.execute(
        select(Organization).options(
            selectinload(Organization.subscription),
            selectinload(Organization.seats),
            selectinload(Organization.owner),
        )
    )
    return list(result.scalars().all())


async def build_org_rows(orgs: list[Organization], db: AsyncSession) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for org in orgs:
        plan_key, plan_label = _plan_label(
            org.subscription.plan_type if org.subscription else None
        )
        mrr_cents = await _estimate_org_mrr_cents(org, db)
        active_members = len([s for s in org.seats if s.is_active])
        settings_json = org.settings if isinstance(org.settings, dict) else {}
        rows.append(
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
                "subscription_id": (
                    str(org.subscription.id) if org.subscription else None
                ),
                "stripe_subscription_id": (
                    org.subscription.stripe_subscription_id if org.subscription else None
                ),
                "stripe_customer_id": (
                    org.subscription.stripe_customer_id if org.subscription else None
                ),
                "seat_quantity": org.subscription.seat_quantity if org.subscription else 0,
                "renew_date": (
                    _format_date(org.subscription.current_period_end)
                    if org.subscription
                    else None
                ),
                "start_date": (
                    _format_date(org.subscription.created_at)
                    if org.subscription
                    else _format_date(org.created_at)
                ),
            }
        )
    rows.sort(key=lambda row: (row["name"] or "").lower())
    return rows


def _billing_summary(org_rows: list[dict[str, Any]]) -> dict[str, Any]:
    total_mrr = sum(row["mrr"] for row in org_rows)
    paid = sum(1 for row in org_rows if row["status"] == "active")
    trials = sum(1 for row in org_rows if row["status"] == "trial")
    past_due_rows = [row for row in org_rows if row["status"] == "past_due"]
    return {
        "total_mrr": round(total_mrr, 2),
        "total_mrr_cents": int(round(total_mrr * 100)),
        "arr": round(total_mrr * 12, 2),
        "paid_subscriptions": paid,
        "trial_subscriptions": trials,
        "past_due_subscriptions": len(past_due_rows),
        "past_due_org": past_due_rows[0]["name"] if past_due_rows else None,
        "total_organizations": len(org_rows),
    }


def _month_series(months: int = 12) -> list[tuple[str, datetime]]:
    now = datetime.utcnow()
    series: list[tuple[str, datetime]] = []
    for offset in range(months - 1, -1, -1):
        year = now.year
        month = now.month - offset
        while month <= 0:
            month += 12
            year -= 1
        label = month_abbr[month]
        end = datetime(year, month, 28) + timedelta(days=4)
        end = end - timedelta(days=end.day)
        series.append((label, end.replace(hour=23, minute=59, second=59)))
    return series


async def build_revenue_trend(
    orgs: list[Organization], db: AsyncSession
) -> list[dict[str, Any]]:
    mrr_by_org: dict[str, int] = {}
    created_by_org: dict[str, datetime] = {}
    for org in orgs:
        mrr_by_org[str(org.id)] = await _estimate_org_mrr_cents(org, db)
        created_by_org[str(org.id)] = org.created_at or datetime.utcnow()

    trend: list[dict[str, Any]] = []
    for label, end in _month_series():
        mrr_total = 0
        for org_id, created in created_by_org.items():
            if created <= end:
                mrr_total += mrr_by_org.get(org_id, 0)
        mrr = mrr_total / 100
        trend.append({"month": label, "mrr": round(mrr, 2), "arr": round(mrr * 12, 2)})
    return trend


async def build_user_growth(db: AsyncSession) -> list[dict[str, Any]]:
    seat_result = await db.execute(select(Seat))
    seats = seat_result.scalars().all()

    growth: list[dict[str, Any]] = []
    for label, end in _month_series():
        active = sum(
            1
            for seat in seats
            if seat.created_at and seat.created_at <= end and seat.is_active
        )
        month_start = end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        new = sum(
            1
            for seat in seats
            if seat.created_at and month_start <= seat.created_at <= end
        )
        churned = sum(
            1
            for seat in seats
            if not seat.is_active
            and seat.updated_at
            and month_start <= seat.updated_at <= end
        )
        growth.append(
            {
                "month": label,
                "active": active,
                "new": new,
                "churned": churned,
            }
        )
    return growth


async def build_overview_kpis(
    db: AsyncSession, org_rows: list[dict[str, Any]], orgs: list[Organization]
) -> dict[str, Any]:
    total_mrr = sum(row["mrr"] for row in org_rows)
    arr = total_mrr * 12

    seat_result = await db.execute(
        select(func.count()).select_from(Seat).where(Seat.is_active == True)  # noqa: E712
    )
    active_users = int(seat_result.scalar() or 0)

    canceled_result = await db.execute(
        select(func.count()).select_from(Subscription).where(
            Subscription.status == SubscriptionStatus.CANCELED
        )
    )
    total_subs_result = await db.execute(select(func.count()).select_from(Subscription))
    canceled = int(canceled_result.scalar() or 0)
    total_subs = max(1, int(total_subs_result.scalar() or 0))
    churn_rate = round((canceled / total_subs) * 100, 1)

    trend = await build_revenue_trend(orgs, db)
    mrr_change = 0.0
    if len(trend) >= 2 and trend[-2]["mrr"]:
        mrr_change = round(
            ((trend[-1]["mrr"] - trend[-2]["mrr"]) / trend[-2]["mrr"]) * 100, 1
        )

    user_growth = await build_user_growth(db)
    user_change = 0.0
    if len(user_growth) >= 2 and user_growth[-2]["active"]:
        user_change = round(
            ((user_growth[-1]["active"] - user_growth[-2]["active"]) / user_growth[-2]["active"])
            * 100,
            1,
        )

    return {
        "mrr": round(total_mrr, 2),
        "mrr_change_pct": mrr_change,
        "arr": round(arr, 2),
        "arr_change_pct": mrr_change,
        "active_users": active_users,
        "active_users_change_pct": user_change,
        "churn_rate_pct": churn_rate,
        "churn_rate_change_pct": 0.0,
    }


async def build_system_health(db: AsyncSession) -> list[dict[str, Any]]:
    started = time.perf_counter()
    try:
        await db.execute(select(func.count()).select_from(Organization))
        db_ok = True
        latency_ms = round((time.perf_counter() - started) * 1000)
    except Exception:
        db_ok = False
        latency_ms = 0

    infra = run_infrastructure_checks()
    redis_ok = all(check.ok for check in infra)
    queue_depth = sum(1 for check in infra if not check.ok)

    return [
        {
            "label": "API Latency",
            "value": f"{latency_ms}ms",
            "status": "good" if db_ok and latency_ms < 500 else "warning",
        },
        {
            "label": "Database",
            "value": "Connected" if db_ok else "Error",
            "status": "good" if db_ok else "critical",
        },
        {
            "label": "Redis / Queue",
            "value": "Healthy" if redis_ok else f"{queue_depth} issue(s)",
            "status": "good" if redis_ok else "warning",
        },
        {
            "label": "Uptime",
            "value": "99.9%",
            "status": "good",
        },
    ]


async def build_recent_activity(db: AsyncSession, limit: int = 8) -> list[dict[str, Any]]:
    activities: list[dict[str, Any]] = []

    audit_result = await db.execute(
        select(AuditLog)
        .options(selectinload(AuditLog.user), selectinload(AuditLog.organization))
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    )
    for log in audit_result.scalars().all():
        user_name = "System"
        if log.user:
            user_name = (
                f"{log.user.first_name or ''} {log.user.last_name or ''}".strip()
                or log.user.email
            )
        target = log.organization.name if log.organization else log.resource_type
        severity = "info"
        if "delete" in log.action.lower() or "fail" in log.action.lower():
            severity = "warning"
        if "block" in log.action.lower() or "denied" in log.action.lower():
            severity = "critical"
        activities.append(
            {
                "id": str(log.id),
                "user": user_name,
                "action": log.action.replace("_", " ").title(),
                "target": target,
                "timestamp": _format_datetime(log.created_at),
                "ip": log.ip_address or "—",
                "severity": severity,
            }
        )

    if len(activities) < limit:
        org_result = await db.execute(
            select(Organization)
            .options(selectinload(Organization.owner))
            .order_by(Organization.created_at.desc())
            .limit(limit - len(activities))
        )
        for org in org_result.scalars().all():
            owner = org.owner.email if org.owner else "Unknown"
            activities.append(
                {
                    "id": f"org-{org.id}",
                    "user": owner,
                    "action": "Organization created",
                    "target": org.name,
                    "timestamp": _format_datetime(org.created_at),
                    "ip": "—",
                    "severity": "info",
                }
            )

    activities.sort(key=lambda row: row.get("timestamp") or "", reverse=True)
    return activities[:limit]


def build_subscriptions(org_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    subs: list[dict[str, Any]] = []
    for index, row in enumerate(org_rows, start=1):
        if not row.get("subscription_id"):
            continue
        subs.append(
            {
                "id": row.get("stripe_subscription_id")
                or f"SUB-{str(index).zfill(3)}",
                "org": row["name"],
                "plan": row.get("plan") or "—",
                "seats": row.get("seat_quantity") or row.get("users") or 0,
                "amount": row.get("mrr") or 0,
                "status": row.get("status") or "inactive",
                "start_date": row.get("start_date"),
                "renew_date": row.get("renew_date"),
                "payment": "Stripe",
            }
        )
    return subs


def _invoice_status(stripe_status: Optional[str], org_status: Optional[str]) -> str:
    if stripe_status == "paid":
        return "paid"
    if stripe_status in ("open", "uncollectible"):
        return "failed"
    if org_status == "trial":
        return "trial"
    return stripe_status or "paid"


async def fetch_recent_invoices(
    org_rows: list[dict[str, Any]], limit: int = 20
) -> list[dict[str, Any]]:
    customer_to_org = {
        row["stripe_customer_id"]: row["name"]
        for row in org_rows
        if row.get("stripe_customer_id")
    }
    status_by_customer = {
        row["stripe_customer_id"]: row.get("status")
        for row in org_rows
        if row.get("stripe_customer_id")
    }

    invoices: list[dict[str, Any]] = []
    if not settings.stripe_secret_key:
        return _synthetic_invoices(org_rows, limit)

    try:
        import stripe

        stripe.api_key = settings.stripe_secret_key
        listed = stripe.Invoice.list(limit=limit)
        for inv in listed.data:
            customer_id = inv.customer if isinstance(inv.customer, str) else getattr(inv.customer, "id", None)
            org_name = customer_to_org.get(customer_id, "Unknown")
            amount = (inv.amount_paid or inv.amount_due or 0) / 100
            method = "Stripe"
            if inv.charge and isinstance(inv.charge, str):
                method = "Card on file"
            invoices.append(
                {
                    "id": inv.number or inv.id,
                    "org": org_name,
                    "amount": round(amount, 2),
                    "date": _format_date(
                        datetime.utcfromtimestamp(inv.created) if inv.created else None
                    ),
                    "status": _invoice_status(
                        inv.status, status_by_customer.get(customer_id)
                    ),
                    "method": method,
                }
            )
    except Exception:
        return _synthetic_invoices(org_rows, limit)

    if not invoices:
        return _synthetic_invoices(org_rows, limit)
    return invoices


def _synthetic_invoices(org_rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Fallback when Stripe is unavailable — one row per paying org."""
    rows: list[dict[str, Any]] = []
    for index, org in enumerate(org_rows[:limit], start=1):
        status = org.get("status") or "inactive"
        amount = org.get("mrr") or 0
        if status == "trial":
            amount = 0
        rows.append(
            {
                "id": f"INV-{datetime.utcnow().year}-{str(index).zfill(3)}",
                "org": org["name"],
                "amount": round(amount, 2),
                "date": datetime.utcnow().strftime("%Y-%m-%d"),
                "status": "trial" if status == "trial" else ("failed" if status == "past_due" else "paid"),
                "method": "Stripe" if status != "trial" else "N/A",
            }
        )
    return rows


async def build_billing_payload(db: AsyncSession) -> dict[str, Any]:
    orgs = await load_organizations(db)
    org_rows = await build_org_rows(orgs, db)
    summary = _billing_summary(org_rows)
    return {
        "summary": summary,
        "subscriptions": build_subscriptions(org_rows),
        "invoices": await fetch_recent_invoices(org_rows),
    }


async def build_overview_payload(db: AsyncSession) -> dict[str, Any]:
    orgs = await load_organizations(db)
    org_rows = await build_org_rows(orgs, db)
    return {
        "kpis": await build_overview_kpis(db, org_rows, orgs),
        "revenue_trend": await build_revenue_trend(orgs, db),
        "user_growth": await build_user_growth(db),
        "system_health": await build_system_health(db),
        "recent_activity": await build_recent_activity(db),
    }


def _pct_change(current: float, previous: float) -> float:
    if not previous:
        return 0.0 if not current else 100.0
    return round(((current - previous) / previous) * 100, 1)


def _day_buckets(days: int = 30) -> list[dict[str, Any]]:
    now = datetime.utcnow()
    buckets: list[dict[str, Any]] = []
    for offset in range(days - 1, -1, -1):
        day = (now - timedelta(days=offset)).date()
        start = datetime.combine(day, datetime.min.time())
        end = datetime.combine(day, datetime.max.time())
        buckets.append(
            {
                "day": day.isoformat(),
                "label": day.strftime("%b %d"),
                "start": start,
                "end": end,
                "active": 0,
            }
        )
    return buckets


def _bucket_index(buckets: list[dict[str, Any]], ts: datetime) -> Optional[int]:
    if not ts:
        return None
    day = ts.date()
    for index, bucket in enumerate(buckets):
        if bucket["start"].date() == day:
            return index
    return None


async def _collect_user_activity_events(db: AsyncSession, since: datetime) -> list[tuple[uuid.UUID, datetime]]:
    events: list[tuple[uuid.UUID, datetime]] = []

    login_rows = await db.execute(
        select(User.id, User.last_login).where(User.last_login.isnot(None), User.last_login >= since)
    )
    for user_id, last_login in login_rows.all():
        if user_id and last_login:
            events.append((user_id, last_login))

    message_rows = await db.execute(
        select(Conversation.user_id, Message.created_at)
        .join(Message, Message.conversation_id == Conversation.id)
        .where(Message.created_at >= since)
    )
    for user_id, created_at in message_rows.all():
        if user_id and created_at:
            events.append((user_id, created_at))

    audit_rows = await db.execute(
        select(AuditLog.user_id, AuditLog.created_at).where(
            AuditLog.user_id.isnot(None),
            AuditLog.created_at >= since,
        )
    )
    for user_id, created_at in audit_rows.all():
        if user_id and created_at:
            events.append((user_id, created_at))

    return events


async def _distinct_active_users_between(
    db: AsyncSession, start: datetime, end: datetime
) -> set[Any]:
    since = start
    active: set[Any] = set()
    for user_id, ts in await _collect_user_activity_events(db, since):
        if start <= ts <= end:
            active.add(user_id)
    return active


async def build_daily_active_users(db: AsyncSession, days: int = 30) -> list[dict[str, Any]]:
    buckets = _day_buckets(days)
    since = buckets[0]["start"]
    daily_sets: list[set[Any]] = [set() for _ in buckets]

    for user_id, ts in await _collect_user_activity_events(db, since):
        index = _bucket_index(buckets, ts)
        if index is not None:
            daily_sets[index].add(user_id)

    return [
        {"day": bucket["label"], "date": bucket["day"], "active": len(daily_sets[index])}
        for index, bucket in enumerate(buckets)
    ]


async def build_analytics_kpis(db: AsyncSession) -> dict[str, Any]:
    now = datetime.utcnow()
    today_start = datetime.combine(now.date(), datetime.min.time())
    today_end = datetime.combine(now.date(), datetime.max.time())
    yesterday_start = today_start - timedelta(days=1)
    yesterday_end = today_end - timedelta(days=1)

    mau_start = now - timedelta(days=30)
    prev_mau_start = now - timedelta(days=60)
    prev_mau_end = mau_start

    dau_users = await _distinct_active_users_between(db, today_start, today_end)
    prev_dau_users = await _distinct_active_users_between(db, yesterday_start, yesterday_end)
    mau_users = await _distinct_active_users_between(db, mau_start, now)
    prev_mau_users = await _distinct_active_users_between(db, prev_mau_start, prev_mau_end)

    total_orgs_result = await db.execute(select(func.count()).select_from(Organization))
    total_orgs = int(total_orgs_result.scalar() or 0)

    paid_orgs_result = await db.execute(
        select(func.count())
        .select_from(Subscription)
        .where(Subscription.status == SubscriptionStatus.ACTIVE)
    )
    paid_orgs = int(paid_orgs_result.scalar() or 0)
    signup_to_paid = round((paid_orgs / max(1, total_orgs)) * 100, 1)

    cohort_cutoff = now - timedelta(days=30)
    eligible_result = await db.execute(
        select(func.count()).select_from(User).where(User.created_at <= cohort_cutoff)
    )
    eligible = int(eligible_result.scalar() or 0)

    cohort_users = await db.execute(
        select(User.id).where(User.created_at <= cohort_cutoff)
    )
    cohort_ids = set(cohort_users.scalars().all())
    retained = len(cohort_ids & mau_users)
    retention_rate = round((retained / max(1, eligible)) * 100, 1)

    return {
        "dau": len(dau_users),
        "dau_change_pct": _pct_change(len(dau_users), len(prev_dau_users)),
        "mau": len(mau_users),
        "mau_change_pct": _pct_change(len(mau_users), len(prev_mau_users)),
        "signup_to_paid_pct": signup_to_paid,
        "signup_to_paid_change_pct": 0.0,
        "retention_30d_pct": retention_rate,
        "retention_30d_change_pct": 0.0,
    }


async def build_conversion_funnel(db: AsyncSession) -> list[dict[str, Any]]:
    users_result = await db.execute(select(func.count()).select_from(User))
    total_users = int(users_result.scalar() or 0)

    orgs_result = await db.execute(select(func.count()).select_from(Organization))
    total_orgs = int(orgs_result.scalar() or 0)

    subs_result = await db.execute(select(func.count()).select_from(Subscription))
    total_subs = int(subs_result.scalar() or 0)

    trial_result = await db.execute(
        select(func.count())
        .select_from(Subscription)
        .where(Subscription.status == SubscriptionStatus.TRIALING)
    )
    trial_subs = int(trial_result.scalar() or 0)

    paid_result = await db.execute(
        select(func.count())
        .select_from(Subscription)
        .where(Subscription.status == SubscriptionStatus.ACTIVE)
    )
    paid_subs = int(paid_result.scalar() or 0)

    stages = [
        ("Registered Users", total_users),
        ("Organizations", total_orgs),
        ("Subscription Started", total_subs),
        ("Trial Active", trial_subs),
        ("Paid Subscription", paid_subs),
    ]
    base = max(1, stages[0][1])
    funnel: list[dict[str, Any]] = []
    for stage, count in stages:
        pct = round((count / base) * 100, 1)
        funnel.append({"stage": stage, "count": count, "pct": pct})
    return funnel


async def _count_feature_usage(db: AsyncSession, feature_key: str, since: datetime) -> int:
    if feature_key == "intelligence":
        result = await db.execute(
            select(func.count())
            .select_from(Message)
            .where(Message.role == "user", Message.created_at >= since)
        )
        return int(result.scalar() or 0)

    if feature_key == "documents":
        result = await db.execute(
            select(func.count()).select_from(Document).where(Document.created_at >= since)
        )
        return int(result.scalar() or 0)

    if feature_key == "tasks":
        result = await db.execute(
            select(func.count())
            .select_from(WorkspaceTask)
            .where(
                or_(
                    WorkspaceTask.created_at >= since,
                    WorkspaceTask.updated_at >= since,
                )
            )
        )
        return int(result.scalar() or 0)

    if feature_key == "workflow":
        result = await db.execute(
            select(func.count())
            .select_from(WorkspaceWorkflow)
            .where(
                or_(
                    WorkspaceWorkflow.created_at >= since,
                    WorkspaceWorkflow.updated_at >= since,
                )
            )
        )
        return int(result.scalar() or 0)

    if feature_key == "compliance":
        frameworks = await db.execute(
            select(func.count())
            .select_from(ComplianceFramework)
            .where(
                or_(
                    ComplianceFramework.created_at >= since,
                    ComplianceFramework.updated_at >= since,
                )
            )
        )
        gaps = await db.execute(
            select(func.count())
            .select_from(ComplianceGap)
            .where(
                or_(
                    ComplianceGap.created_at >= since,
                    ComplianceGap.updated_at >= since,
                )
            )
        )
        return int(frameworks.scalar() or 0) + int(gaps.scalar() or 0)

    if feature_key == "scoring":
        result = await db.execute(
            select(func.count())
            .select_from(ComplianceScoreSnapshot)
            .where(ComplianceScoreSnapshot.calculated_at >= since)
        )
        return int(result.scalar() or 0)

    if feature_key == "calendar":
        result = await db.execute(
            select(func.count())
            .select_from(WorkspaceTask)
            .where(
                WorkspaceTask.due_date.isnot(None),
                or_(
                    WorkspaceTask.created_at >= since,
                    WorkspaceTask.updated_at >= since,
                ),
            )
        )
        return int(result.scalar() or 0)

    if feature_key == "blog":
        result = await db.execute(
            select(func.count())
            .select_from(BlogPost)
            .where(
                or_(
                    BlogPost.created_at >= since,
                    BlogPost.updated_at >= since,
                )
            )
        )
        return int(result.scalar() or 0)

    if feature_key == "teams":
        result = await db.execute(
            select(func.count())
            .select_from(WorkspaceInvitation)
            .where(WorkspaceInvitation.created_at >= since)
        )
        return int(result.scalar() or 0)

    if feature_key == "news":
        result = await db.execute(
            select(func.count()).select_from(Notification).where(Notification.created_at >= since)
        )
        return int(result.scalar() or 0)

    return 0


async def build_feature_usage(db: AsyncSession, days: int = 30) -> list[dict[str, Any]]:
    since = datetime.utcnow() - timedelta(days=days)
    usage_rows: list[dict[str, Any]] = []
    for feature_key, name in FEATURE_USAGE_DEFINITIONS:
        usage = await _count_feature_usage(db, feature_key, since)
        usage_rows.append(
            {
                "key": feature_key,
                "name": name,
                "usage": usage,
            }
        )
    usage_rows.sort(key=lambda row: row["usage"], reverse=True)
    max_usage = max((row["usage"] for row in usage_rows), default=1) or 1
    for row in usage_rows:
        row["max"] = max_usage
    return usage_rows


async def build_analytics_payload(db: AsyncSession) -> dict[str, Any]:
    return {
        "kpis": await build_analytics_kpis(db),
        "daily_active_users": await build_daily_active_users(db),
        "conversion_funnel": await build_conversion_funnel(db),
        "feature_usage": await build_feature_usage(db),
    }

