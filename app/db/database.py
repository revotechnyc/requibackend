"""
Database connection and session management
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

def _database_url_lower() -> str:
    return (settings.database_url or "").lower()


def _is_external_database() -> bool:
    """Managed/cloud Postgres (Supabase, RDS, Neon, etc.)."""
    url = _database_url_lower()
    return any(
        host in url
        for host in (
            "supabase.co",
            "supabase.com",
            "pooler.supabase",
            "amazonaws.com",
            "neon.tech",
            "render.com",
            "railway.app",
        )
    )


def _is_supabase_session_pooler() -> bool:
    """Supabase Session mode (port 5432) — strict client limit (~15 on free/pro)."""
    url = _database_url_lower()
    if "pooler.supabase" not in url:
        return False
    # Transaction pooler (port 6543) allows more concurrent clients.
    if ":6543" in url:
        return False
    return True


def get_async_database_url() -> str:
    """Convert sync PostgreSQL URL to async."""
    url = settings.database_url
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _engine_connect_args() -> dict:
    """SSL for managed Postgres when not already set in the URL."""
    url = (settings.database_url or "").lower()
    if "sslmode=" in url or "ssl=" in url:
        return {}
    if _is_external_database():
        return {"ssl": "require"}
    return {}


# Session-pooler caps per process so api + worker + beat stay under ~15 total.
_SESSION_POOLER_CAPS: dict[str, tuple[int, int]] = {
    "api": (4, 2),
    "worker": (2, 1),
    "beat": (1, 0),
}


def _effective_pool_size() -> int:
    if _is_supabase_session_pooler():
        role = (settings.database_pool_role or "api").strip().lower()
        cap, _ = _SESSION_POOLER_CAPS.get(role, (3, 1))
        return min(settings.database_pool_size, cap)
    if _is_external_database():
        return min(settings.database_pool_size, 8)
    return settings.database_pool_size


def _effective_max_overflow() -> int:
    if _is_supabase_session_pooler():
        role = (settings.database_pool_role or "api").strip().lower()
        _, cap = _SESSION_POOLER_CAPS.get(role, (3, 1))
        return min(settings.database_max_overflow, cap)
    if _is_external_database():
        return min(settings.database_max_overflow, 4)
    return settings.database_max_overflow


_pool_size = _effective_pool_size()
_max_overflow = _effective_max_overflow()

# Create async engine
async_engine = create_async_engine(
    get_async_database_url(),
    pool_size=_pool_size,
    max_overflow=_max_overflow,
    pool_pre_ping=True,
    pool_recycle=300,
    pool_timeout=30,
    echo=settings.debug,
    connect_args=_engine_connect_args(),
)

if _is_supabase_session_pooler():
    print(
        f"[DB  ] Supabase session pooler: role={settings.database_pool_role!r} "
        f"pool_size={_pool_size} max_overflow={_max_overflow}"
    )

# Create async session factory
AsyncSessionLocal = async_sessionmaker(
    async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for getting database session"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    """Context manager for database sessions"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def _ensure_platform_admin_invited_by_column(conn) -> None:
    await conn.execute(
        text(
            """
            ALTER TABLE platform_admins
            ADD COLUMN IF NOT EXISTS invited_by_id UUID REFERENCES platform_admins(id)
            """
        )
    )


async def _ensure_platform_admins_role_column(conn) -> None:
    """Migrate platform_admins.role from PG enum to varchar (safe for re-deploys)."""
    await conn.execute(
        text(
            """
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'platform_admins'
                  AND column_name = 'role'
                  AND udt_name = 'platformadminrole'
              ) THEN
                ALTER TABLE platform_admins
                  ALTER COLUMN role TYPE VARCHAR(50) USING role::text;
              END IF;
            END $$;
            """
        )
    )
    await conn.execute(text("DROP TYPE IF EXISTS platformadminrole"))


async def _ensure_conversation_share_columns(conn) -> None:
    """Add share-import columns to existing deployments (create_all does not alter tables)."""
    await conn.execute(
        text(
            """
            ALTER TABLE conversations
            ADD COLUMN IF NOT EXISTS is_shared_import BOOLEAN NOT NULL DEFAULT FALSE
            """
        )
    )
    await conn.execute(
        text(
            """
            ALTER TABLE conversations
            ADD COLUMN IF NOT EXISTS shared_from_token VARCHAR(64)
            """
        )
    )


