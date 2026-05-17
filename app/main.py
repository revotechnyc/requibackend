"""
Requi Health API - Main application
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.api.routes import api_router
from app.core.config import settings
from app.core.infrastructure import (
    celery_is_ready,
    log_startup_infrastructure,
    run_infrastructure_checks,
)
from app.db.database import close_db, init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler"""
    # Startup
    print(f"Starting {settings.app_name}...")
    await init_db()
    print("[OK  ] Database: tables ready")

    celery_ready = log_startup_infrastructure()
    app.state.celery_ready = celery_ready
    app.state.infrastructure_checks = run_infrastructure_checks()

    yield

    # Shutdown
    print("Shutting down...")
    await close_db()
    print("Database connections closed")


# Create FastAPI app
app = FastAPI(
    title=settings.app_name,
    description="Healthcare Compliance AI Platform API",
    version="1.0.0",
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
    lifespan=lifespan,
)

# Local Vite dev servers only (_dev_cors_origins is NOT used on EC2 when APP_ENV=production)
_dev_cors_origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]


def _normalize_origin(origin: str) -> str:
    return origin.strip().rstrip("/")


def _cors_allow_origins() -> list[str]:
    """Dev: localhost + CORS_ORIGINS. Production: CORS_ORIGINS only (unless allow-all)."""
    combined: list[str] = list(_dev_cors_origins) if settings.is_development else []
    combined.extend(settings.cors_origins_list)
    seen: set[str] = set()
    result: list[str] = []
    for origin in combined:
        normalized = _normalize_origin(origin)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


# Any http(s) frontend origin (credentials + Authorization header both work)
_CORS_ALLOW_ALL_REGEX = r"https?://.*"

if settings.cors_allow_all_enabled:
    print("[CORS] Allowing all origins (CORS_ALLOW_ALL=true or CORS_ORIGINS=*)")
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=_CORS_ALLOW_ALL_REGEX,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    _origins = _cors_allow_origins()
    if settings.is_production and not _origins:
        print("[CORS] Warning: no CORS_ORIGINS in production — browsers may block cross-origin calls")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.add_middleware(GZipMiddleware, minimum_size=1000)

# Include API routes
app.include_router(api_router, prefix=settings.api_v1_prefix)


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "name": settings.app_name,
        "version": "1.0.0",
        "status": "healthy",
    }


@app.get("/health")
async def health_check():
    """Health check endpoint (database + Redis/Celery infrastructure)."""
    from sqlalchemy import text

    from app.db.database import async_engine

    checks = run_infrastructure_checks()
    celery_ready = celery_is_ready(checks)
    by_name = {c.name: c for c in checks}

    db_status = "connected"
    try:
        async with async_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        db_status = "disconnected"

    infra_ok = all(c.ok for c in checks)
    overall = db_status == "connected" and infra_ok

    def _status(check_name: str) -> str:
        c = by_name.get(check_name)
        return "connected" if c and c.ok else "disconnected"

    return {
        "status": "healthy" if overall else "degraded",
        "environment": settings.app_env,
        "database": db_status,
        "redis_cache": _status("Redis (cache)"),
        "celery_broker": _status("Celery broker"),
        "celery_result_backend": _status("Celery result backend"),
        "celery_ready": celery_ready,
        "message": (
            "All services connected"
            if overall
            else "API runs but background jobs need Redis; start redis-server and celery worker"
        ),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.is_development,
    )
