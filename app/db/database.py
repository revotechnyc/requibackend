"""
Database connection and session management
"""

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

logger = logging.getLogger(__name__)

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


async def _set_migration_session(conn) -> None:
    """Avoid Supabase statement_timeout during short schema ensures on restart."""
    await conn.execute(text("SET LOCAL statement_timeout = 0"))
    await conn.execute(text("SET LOCAL lock_timeout = '30s'"))


async def _set_migration_session_short_lock(conn) -> None:
    """Hot-table alters: fail fast instead of blocking seats/login for minutes."""
    await conn.execute(text("SET LOCAL statement_timeout = '30s'"))
    await conn.execute(text("SET LOCAL lock_timeout = '3s'"))


async def _column_exists(conn, table: str, column: str) -> bool:
    result = await conn.execute(
        text(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = :table
              AND column_name = :column
            """
        ),
        {"table": table, "column": column},
    )
    return result.scalar() is not None


async def _constraint_exists(conn, table: str, constraint_name: str) -> bool:
    result = await conn.execute(
        text(
            """
            SELECT 1 FROM pg_constraint c
            JOIN pg_class t ON c.conrelid = t.oid
            JOIN pg_namespace n ON t.relnamespace = n.oid
            WHERE n.nspname = 'public'
              AND t.relname = :table
              AND c.conname = :name
            """
        ),
        {"table": table, "name": constraint_name},
    )
    return result.scalar() is not None


async def _ensure_uuid_column(conn, table: str, column: str) -> bool:
    """Add nullable UUID column when missing. Returns True if column was created."""
    if await _column_exists(conn, table, column):
        return False
    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} UUID"))
    return True


async def _fk_on_column_exists(conn, table: str, column: str) -> bool:
    result = await conn.execute(
        text(
            """
            SELECT 1
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_schema = kcu.constraint_schema
             AND tc.constraint_name = kcu.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = 'public'
              AND tc.table_name = :table
              AND kcu.column_name = :column
            """
        ),
        {"table": table, "column": column},
    )
    return result.scalar() is not None


async def _ensure_fk(
    conn,
    table: str,
    column: str,
    ref_table: str,
    constraint_name: str,
    ref_column: str = "id",
) -> None:
    """Add FK without blocking startup: column first, then NOT VALID + validate."""
    await _ensure_uuid_column(conn, table, column)
    if await _fk_on_column_exists(conn, table, column):
        return
    if await _constraint_exists(conn, table, constraint_name):
        return
    await conn.execute(
        text(
            f"""
            ALTER TABLE {table}
            ADD CONSTRAINT {constraint_name}
            FOREIGN KEY ({column}) REFERENCES {ref_table}({ref_column})
            NOT VALID
            """
        )
    )
    await conn.execute(
        text(f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint_name}")
    )


async def _run_migration_step(
    name: str,
    fn: Callable[..., Awaitable[None]],
) -> None:
    """Each step commits independently so one slow ALTER cannot roll back everything."""
    try:
        async with async_engine.begin() as conn:
            await _set_migration_session(conn)
            await fn(conn)
        logger.info("db_migration_step_ok: %s", name)
    except Exception:
        logger.exception("db_migration_step_failed: %s", name)
        raise


async def _run_optional_migration_step(
    name: str,
    fn: Callable[..., Awaitable[None]],
) -> bool:
    """
    Non-blocking migration for busy tables (e.g. seats).
    Skips on lock timeout so API startup and logins are not held hostage.
    """
    try:
        async with async_engine.begin() as conn:
            await _set_migration_session_short_lock(conn)
            await fn(conn)
        logger.info("db_migration_step_ok: %s", name)
        return True
    except Exception as exc:
        logger.warning(
            "db_migration_step_skipped: %s (%s: %s)",
            name,
            type(exc).__name__,
            exc,
        )
        return False


async def _ensure_platform_admin_invited_by_column(conn) -> None:
    await _ensure_fk(
        conn,
        "platform_admins",
        "invited_by_id",
        "platform_admins",
        "fk_platform_admins_invited_by_id",
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
    if not await _column_exists(conn, "conversations", "is_shared_import"):
        await conn.execute(
            text(
                """
                ALTER TABLE conversations
                ADD COLUMN is_shared_import BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
        )
    if not await _column_exists(conn, "conversations", "shared_from_token"):
        await conn.execute(
            text(
                """
                ALTER TABLE conversations
                ADD COLUMN shared_from_token VARCHAR(64)
                """
            )
        )


async def _ensure_platform_blog_post_columns(conn) -> None:
    """Add columns for iterative development (create_all does not alter tables)."""
    if await _column_exists(conn, "platform_blog_posts", "scheduled_for"):
        return
    await conn.execute(
        text(
            """
            ALTER TABLE platform_blog_posts
            ADD COLUMN scheduled_for TIMESTAMP
            """
        )
    )


async def init_db() -> None:
    """Initialize database (create tables and apply idempotent schema patches)."""
    from app.db.models import Base

    async def _create_extension_and_tables(conn) -> None:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)

    migration_steps: list[tuple[str, Callable[..., Awaitable[None]]]] = [
        ("create_extension_and_tables", _create_extension_and_tables),
        ("platform_admins_role", _ensure_platform_admins_role_column),
        ("platform_admin_invited_by", _ensure_platform_admin_invited_by_column),
        ("platform_blog_posts", _ensure_platform_blog_post_columns),
        ("conversation_share", _ensure_conversation_share_columns),
        ("workspace_invitations_table", _ensure_workspace_invitations_table),
        ("workspace_invitation_varchar", _ensure_workspace_invitation_varchar_columns),
        ("seats_role_varchar", _ensure_seats_role_varchar_columns),
        ("userrole_enum_values", _ensure_userrole_enum_values),
        ("workspace_tasks_table", _ensure_workspace_tasks_table),
        ("workspace_task_document", _ensure_workspace_task_document_column),
        ("workspace_task_document_ids", _ensure_workspace_task_document_ids_column),
        ("workspace_workflows_table", _ensure_workspace_workflows_table),
        ("workflow_activities_table", _ensure_workflow_activities_table),
        ("workspace_task_workflow", _ensure_workspace_task_workflow_column),
        ("document_workflow", _ensure_document_workflow_column),
        ("task_resolution", _ensure_task_resolution_columns),
        ("task_approval_ai_reviews", _ensure_task_approval_ai_reviews_column),
        ("workflow_findings_table", _ensure_workflow_findings_table),
        ("conversation_workflow", _ensure_conversation_workflow_columns),
        ("compliance_tables", _ensure_compliance_tables),
        ("member_feature_permissions", _ensure_member_feature_permissions_columns),
        ("workspace_member_credentials_table", _ensure_workspace_member_credentials_table),
        ("user_password_flags_table", _ensure_user_password_flags_table),
        ("notification_type_enum", _ensure_notification_type_enum_values),
    ]

    for name, step in migration_steps:
        await _run_migration_step(name, step)


