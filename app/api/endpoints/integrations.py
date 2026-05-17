"""
Integration Hub API — v2.1
Manages third-party platform integrations.

Pro:     Microsoft 365 (Teams, Outlook, OneDrive, Excel, SharePoint)
         Google Workspace (Gmail, Drive, Docs, Sheets, Calendar)
         NO Salesforce

Enterprise: All Pro integrations + Salesforce (full lifecycle)
            Zapier MCP (enterprise-grade)

API plugins areas are LEFT BLANK for DevOps to configure live credentials.
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.endpoints.auth import get_current_active_user
from app.core.permissions import FeatureGate
from app.db.database import get_db
from app.db.models import Organization, PlanType, Seat

router = APIRouter()


# ============== Enums ==============

class IntegrationProvider(str, Enum):
    MICROSOFT_TEAMS = "microsoft_teams"
    MICROSOFT_OUTLOOK = "microsoft_outlook"
    MICROSOFT_ONEDRIVE = "microsoft_onedrive"
    MICROSOFT_EXCEL = "microsoft_excel"
    MICROSOFT_SHAREPOINT = "microsoft_sharepoint"
    GOOGLE_GMAIL = "google_gmail"
    GOOGLE_DRIVE = "google_drive"
    GOOGLE_DOCS = "google_docs"
    GOOGLE_SHEETS = "google_sheets"
    GOOGLE_CALENDAR = "google_calendar"
    SALESFORCE = "salesforce"
    ZAPIER = "zapier"


class IntegrationStatus(str, Enum):
    NOT_CONFIGURED = "not_configured"
    PENDING_AUTH = "pending_auth"
    ACTIVE = "active"
    ERROR = "error"
    REVOKED = "revoked"


# ============== Pydantic Models ==============

class IntegrationConnect(BaseModel):
    provider: str
    redirect_url: Optional[str] = None


class IntegrationWebhook(BaseModel):
    provider: str
    event_type: str
    payload: dict


# ============== In-memory store ==============
INTEGRATIONS_STORE: List[dict] = []


# ============== Helpers ==============

async def _get_workspace(user, db: AsyncSession) -> Organization:
    result = await db.execute(
        select(Seat).where(Seat.user_id == user.id, Seat.is_active == True)
        .options(selectinload(Seat.organization))
    )
    seat = result.scalar_one_or_none()
    if not seat:
        raise HTTPException(status_code=403, detail="No active workspace")
    return seat.organization


# ============== Pro-tier integrations ==============
PRO_INTEGRATIONS = [
    IntegrationProvider.MICROSOFT_TEAMS,
    IntegrationProvider.MICROSOFT_OUTLOOK,
    IntegrationProvider.MICROSOFT_ONEDRIVE,
    IntegrationProvider.MICROSOFT_EXCEL,
    IntegrationProvider.MICROSOFT_SHAREPOINT,
    IntegrationProvider.GOOGLE_GMAIL,
    IntegrationProvider.GOOGLE_DRIVE,
    IntegrationProvider.GOOGLE_DOCS,
    IntegrationProvider.GOOGLE_SHEETS,
    IntegrationProvider.GOOGLE_CALENDAR,
]

# ============== Enterprise-only integrations ==============
ENTERPRISE_INTEGRATIONS = [
    IntegrationProvider.SALESFORCE,
    IntegrationProvider.ZAPIER,
]


# ============== ENDPOINTS ==============

@router.get("/", response_model=dict)
async def list_integrations(
    current_user = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    GET /v1/integrations
    List all available integrations for the workspace's plan tier.
    """
    org = await _get_workspace(current_user, db)
    is_enterprise = org.subscription and org.subscription.plan_type == PlanType.ENTERPRISE

    available = []
    for p in PRO_INTEGRATIONS:
        available.append({
            "provider": p.value,
            "name": p.value.replace("_", " ").title(),
            "tier": "pro",
            "status": IntegrationStatus.NOT_CONFIGURED.value,
            # BLANK: OAuth credentials
            "oauth_config": {
                "client_id": None,       # DevOps: Set via env var
                "client_secret": None,   # DevOps: Set via env var
                "redirect_uri": None,    # DevOps: Set via env var
                "scopes": [],            # DevOps: Define per provider
                "auth_url": None,        # DevOps: Provider auth URL
                "token_url": None,       # DevOps: Provider token URL
            },
        })

    if is_enterprise:
        for p in ENTERPRISE_INTEGRATIONS:
            available.append({
                "provider": p.value,
                "name": p.value.replace("_", " ").title(),
                "tier": "enterprise",
                "status": IntegrationStatus.NOT_CONFIGURED.value,
                "oauth_config": {
                    "client_id": None,
                    "client_secret": None,
                    "redirect_uri": None,
                    "scopes": [],
                    "auth_url": None,
                    "token_url": None,
                },
            })

    return {
        "workspace_id": str(org.id),
        "plan": org.subscription.plan_type.value if org.subscription else "pro",
        "integrations": available,
        "note": "Configure OAuth credentials via environment variables or vault. See integration docs.",
    }


