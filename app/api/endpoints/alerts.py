"""
Alerts API — v2.1
Requi Health compliance alerts system.
POST /v1/alerts  — Trigger alerts via Zapier/email
GET  /v1/alerts  — Fetch alerts for the workspace
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.endpoints.auth import get_current_active_user
from app.core.permissions import FeatureGate, PermissionChecker
from app.db.database import get_db
from app.db.models import Organization, PlanType, Seat, User

router = APIRouter()


class AlertType(str, Enum):
    COMPLIANCE_BREACH = "compliance_breach"
    TASK_OVERDUE = "task_overdue"
    DOCUMENT_EXPIRY = "document_expiry"
    AI_ANALYSIS_COMPLETE = "ai_analysis_complete"
    RISK_THRESHOLD = "risk_threshold"
    AUDIT_REMINDER = "audit_reminder"


class AlertSeverity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class AlertChannel(str, Enum):
    EMAIL = "email"
    IN_APP = "in_app"
    ZAPIER = "zapier"


# ============== Pydantic Models ==============

class AlertCreate(BaseModel):
    title: str
    message: str
    type: str  # AlertType value
    severity: str  # AlertSeverity value
    channels: List[str] = ["in_app"]  # AlertChannel values
    recipients: Optional[List[str]] = None  # email addresses
    metadata: Optional[dict] = None


class AlertResponse(BaseModel):
    id: str
    title: str
    message: str
    type: str
    severity: str
    status: str
    channels: List[str]
    recipients: List[str]
    metadata: Optional[dict]
    created_at: str
    sent_at: Optional[str] = None
    acknowledged_at: Optional[str] = None
    acknowledged_by: Optional[str] = None


# ============== In-memory store (replace with DB in production) ==============
ALERTS_STORE: List[dict] = []
ALERT_COUNTER = 0


def _next_id() -> int:
    global ALERT_COUNTER
    ALERT_COUNTER += 1
    return ALERT_COUNTER


# ============== Helpers ==============

async def _get_org(
    user: User,
    db: AsyncSession,
) -> Organization:
    result = await db.execute(
        select(Seat)
        .where(Seat.user_id == user.id, Seat.is_active == True)
    )
    seat = result.scalar_one_or_none()
    if not seat:
        raise HTTPException(status_code=403, detail="No active workspace")
    result = await db.execute(
        select(Organization).where(Organization.id == seat.organization_id)
    )
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return org


# ============== ENDPOINTS ==============

@router.post("/", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_alert(
    data: AlertCreate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    POST /v1/alerts
    Trigger compliance alerts via configured channels (email, in-app, Zapier).
    Required fields: title, message, type, severity.
    """
    org = await _get_org(current_user, db)

    # Validate type & severity
    try:
        alert_type = AlertType(data.type)
        severity = AlertSeverity(data.severity)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid type '{data.type}' or severity '{data.severity}'. "
                   f"Valid types: {[t.value for t in AlertType]}. "
                   f"Valid severities: {[s.value for s in AlertSeverity]}."
        )

    alert_id = _next_id()
    now = datetime.utcnow().isoformat()

    # Zapier Webhook Trigger
    zapier_webhook_url = None  # DEV: Populate from workspace settings in production
    if "zapier" in data.channels and zapier_webhook_url:
        # PLACEHOLDER: Integrate Zapier trigger here
        pass

    # Email dispatch
    email_service = None  # DEV: Connect email service here
    if "email" in data.channels and email_service and data.recipients:
        # PLACEHOLDER: Send email via SendGrid/AWS SES
        pass

    alert_record = {
        "id": str(alert_id),
        "workspace_id": str(org.id),
        "title": data.title,
        "message": data.message,
        "type": alert_type.value,
        "severity": severity.value,
        "status": "active",
        "channels": data.channels,
        "recipients": data.recipients or [],
        "metadata": data.metadata or {},
        "created_at": now,
        "sent_at": now if "email" in data.channels else None,
        "acknowledged_at": None,
        "acknowledged_by": None,
        "created_by": str(current_user.id),
    }
    ALERTS_STORE.append(alert_record)

    return {
        "alert": alert_record,
        "delivery_status": {
            "email": "queued" if "email" in data.channels else "skipped",
            "in_app": "delivered" if "in_app" in data.channels else "skipped",
            "zapier": "queued" if "zapier" in data.channels and zapier_webhook_url else "no_webhook",
        },
    }


@router.get("/", response_model=dict)
async def list_alerts(
    status: Optional[str] = None,
    severity: Optional[str] = None,
    alert_type: Optional[str] = None,
    limit: int = 50,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    GET /v1/alerts
    Fetch alerts for the workspace with optional filtering.
    Query params: status, severity, type, limit
    """
    org = await _get_org(current_user, db)
    org_id = str(org.id)

    alerts = [a for a in ALERTS_STORE if a.get("workspace_id") == org_id]

    if status:
        alerts = [a for a in alerts if a["status"] == status]
    if severity:
        alerts = [a for a in alerts if a["severity"] == severity]
    if alert_type:
        alerts = [a for a in alerts if a["type"] == alert_type]

    # Sort by created_at desc
    alerts.sort(key=lambda x: x["created_at"], reverse=True)
    alerts = alerts[:limit]

    # Count summaries
    total_active = len([a for a in ALERTS_STORE if a.get("workspace_id") == org_id and a["status"] == "active"])
    total_critical = len([a for a in ALERTS_STORE if a.get("workspace_id") == org_id and a["severity"] == "critical"])

    return {
        "alerts": alerts,
        "summary": {
            "total_active": total_active,
            "total_critical": total_critical,
            "returned": len(alerts),
        },
        "filters_applied": {
            "status": status,
            "severity": severity,
            "type": alert_type,
        },
    }


@router.patch("/{alert_id}/acknowledge", response_model=dict)
async def acknowledge_alert(
    alert_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark an alert as acknowledged."""
    org = await _get_org(current_user, db)

    for alert in ALERTS_STORE:
        if alert["id"] == alert_id and alert.get("workspace_id") == str(org.id):
            alert["status"] = "acknowledged"
            alert["acknowledged_at"] = datetime.utcnow().isoformat()
            alert["acknowledged_by"] = str(current_user.id)
            return {"alert": alert, "message": "Alert acknowledged"}

    raise HTTPException(status_code=404, detail="Alert not found")


@router.get("/settings/channels", response_model=dict)
async def get_alert_channels(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    GET /v1/alerts/settings/channels
    Returns configured alert channels for the workspace.
    """
    # PLACEHOLDER: Load from workspace settings
    return {
        "channels": {
            "email": {
                "enabled": False,  # DEV: Set True when email service connected
                "provider": None,  # DEV: "sendgrid" | "aws_ses" | "smtp"
                "configured_recipients": [],
            },
            "in_app": {
                "enabled": True,
            },
            "zapier": {
                "enabled": False,  # DEV: Set True when Zapier webhook configured
                "webhook_url": None,  # DEV: Populate from workspace settings
            },
        },
        "note": "Configure channels in workspace settings. DevOps: populate provider credentials via environment variables.",
    }