async def _ensure_platform_blog_post_columns(conn) -> None:
    """Add columns for iterative development (create_all does not alter tables)."""
    # Scheduled publishing (added after initial rollout)
    await conn.execute(
        text(
            """
            ALTER TABLE platform_blog_posts
            ADD COLUMN IF NOT EXISTS scheduled_for TIMESTAMP
            """
        )
    )


async def init_db() -> None:
    """Initialize database (create tables)"""
    from app.db.models import Base
    
    async with async_engine.begin() as conn:
        # Create pgvector extension if not exists
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        # Create all tables
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_platform_admins_role_column(conn)
        await _ensure_platform_admin_invited_by_column(conn)
        await _ensure_platform_blog_post_columns(conn)
        await _ensure_conversation_share_columns(conn)
        await _ensure_workspace_invitations_table(conn)
        await _ensure_workspace_invitation_varchar_columns(conn)
        await _ensure_seats_role_varchar_columns(conn)
        await _ensure_userrole_enum_values(conn)
        await _ensure_workspace_tasks_table(conn)
        await _ensure_workspace_task_document_column(conn)
        await _ensure_workspace_task_document_ids_column(conn)
        await _ensure_workspace_workflows_table(conn)
        await _ensure_workflow_activities_table(conn)
        await _ensure_workspace_task_workflow_column(conn)
        await _ensure_document_workflow_column(conn)
        await _ensure_task_resolution_columns(conn)
        await _ensure_task_approval_ai_reviews_column(conn)
        await _ensure_workflow_findings_table(conn)
        await _ensure_conversation_workflow_columns(conn)
        await _ensure_compliance_tables(conn)
        await _ensure_member_feature_permissions_columns(conn)
        await _ensure_notification_type_enum_values(conn)


async def _ensure_member_feature_permissions_columns(conn) -> None:
    await conn.execute(
        text(
            """
            ALTER TABLE seats
            ADD COLUMN IF NOT EXISTS feature_permissions JSONB
            """
        )
    )
    await conn.execute(
        text(
            """
            ALTER TABLE workspace_invitations
            ADD COLUMN IF NOT EXISTS feature_permissions JSONB
            """
        )
    )


async def _ensure_compliance_tables(conn) -> None:
    """Compliance frameworks, gaps, and score snapshots."""
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS compliance_frameworks (
                id UUID PRIMARY KEY,
                organization_id UUID NOT NULL REFERENCES organizations(id),
                slug VARCHAR(64) NOT NULL,
                name VARCHAR(120) NOT NULL,
                score NUMERIC(5, 2),
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc'),
                updated_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc'),
                UNIQUE (organization_id, slug)
            )
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS compliance_gaps (
                id UUID PRIMARY KEY,
                organization_id UUID NOT NULL REFERENCES organizations(id),
                framework_slug VARCHAR(64) NOT NULL,
                title VARCHAR(500) NOT NULL,
                description TEXT,
                severity VARCHAR(20) NOT NULL DEFAULT 'medium',
                status VARCHAR(20) NOT NULL DEFAULT 'open',
                category VARCHAR(100) NOT NULL DEFAULT 'General',
                created_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc'),
                updated_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc'),
                resolved_at TIMESTAMP
            )
            """
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_compliance_gaps_org_status "
            "ON compliance_gaps (organization_id, status)"
        )
    )
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS compliance_score_snapshots (
                id UUID PRIMARY KEY,
                organization_id UUID NOT NULL REFERENCES organizations(id),
                framework_scores JSONB NOT NULL DEFAULT '{}'::jsonb,
                overall_score NUMERIC(5, 2) NOT NULL DEFAULT 0,
                risk_level VARCHAR(20) NOT NULL DEFAULT 'medium',
                gaps_found JSONB NOT NULL DEFAULT '[]'::jsonb,
                recommendations JSONB NOT NULL DEFAULT '[]'::jsonb,
                source_type VARCHAR(40) NOT NULL DEFAULT 'aggregation',
                calculated_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc')
            )
            """
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_compliance_scores_org_calc "
            "ON compliance_score_snapshots (organization_id, calculated_at DESC)"
        )
    )