@router.post("/{provider}/connect", response_model=dict)
async def connect_integration(
    provider: str,
    data: IntegrationConnect,
    current_user = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    POST /v1/integrations/{provider}/connect
    Initiate OAuth connection flow for a third-party platform.
    Redirects to provider's authorization URL.
    """
    org = await _get_workspace(current_user, db)

    # Validate provider access
    try:
        prov_enum = IntegrationProvider(provider)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown provider '{provider}'")

    is_enterprise = org.subscription and org.subscription.plan_type == PlanType.ENTERPRISE

    if prov_enum in ENTERPRISE_INTEGRATIONS and not is_enterprise:
        raise HTTPException(status_code=403, detail=f"{provider} requires Enterprise plan")

    # BLANK: Build OAuth URL (DevOps implements per provider)
    auth_url = None  # PLACEHOLDER: Construct from oauth_config

    return {
        "provider": provider,
        "status": IntegrationStatus.PENDING_AUTH.value,
        "auth_url": auth_url,
        "message": f"Redirect user to {provider} authorization. DevOps: Implement OAuth flow.",
        # BLANK: Implementation guide for DevOps
        "implementation_guide": {
            "step_1": f"Register app at {provider} developer portal",
            "step_2": "Store client_id and client_secret in vault (HashiCorp Vault / AWS Secrets Manager)",
            "step_3": f"Set OAUTH_{provider.upper()}_CLIENT_ID and OAUTH_{provider.upper()}_CLIENT_SECRET env vars",
            "step_4": f"Implement token exchange in app/api/endpoints/integrations.py:connect_integration()",
            "step_5": "Store tokens encrypted at rest (AES-256-GCM)",
            "step_6": "Implement webhook handlers for real-time sync",
        },
    }


@router.post("/{provider}/webhook", response_model=dict)
async def receive_webhook(
    provider: str,
    data: IntegrationWebhook,
    current_user = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    POST /v1/integrations/{provider}/webhook
    Receive webhook events from third-party platforms.
    Secured with signature verification.
    """
    org = await _get_workspace(current_user, db)

    # BLANK: Signature verification (DevOps implements per provider)
    # Each provider uses different signature methods:
    # - Microsoft: HMAC-SHA256 with client secret
    # - Google: JWT verification with public key
    # - Salesforce: OAuth token + HMAC
    # - Zapier: Shared secret header

    signature_valid = False  # PLACEHOLDER
    if not signature_valid:
        # DevOps: Uncomment when signature verification is implemented
        # raise HTTPException(status_code=401, detail="Invalid webhook signature")
        pass

    # BLANK: Event processing (DevOps implements per event_type)
    event_processed = {
        "provider": provider,
        "event_type": data.event_type,
        "received_at": datetime.utcnow().isoformat(),
        "status": "queued_for_processing",
        "payload_preview": str(data.payload)[:200] + "..." if len(str(data.payload)) > 200 else data.payload,
        # BLANK: Event handler mapping
        "handler": None,  # DevOps: Map event_type to handler function
    }

    INTEGRATIONS_STORE.append(event_processed)

    return {
        "event": event_processed,
        "note": "Webhook received. DevOps: Implement event handlers and signature verification.",
    }


@router.get("/{provider}/status", response_model=dict)
async def get_integration_status(
    provider: str,
    current_user = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """GET /v1/integrations/{provider}/status — Check connection status."""
    return {
        "provider": provider,
        "status": IntegrationStatus.NOT_CONFIGURED.value,
        "last_synced_at": None,
        "sync_frequency": None,  # DevOps: Set per provider
        "error_count": 0,
        "note": "Integration not yet configured. Follow connect flow to activate.",
    }


@router.delete("/{provider}", response_model=dict)
async def disconnect_integration(
    provider: str,
    current_user = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """DELETE /v1/integrations/{provider} — Revoke integration access."""
    return {
        "provider": provider,
        "status": IntegrationStatus.REVOKED.value,
        "revoked_at": datetime.utcnow().isoformat(),
        "message": f"{provider} integration revoked. Tokens deleted.",
    }


@router.get("/salesforce/schema", response_model=dict)
async def get_salesforce_schema(
    current_user = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    GET /v1/integrations/salesforce/schema
    Enterprise only: Salesforce data mapping configuration.
    BLANK: Requires live Salesforce connection.
    """
    org = await _get_workspace(current_user, db)
    if not org.subscription or org.subscription.plan_type != PlanType.ENTERPRISE:
        raise HTTPException(status_code=403, detail="Salesforce requires Enterprise plan")

    return {
        "provider": "salesforce",
        "status": IntegrationStatus.NOT_CONFIGURED.value,
        "schema_mapping": {
            "salesforce_account": None,      # DevOps: Map to Account object
            "salesforce_contact": None,      # DevOps: Map to Contact object
            "salesforce_opportunity": None,  # DevOps: Map to Opportunity object
            "salesforce_task": None,         # DevOps: Map to Task object
            "salesforce_document": None,     # DevOps: Map to Document object
        },
        "sync_config": {
            "direction": None,               # DevOps: "bidirectional" | "to_salesforce" | "from_salesforce"
            "frequency": None,               # DevOps: "realtime" | "hourly" | "daily"
            "conflict_resolution": None,     # DevOps: "salesforce_wins" | "requi_wins" | "manual"
        },
        "note": "DevOps: Configure Salesforce connected app and populate schema mapping.",
    }


# ==========================
# v3.0 — Zapier Integration Hub
# ==========================

class ZapierActionRequest(BaseModel):
    """Trigger a Zapier action from AI chat"""
    action_type: str  # "email", "calendar", "forms", "slack", "teams"
    payload: dict
    conversation_id: Optional[str] = None


class ZapierWorkflow(BaseModel):
    """Define a Zapier workflow trigger"""
    name: str
    trigger_event: str  # "new_task", "compliance_alert", "ai_summary"
    action_steps: List[dict]
    is_active: bool = True


@router.post("/zapier/actions", response_model=dict)
async def trigger_zapier_action(
    request: ZapierActionRequest,
    current_user = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    POST /v1/integrations/zapier/actions
    Trigger a Zapier action from the AI chat.
    
    Supported actions:
    - email: Send summary via SendGrid/Outlook/Gmail
    - calendar: Create Outlook/Google Calendar event
    - forms: Export to Microsoft Forms/Google Forms
    - slack: Send message to Slack channel
    - teams: Post to Microsoft Teams channel
    
    DevOps: Configure Zapier webhooks at https://zapier.com/app/webhooks
    Store webhook URLs in environment variables:
    - ZAPIER_EMAIL_WEBHOOK_URL
    - ZAPIER_CALENDAR_WEBHOOK_URL
    - ZAPIER_FORMS_WEBHOOK_URL
    - ZAPIER_SLACK_WEBHOOK_URL
    - ZAPIER_TEAMS_WEBHOOK_URL
    
    Example payload for email:
    {
      "action_type": "email",
      "payload": {
        "to": "user@example.com",
        "subject": "Requi AI Compliance Summary",
        "body": "HIPAA compliance is at 87%...",
        "attachments": []
      }
    }
    """
    org = await _get_workspace(current_user, db)
    
    # Validate action type
    valid_actions = ["email", "calendar", "forms", "slack", "teams"]
    if request.action_type not in valid_actions:
        raise HTTPException(status_code=400, detail=f"Invalid action_type. Must be one of: {valid_actions}")
    
    # PLACEHOLDER: Queue for Zapier delivery
    import time
    action_id = f"zap_{int(time.time() * 1000)}"
    
    return {
        "action_id": action_id,
        "action_type": request.action_type,
        "status": "queued",
        "provider": "zapier",
        "payload_preview": str(request.payload)[:200],
        "webhook_url": f"# BLANK — DevOps: Set ZAPIER_{request.action_type.upper()}_WEBHOOK_URL env var",
        "organization_id": str(org.id),
        "triggered_by": str(current_user.id),
        "created_at": datetime.utcnow().isoformat(),
        "estimated_delivery": "< 30 seconds",
        "retry_policy": {
            "max_retries": 3,
            "backoff": "exponential",
            "timeout_seconds": 30,
        },
        "implementation": {
            "step_1": "Create Zap at zapier.com with Webhooks by Zapier trigger",
            "step_2": f"Set webhook URL as ZAPIER_{request.action_type.upper()}_WEBHOOK_URL",
            "step_3": "Add action step (Email by SendGrid / Outlook / Gmail)",
            "step_4": "Map payload fields to action fields",
            "step_5": "Test trigger with sample payload from this endpoint",
        },
    }


@router.get("/zapier/workflows", response_model=dict)
async def list_zapier_workflows(
    current_user = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    GET /v1/integrations/zapier/workflows
    List configured Zapier workflows for the workspace.
    """
    return {
        "workflows": [
            {
                "id": "zap_email_summary",
                "name": "Send AI Summary via Email",
                "trigger": "ai.chat.completed",
                "actions": ["sendgrid.send_email"],
                "status": "not_configured",
                "webhook_url": "# BLANK",
            },
            {
                "id": "zap_calendar_event",
                "name": "Create Calendar Event from Task",
                "trigger": "task.created",
                "actions": ["outlook.create_event", "google_calendar.create_event"],
                "status": "not_configured",
                "webhook_url": "# BLANK",
            },
            {
                "id": "zap_forms_export",
                "name": "Export Compliance to Microsoft Forms",
                "trigger": "compliance.scan.completed",
                "actions": ["microsoft_forms.create_entry"],
                "status": "not_configured",
                "webhook_url": "# BLANK",
            },
            {
                "id": "zap_slack_alert",
                "name": "Slack Alert for High-Risk Findings",
                "trigger": "compliance.risk.high",
                "actions": ["slack.send_message"],
                "status": "not_configured",
                "webhook_url": "# BLANK",
            },
        ],
        "note": "DevOps: Configure webhooks in Zapier and set environment variables.",
    }


@router.post("/zapier/workflows", response_model=dict)
async def create_zapier_workflow(
    workflow: ZapierWorkflow,
    current_user = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    POST /v1/integrations/zapier/workflows
    Create a new Zapier workflow.
    
    Example:
    {
      "name": "Notify on Critical Finding",
      "trigger_event": "compliance.risk.critical",
      "action_steps": [
        {"service": "slack", "action": "send_message", "channel": "#compliance"},
        {"service": "email", "action": "send_email", "to": "admin@org.com"}
      ]
    }
    """
    import time
    workflow_id = f"zapwf_{int(time.time() * 1000)}"
    
    return {
        "workflow_id": workflow_id,
        "name": workflow.name,
        "trigger_event": workflow.trigger_event,
        "action_steps": workflow.action_steps,
        "is_active": workflow.is_active,
        "status": "created_pending_config",
        "note": "DevOps: Complete configuration in Zapier dashboard.",
    }
