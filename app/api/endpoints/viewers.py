"""
View-Only User Management API — v2.1
Manages read-only observers who can view dashboards, tasks, documents, frameworks,
evidence, reports, and team directory but CANNOT create, edit, assign, approve, or delete.

Endpoints:
  POST /v1/viewers/invite     — Invite a viewer (Admin only)
  GET  /v1/viewers            — List all viewers in workspace
  POST /v1/viewers/{id}/revoke — Revoke viewer access (Admin only)
  GET  /v1/viewers/me         — Get current viewer's accessible resources
"""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.endpoints.auth import get_current_active_user
from app.core.permissions import PermissionChecker
from app.db.database import get_db
from app.db.models import Organization, Seat, User, UserRole

router = APIRouter()


# ============== Pydantic Models ==============

class ViewerInvite(BaseModel):
    email: EmailStr
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    message: Optional[str] = None  # Custom invite message


class ViewerResponse(BaseModel):
    id: str
    email: str
    first_name: Optional[str]
    last_name: Optional[str]
    status: str  # "active", "pending", "revoked"
    invited_by: str
    invited_at: str
    revoked_at: Optional[str] = None
    last_accessed_at: Optional[str] = None


# ============== In-memory store ==============
VIEWERS_STORE: List[dict] = []
VIEWER_COUNTER = 0


def _next_viewer_id() -> int:
    global VIEWER_COUNTER
    VIEWER_COUNTER += 1
    return VIEWER_COUNTER


# ============== Helpers ==============

async def _get_workspace(user: User, db: AsyncSession) -> Organization:
    result = await db.execute(
        select(Seat).where(Seat.user_id == user.id, Seat.is_active == True)
        .options(selectinload(Seat.organization))
    )
    seat = result.scalar_one_or_none()
    if not seat:
        raise HTTPException(status_code=403, detail="No active workspace")
    return seat.organization


def _check_admin(role: UserRole):
    if role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Only Admin can manage viewers")


# ============== ENDPOINTS ==============

@router.post("/invite", response_model=dict, status_code=status.HTTP_201_CREATED)
async def invite_viewer(
    data: ViewerInvite,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    POST /v1/viewers/invite
    Invite a view-only user. Admin only.
    Sends email invitation. Viewer accepts via magic link to activate.
    """
    org = await _get_workspace(current_user, db)
    result = await db.execute(
        select(Seat).where(Seat.user_id == current_user.id, Seat.is_active == True)
    )
    seat = result.scalar_one()
    _check_admin(seat.role)

    # Check for duplicate
    existing = [v for v in VIEWERS_STORE
                if v["email"] == data.email and v["workspace_id"] == str(org.id)
                and v["status"] in ("active", "pending")]
    if existing:
        raise HTTPException(status_code=409, detail="Viewer already invited or active")

    viewer_id = _next_viewer_id()
    now = datetime.utcnow().isoformat()

    # PLACEHOLDER: Send email invitation
    # DevOps: Connect SendGrid/AWS SES here
    magic_link = f"https://requi.health/accept-invite?token=VIEWER_TOKEN_{viewer_id}"
    email_sent = False  # Set True when email service is live

    viewer_record = {
        "id": str(viewer_id),
        "workspace_id": str(org.id),
        "email": data.email,
        "first_name": data.first_name,
        "last_name": data.last_name,
        "status": "pending",
        "invited_by": str(current_user.id),
        "invited_by_name": f"{current_user.first_name} {current_user.last_name}".strip() or current_user.email,
        "invited_at": now,
        "message": data.message,
        "revoked_at": None,
        "revoked_by": None,
        "last_accessed_at": None,
        "magic_link": magic_link,
    }
    VIEWERS_STORE.append(viewer_record)

    return {
        "viewer": viewer_record,
        "delivery": {
            "email_sent": email_sent,
            "magic_link": magic_link,  # For testing; remove in production
            "note": "DevOps: Configure email service (SendGrid/AWS SES) to send actual invitations.",
        },
    }


@router.get("/", response_model=dict)
async def list_viewers(
    status: Optional[str] = None,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    GET /v1/viewers
    List all view-only users in the workspace. Admin only.
    """
    org = await _get_workspace(current_user, db)
    result = await db.execute(select(Seat).where(Seat.user_id == current_user.id, Seat.is_active == True))
    seat = result.scalar_one()
    _check_admin(seat.role)

    viewers = [v for v in VIEWERS_STORE if v.get("workspace_id") == str(org.id)]
    if status:
        viewers = [v for v in viewers if v["status"] == status]

    counts = {
        "total": len(viewers),
        "active": len([v for v in viewers if v["status"] == "active"]),
        "pending": len([v for v in viewers if v["status"] == "pending"]),
        "revoked": len([v for v in viewers if v["status"] == "revoked"]),
    }

    return {"viewers": viewers, "counts": counts}


@router.post("/{viewer_id}/revoke", response_model=dict)
async def revoke_viewer(
    viewer_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    POST /v1/viewers/{id}/revoke
    Revoke a viewer's access. Admin only.
    Immediate termination. Viewer can be re-invited.
    """
    org = await _get_workspace(current_user, db)
    result = await db.execute(select(Seat).where(Seat.user_id == current_user.id, Seat.is_active == True))
    seat = result.scalar_one()
    _check_admin(seat.role)

    for viewer in VIEWERS_STORE:
        if viewer["id"] == viewer_id and viewer.get("workspace_id") == str(org.id):
            if viewer["status"] == "revoked":
                raise HTTPException(status_code=400, detail="Viewer already revoked")

            now = datetime.utcnow().isoformat()
            viewer["status"] = "revoked"
            viewer["revoked_at"] = now
            viewer["revoked_by"] = str(current_user.id)

            return {
                "viewer": viewer,
                "message": "Viewer access revoked. Immediate termination applied.",
            }

    raise HTTPException(status_code=404, detail="Viewer not found")


@router.get("/me", response_model=dict)
async def viewer_me(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    GET /v1/viewers/me
    Returns the view-only user's accessible resources.
    View-Only users can access: dashboards, tasks, reminders, documents,
    frameworks, evidence, reports, team directory.
    """
    org = await _get_workspace(current_user, db)
    result = await db.execute(select(Seat).where(Seat.user_id == current_user.id, Seat.is_active == True))
    seat = result.scalar_one()

    if seat.role != UserRole.VIEWER:
        raise HTTPException(status_code=403, detail="Endpoint reserved for View-Only users")

    return {
        "role": "viewer",
        "workspace_id": str(org.id),
        "accessible_resources": {
            "dashboards": {"read": True, "write": False, "interact": False},
            "tasks": {"read": True, "write": False, "assign": False, "approve": False, "create": False, "comment": False},
            "compliance": {"read": True, "write": False, "interact": False},
            "reminders": {"read": True, "write": False, "interact": False},
        },
        "restricted_modules": [
            "intelligence",
            "documents",
            "news",
            "blog",
            "teams",
            "settings",
            "integrations",
            "admin",
        ],
        "restricted_actions": [
            "use_intelligence",
            "create_tasks",
            "edit_tasks",
            "delete_tasks",
            "assign_tasks",
            "approve_tasks",
            "comment_on_tasks",
            "upload_documents",
            "edit_anything",
            "interact",
            "invite_users",
            "revoke_users",
            "manage_integrations",
            "manage_billing",
            "configure_alerts",
            "export_audit_trail",
        ],
        "note": "View-Only access: read Dashboard, Tasks, Compliance, and Reminders only. Cannot use Intelligence, cannot make edits, cannot interact. Contact workspace admin for elevated permissions.",
    }