async def _pg_type_exists(conn, type_name: str) -> bool:
    result = await conn.execute(
        text("SELECT 1 FROM pg_type WHERE typname = :name"),
        {"name": type_name},
    )
    return result.scalar() is not None


async def _ensure_notification_type_enum_values(conn) -> None:
    """Add task reminder values to PG notificationtype enum when missing.

    Existing notification types use UPPERCASE labels (WELCOME, TRIAL_STARTED, …).
    SQLAlchemy sends enum member names to PostgreSQL — task types must match.
    """
    if not await _pg_type_exists(conn, "notificationtype"):
        return

    for value in ("TASK_DUE_SOON", "TASK_DUE_TODAY", "TASK_OVERDUE"):
        await conn.execute(
            text(
                f"""
                DO $$
                BEGIN
                  IF NOT EXISTS (
                    SELECT 1 FROM pg_enum e
                    JOIN pg_type t ON e.enumtypid = t.oid
                    WHERE t.typname = 'notificationtype' AND e.enumlabel = '{value}'
                  ) THEN
                    ALTER TYPE notificationtype ADD VALUE '{value}';
                  END IF;
                END $$;
                """
            )
        )


async def _ensure_workspace_tasks_table(conn) -> None:
    """Create workspace_tasks for compliance task management."""
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS workspace_tasks (
                id UUID PRIMARY KEY,
                organization_id UUID NOT NULL REFERENCES organizations(id),
                creator_id UUID NOT NULL REFERENCES users(id),
                assignee_id UUID REFERENCES users(id),
                reviewer_id UUID REFERENCES users(id),
                approver_id UUID REFERENCES users(id),
                title VARCHAR(500) NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status VARCHAR(32) NOT NULL DEFAULT 'pending',
                priority VARCHAR(20) NOT NULL DEFAULT 'medium',
                category VARCHAR(100) NOT NULL DEFAULT 'General',
                due_date VARCHAR(32),
                tags JSONB DEFAULT '[]'::jsonb,
                comments JSONB DEFAULT '[]'::jsonb,
                history JSONB DEFAULT '[]'::jsonb,
                created_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc'),
                updated_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc')
            )
            """
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_workspace_tasks_org_status "
            "ON workspace_tasks (organization_id, status)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_workspace_tasks_org_created "
            "ON workspace_tasks (organization_id, created_at DESC)"
        )
    )


async def _ensure_workspace_task_document_column(conn) -> None:
    """Optional document attachment on compliance tasks."""
    await conn.execute(
        text(
            """
            ALTER TABLE workspace_tasks
            ADD COLUMN IF NOT EXISTS document_id UUID REFERENCES documents(id)
            """
        )
    )


async def _ensure_workspace_task_document_ids_column(conn) -> None:
    """Multiple document attachments on compliance tasks."""
    await conn.execute(
        text(
            """
            ALTER TABLE workspace_tasks
            ADD COLUMN IF NOT EXISTS document_ids JSONB DEFAULT '[]'::jsonb
            """
        )
    )
    await conn.execute(
        text(
            """
            UPDATE workspace_tasks
            SET document_ids = jsonb_build_array(document_id::text)
            WHERE document_id IS NOT NULL
              AND (
                document_ids IS NULL
                OR document_ids = '[]'::jsonb
                OR document_ids = 'null'::jsonb
              )
            """
        )
    )


async def _ensure_workspace_workflows_table(conn) -> None:
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS workspace_workflows (
                id UUID PRIMARY KEY,
                organization_id UUID NOT NULL REFERENCES organizations(id),
                creator_id UUID NOT NULL REFERENCES users(id),
                owner_id UUID NOT NULL REFERENCES users(id),
                reference_code VARCHAR(32) NOT NULL,
                title VARCHAR(500) NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status VARCHAR(32) NOT NULL DEFAULT 'open',
                source VARCHAR(32) NOT NULL DEFAULT 'manual',
                external_ref VARCHAR(255),
                priority VARCHAR(20) NOT NULL DEFAULT 'medium',
                due_date VARCHAR(32),
                category VARCHAR(100) NOT NULL DEFAULT 'General',
                created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (NOW() AT TIME ZONE 'utc'),
                updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (NOW() AT TIME ZONE 'utc'),
                completed_at TIMESTAMP WITHOUT TIME ZONE
            )
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_workspace_workflows_org_reference
            ON workspace_workflows (organization_id, reference_code)
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS idx_workspace_workflows_org_status
            ON workspace_workflows (organization_id, status)
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS idx_workspace_workflows_org_created
            ON workspace_workflows (organization_id, created_at DESC)
            """
        )
    )


