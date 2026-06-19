"""
API Routes Registration — v2.1
Requi Health platform endpoints
"""

from fastapi import APIRouter, Depends

from app.api.endpoints import (
    admin,
    ai,
    alerts,
    auth,
    platform_admin_auth,
    platform_admin_team,
    platform_admin_blog,
    platform_admin_customers,
    platform_admin_overview,
    platform_admin_billing,
    platform_admin_analytics,
    platform_blog_public,
    chat_share,
    billing,
    blog,
    integrations,
    knowledge,
    organizations,
    permissions,
    scoring,
    sources,
    tasks,
    workflows,
    calendar,
    compliance,
    teams,
    users,
    viewers,
    notifications,
)
from app.api.endpoints.auth import get_current_active_user
from app.core.platform_admin_security import get_current_platform_admin

api_router = APIRouter()

# Auth routes (no auth required)
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])

# SaaS admin portal auth (separate JWT; no customer session)
api_router.include_router(
    platform_admin_auth.router,
    prefix="/platform-admin/auth",
    tags=["platform-admin-auth"],
)

platform_admin_dep = [Depends(get_current_platform_admin)]

api_router.include_router(
    platform_admin_team.router,
    prefix="/platform-admin/team",
    tags=["platform-admin-team"],
    dependencies=platform_admin_dep,
)

api_router.include_router(
    platform_admin_blog.router,
    prefix="/platform-admin/blog",
    tags=["platform-admin-blog"],
    dependencies=platform_admin_dep,
)

api_router.include_router(
    platform_admin_customers.router,
    prefix="/platform-admin/customers",
    tags=["platform-admin-customers"],
    dependencies=platform_admin_dep,
)

api_router.include_router(
    platform_admin_overview.router,
    prefix="/platform-admin/overview",
    tags=["platform-admin-overview"],
    dependencies=platform_admin_dep,
)

api_router.include_router(
    platform_admin_billing.router,
    prefix="/platform-admin/billing",
    tags=["platform-admin-billing"],
    dependencies=platform_admin_dep,
)

api_router.include_router(
    platform_admin_analytics.router,
    prefix="/platform-admin/analytics",
    tags=["platform-admin-analytics"],
    dependencies=platform_admin_dep,
)

# Public platform blog (read-only)
api_router.include_router(
    platform_blog_public.public_router,
    prefix="/platform-blog",
    tags=["platform-blog"],
)

# Protected routes — all require authentication
auth_dep = [Depends(get_current_active_user)]

api_router.include_router(
    users.router, prefix="/users", tags=["users"], dependencies=auth_dep,
)
api_router.include_router(
    organizations.router, prefix="/organizations", tags=["organizations"], dependencies=auth_dep,
)
api_router.include_router(
    billing.public_router, prefix="/billing", tags=["billing"],
)
api_router.include_router(
    billing.router, prefix="/billing", tags=["billing"], dependencies=auth_dep,
)
api_router.include_router(
    sources.router, prefix="/sources", tags=["sources"], dependencies=auth_dep,
)
api_router.include_router(
    knowledge.router, prefix="/knowledge", tags=["knowledge"], dependencies=auth_dep,
)
api_router.include_router(
    ai.router, prefix="/ai", tags=["ai"], dependencies=auth_dep,
)
# Public shared chat view (no auth)
api_router.include_router(
    chat_share.public_router, prefix="/ai", tags=["Chat Share"],
)
api_router.include_router(
    chat_share.router, prefix="/ai", tags=["Chat Share"], dependencies=auth_dep,
)
api_router.include_router(
    admin.router, prefix="/admin", tags=["admin"], dependencies=auth_dep,
)

# ==========================
# v2.1 ENDPOINTS
# ==========================

# Alerts API — Compliance alerts (Zapier, email, in-app)
api_router.include_router(
    alerts.router, prefix="/alerts", tags=["v2.1 — Alerts"], dependencies=auth_dep,
)

# Tasks API — Full lifecycle with approval workflows
api_router.include_router(
    tasks.router, prefix="/tasks", tags=["v2.1 — Tasks"], dependencies=auth_dep,
)

api_router.include_router(
    workflows.router, prefix="/workflows", tags=["v2.1 — Workflows"], dependencies=auth_dep,
)

api_router.include_router(
    calendar.router, prefix="/calendar", tags=["v2.1 — Calendar"], dependencies=auth_dep,
)

api_router.include_router(
    compliance.router, prefix="/compliance", tags=["v2.1 — Compliance"], dependencies=auth_dep,
)

# View-Only User Management API
api_router.include_router(
    viewers.router, prefix="/viewers", tags=["v2.1 — View-Only Users"], dependencies=auth_dep,
)

# Scoring Engine API — Compliance, Risk, Audit Readiness
api_router.include_router(
    scoring.router, prefix="/scoring", tags=["v2.1 — Scoring"], dependencies=auth_dep,
)

# Blog API — Content management for SEO team
api_router.include_router(
    blog.router, prefix="/blog", tags=["v2.1 — Blog"], dependencies=auth_dep,
)

# Integration Hub API — Microsoft, Google, Salesforce (placeholders)
api_router.include_router(
    integrations.router, prefix="/integrations", tags=["v2.1 — Integrations"], dependencies=auth_dep,
)

# Teams API — v2.1 role management
api_router.include_router(
    teams.router, prefix="/teams", tags=["v2.1 — Teams"], dependencies=auth_dep,
)

# Permissions API — Feature gating & role checks
api_router.include_router(
    permissions.router, prefix="/permissions", tags=["v2.1 — Permissions"], dependencies=auth_dep,
)

api_router.include_router(
    notifications.router, tags=["notifications"], dependencies=auth_dep,
)
