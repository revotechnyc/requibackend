"""
Requi Health API - Main application
"""

import asyncio
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import api_router
from app.core.config import settings
from app.core.infrastructure import (
    celery_is_ready,
    log_startup_infrastructure,
    run_infrastructure_checks,
)
from app.db.database import close_db, get_db_context, init_db
from app.db.platform_admin_seed import ensure_platform_admin_seed
from app.services.task_reminder_service import process_task_reminders


async def _run_task_reminders_once(delay_seconds: int = 90) -> None:
    """Fallback when Celery Beat is not running — one pass after API startup."""
    await asyncio.sleep(delay_seconds)
    try:
        async with get_db_context() as db:
            await process_task_reminders(db)
    except Exception as exc:
        print(f"[WARN] Task reminder startup run failed: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler"""
    # Startup
    print(f"Starting {settings.app_name}...")
    await init_db()
    print("[OK  ] Database: tables ready")
    await ensure_platform_admin_seed()
    print("[OK  ] Platform admin seed checked")

    celery_ready = log_startup_infrastructure()
    app.state.celery_ready = celery_ready
    app.state.infrastructure_checks = run_infrastructure_checks()

    if settings.task_reminder_enabled:
        asyncio.create_task(_run_task_reminders_once())

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
    "http://localhost:5174",
    "http://127.0.0.1:5174",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

# Frontend on :443 → API on :8000 is cross-origin; allow requi.io without extra .env
_requi_frontend_origins = [
    "https://requi.io",
    "https://www.requi.io",
    "http://requi.io",
    "http://www.requi.io",
]


def _normalize_origin(origin: str) -> str:
    return origin.strip().rstrip("/")


def _cors_allow_origins() -> list[str]:
    """Dev: localhost + CORS_ORIGINS. Production: CORS_ORIGINS only (unless allow-all)."""
    combined: list[str] = list(_dev_cors_origins) if settings.is_development else []
    combined.extend(_requi_frontend_origins)
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

# SaaS admin portal deployed on Netlify (preview + production URLs)
_NETLIFY_ADMIN_ORIGIN_REGEX = r"https://[a-z0-9-]+\.netlify\.app"

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
        allow_origin_regex=_NETLIFY_ADMIN_ORIGIN_REGEX,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.add_middleware(GZipMiddleware, minimum_size=1000)


def _cors_headers_for_request(request: Request) -> dict[str, str]:
    """Ensure error responses include CORS headers (browser otherwise reports CORS, not 500)."""
    origin = request.headers.get("origin")
    if not origin:
        return {}
    normalized = _normalize_origin(origin)
    if settings.cors_allow_all_enabled:
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
        }
    allowed = {_normalize_origin(o) for o in _cors_allow_origins()}
    if normalized in allowed or re.fullmatch(_NETLIFY_ADMIN_ORIGIN_REGEX, origin):
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
        }
    return {}


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return error detail in development to simplify debugging."""
    cors_headers = _cors_headers_for_request(request)
    if settings.debug:
        import traceback

        return JSONResponse(
            status_code=500,
            content={
                "detail": str(exc),
                "type": type(exc).__name__,
                "traceback": traceback.format_exc().splitlines()[-8:],
            },
            headers=cors_headers,
        )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error"},
        headers=cors_headers,
    )


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

    from app.core.ssl import uvicorn_ssl_kwargs

    ssl_kwargs = uvicorn_ssl_kwargs()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.is_development and not ssl_kwargs,
        **ssl_kwargs,
    )