async def _ensure_workflow_activities_table(conn) -> None:
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS workflow_activities (
                id UUID PRIMARY KEY,
                workflow_id UUID NOT NULL REFERENCES workspace_workflows(id) ON DELETE CASCADE,
                organization_id UUID NOT NULL REFERENCES organizations(id),
                actor_id UUID NOT NULL REFERENCES users(id),
                event_type VARCHAR(64) NOT NULL,
                payload JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (NOW() AT TIME ZONE 'utc')
            )
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_activities_workflow
            ON workflow_activities (workflow_id, created_at DESC)
            """
        )
    )


async def _ensure_workspace_task_workflow_column(conn) -> None:
    await conn.execute(
        text(
            """
            ALTER TABLE workspace_tasks
            ADD COLUMN IF NOT EXISTS workflow_id UUID REFERENCES workspace_workflows(id)
            """
        )
    )


async def _ensure_document_workflow_column(conn) -> None:
    await conn.execute(
        text(
            """
            ALTER TABLE documents
            ADD COLUMN IF NOT EXISTS workflow_id UUID REFERENCES workspace_workflows(id)
            """
        )
    )


async def _ensure_task_approval_ai_reviews_column(conn) -> None:
    await conn.execute(
        text(
            """
            ALTER TABLE workspace_tasks
            ADD COLUMN IF NOT EXISTS approval_ai_reviews JSONB DEFAULT '[]'
            """
        )
    )


async def _ensure_task_resolution_columns(conn) -> None:
    await conn.execute(
        text(
            """
            ALTER TABLE workspace_tasks
            ADD COLUMN IF NOT EXISTS resolution_result JSONB
            """
        )
    )
    await conn.execute(
        text(
            """
            ALTER TABLE workspace_tasks
            ADD COLUMN IF NOT EXISTS resolution_document_id UUID REFERENCES documents(id)
            """
        )
    )
    await conn.execute(
        text(
            """
            ALTER TABLE workspace_tasks
            ADD COLUMN IF NOT EXISTS execution_conversation_id UUID REFERENCES conversations(id)
            """
        )
    )
    await conn.execute(
        text(
            """
            ALTER TABLE workspace_tasks
            ADD COLUMN IF NOT EXISTS resolution_history JSONB DEFAULT '[]'
            """
        )
    )


async def _ensure_workflow_findings_table(conn) -> None:
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS workflow_findings (
                id UUID PRIMARY KEY,
                workflow_id UUID NOT NULL REFERENCES workspace_workflows(id) ON DELETE CASCADE,
                organization_id UUID NOT NULL REFERENCES organizations(id),
                task_id UUID REFERENCES workspace_tasks(id),
                created_by_id UUID NOT NULL REFERENCES users(id),
                summary TEXT NOT NULL DEFAULT '',
                findings JSONB DEFAULT '[]',
                risk_level VARCHAR(32),
                recommendations JSONB DEFAULT '[]',
                evidence_refs JSONB DEFAULT '[]',
                raw_payload JSONB DEFAULT '{}',
                created_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc')
            )
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_findings_workflow
            ON workflow_findings (workflow_id, created_at DESC)
            """
        )
    )


async def _ensure_conversation_workflow_columns(conn) -> None:
    await conn.execute(
        text(
            """
            ALTER TABLE conversations
            ADD COLUMN IF NOT EXISTS workflow_id UUID REFERENCES workspace_workflows(id)
            """
        )
    )
    await conn.execute(
        text(
            """
            ALTER TABLE conversations
            ADD COLUMN IF NOT EXISTS task_id UUID REFERENCES workspace_tasks(id)
            """
        )
    )