async def retry_optional_migrations() -> bool:
    """Kept for API compatibility; all migrations run at startup."""
    return True


async def _ensure_member_feature_permissions_columns(conn) -> None:
    if not await _column_exists(conn, "seats", "feature_permissions"):
        await conn.execute(
            text("ALTER TABLE seats ADD COLUMN feature_permissions JSONB")
        )
    if not await _column_exists(conn, "workspace_invitations", "feature_permissions"):
        await conn.execute(
            text("ALTER TABLE workspace_invitations ADD COLUMN feature_permissions JSONB")
        )


async def _ensure_workspace_member_credentials_table(conn) -> None:
    """Legacy table — no longer used for admin password retrieval."""
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS workspace_member_credentials (
                seat_id UUID PRIMARY KEY REFERENCES seats(id) ON DELETE CASCADE,
                provisioned_password_encrypted VARCHAR(512) NOT NULL,
                created_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc')
            )
            """
        )
    )


async def _ensure_user_password_flags_table(conn) -> None:
    """Track forced password changes without ALTER TABLE users (avoids lock on hot table)."""
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS user_password_flags (
                user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                must_change_password BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc')
            )
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
    await _ensure_fk(
        conn,
        "workspace_tasks",
        "document_id",
        "documents",
        "fk_workspace_tasks_document_id",
    )


async def _ensure_workspace_task_document_ids_column(conn) -> None:
    """Multiple document attachments on compliance tasks."""
    created = not await _column_exists(conn, "workspace_tasks", "document_ids")
    if created:
        await conn.execute(
            text(
                """
                ALTER TABLE workspace_tasks
                ADD COLUMN document_ids JSONB DEFAULT '[]'::jsonb
                """
            )
        )
    if created:
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
    await _ensure_fk(
        conn,
        "workspace_tasks",
        "workflow_id",
        "workspace_workflows",
        "fk_workspace_tasks_workflow_id",
    )


async def _ensure_document_workflow_column(conn) -> None:
    await _ensure_fk(
        conn,
        "documents",
        "workflow_id",
        "workspace_workflows",
        "fk_documents_workflow_id",
    )


async def _ensure_task_approval_ai_reviews_column(conn) -> None:
    if await _column_exists(conn, "workspace_tasks", "approval_ai_reviews"):
        return
    await conn.execute(
        text(
            """
            ALTER TABLE workspace_tasks
            ADD COLUMN approval_ai_reviews JSONB DEFAULT '[]'
            """
        )
    )


async def _ensure_task_resolution_columns(conn) -> None:
    if not await _column_exists(conn, "workspace_tasks", "resolution_result"):
        await conn.execute(
            text("ALTER TABLE workspace_tasks ADD COLUMN resolution_result JSONB")
        )
    await _ensure_fk(
        conn,
        "workspace_tasks",
        "resolution_document_id",
        "documents",
        "fk_workspace_tasks_resolution_document_id",
    )
    await _ensure_fk(
        conn,
        "workspace_tasks",
        "execution_conversation_id",
        "conversations",
        "fk_workspace_tasks_execution_conversation_id",
    )
    if not await _column_exists(conn, "workspace_tasks", "resolution_history"):
        await conn.execute(
            text(
                """
                ALTER TABLE workspace_tasks
                ADD COLUMN resolution_history JSONB DEFAULT '[]'
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
    await _ensure_fk(
        conn,
        "conversations",
        "workflow_id",
        "workspace_workflows",
        "fk_conversations_workflow_id",
    )
    await _ensure_fk(
        conn,
        "conversations",
        "task_id",
        "workspace_tasks",
        "fk_conversations_task_id",
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
