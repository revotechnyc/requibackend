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

# Convert PostgreSQL URL to async version
def get_async_database_url() -> str:
    """Convert sync PostgreSQL URL to async"""
    url = settings.database_url
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url

# Create async engine
async_engine = create_async_engine(
    get_async_database_url(),
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    pool_pre_ping=True,
    echo=settings.debug,
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
        await _ensure_userrole_enum_values(conn)


async def _ensure_userrole_enum_values(conn) -> None:
    """Add reviewer/contributor to PG userrole enum when missing (safe re-deploy)."""
    for value in ("reviewer", "contributor"):
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