async def _ensure_workspace_invitation_varchar_columns(conn) -> None:
    """Migrate workspace_invitations.role/status from PG enum to VARCHAR (lowercase values)."""
    await conn.execute(
        text(
            """
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'workspace_invitations'
                  AND column_name = 'role'
                  AND udt_name = 'userrole'
              ) THEN
                ALTER TABLE workspace_invitations
                  ALTER COLUMN role TYPE VARCHAR(50) USING (
                    CASE role::text
                      WHEN 'VIEWER' THEN 'viewer'
                      WHEN 'ADMIN' THEN 'admin'
                      WHEN 'SEO' THEN 'seo'
                      WHEN 'REVIEWER' THEN 'reviewer'
                      WHEN 'CONTRIBUTOR' THEN 'contributor'
                      ELSE lower(role::text)
                    END
                  );
              END IF;

              IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'workspace_invitations'
                  AND column_name = 'status'
                  AND udt_name = 'workspaceinvitationstatus'
              ) THEN
                ALTER TABLE workspace_invitations
                  ALTER COLUMN status TYPE VARCHAR(32) USING (
                    CASE status::text
                      WHEN 'PENDING' THEN 'pending'
                      WHEN 'ACCEPTED' THEN 'accepted'
                      WHEN 'REVOKED' THEN 'revoked'
                      WHEN 'EXPIRED' THEN 'expired'
                      ELSE lower(status::text)
                    END
                  );
              END IF;
            END $$;
            """
        )
    )


async def _ensure_seats_role_varchar_columns(conn) -> None:
    """Migrate seats.role from PG userrole enum to VARCHAR (lowercase values)."""
    await conn.execute(
        text(
            """
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'seats'
                  AND column_name = 'role'
                  AND udt_name = 'userrole'
              ) THEN
                ALTER TABLE seats
                  ALTER COLUMN role TYPE VARCHAR(50) USING (
                    CASE role::text
                      WHEN 'VIEWER' THEN 'viewer'
                      WHEN 'ADMIN' THEN 'admin'
                      WHEN 'SEO' THEN 'seo'
                      WHEN 'REVIEWER' THEN 'reviewer'
                      WHEN 'CONTRIBUTOR' THEN 'contributor'
                      ELSE lower(role::text)
                    END
                  );
              END IF;
            END $$;
            """
        )
    )


async def _ensure_userrole_enum_values(conn) -> None:
    """Add reviewer/contributor to PG userrole enum when missing (safe re-deploy)."""
    if not await _pg_type_exists(conn, "userrole"):
        return

    for value in ("reviewer", "contributor", "approver"):
        await conn.execute(
            text(
                f"""
                DO $$
                BEGIN
                  IF NOT EXISTS (
                    SELECT 1 FROM pg_enum e
                    JOIN pg_type t ON e.enumtypid = t.oid
                    WHERE t.typname = 'userrole' AND e.enumlabel = '{value}'
                  ) THEN
                    ALTER TYPE userrole ADD VALUE '{value}';
                  END IF;
                END $$;
                """
            )
        )


async def _ensure_workspace_invitations_table(conn) -> None:
    """Create workspace_invitations if missing (create_all on fresh DB; ALTER for existing)."""
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS workspace_invitations (
                id UUID PRIMARY KEY,
                organization_id UUID NOT NULL REFERENCES organizations(id),
                invited_by_id UUID NOT NULL REFERENCES users(id),
                email VARCHAR(255) NOT NULL,
                role VARCHAR(50) NOT NULL DEFAULT 'viewer',
                token VARCHAR(255) NOT NULL UNIQUE,
                status VARCHAR(32) NOT NULL DEFAULT 'pending',
                first_name VARCHAR(100),
                last_name VARCHAR(100),
                message TEXT,
                expires_at TIMESTAMP NOT NULL,
                accepted_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc'),
                updated_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc')
            )
            """
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_workspace_invite_org_email "
            "ON workspace_invitations (organization_id, email)"
        )
    )


async def close_db() -> None:
    """Close database connections"""
    await async_engine.dispose()
