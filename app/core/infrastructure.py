"""
Redis & Celery connectivity checks for startup and health endpoints.

Celery is used for: document ingestion, daily knowledge updates, gap resolution,
and admin-triggered jobs. Redis backs the Celery broker/result backend (and cache URL).
Workers must be started separately: celery -A app.tasks.celery_app worker
"""

from dataclasses import dataclass
from typing import Optional

import redis

from app.core.config import settings


@dataclass
class ServiceCheck:
    name: str
    ok: bool
    detail: str
    url_hint: str = ""


def _ping_redis(url: str, socket_timeout: float = 2.0) -> tuple[bool, str]:
    try:
        client = redis.from_url(url, socket_connect_timeout=socket_timeout)
        client.ping()
        return True, "connected"
    except redis.ConnectionError as e:
        return False, f"connection refused ({e})"
    except redis.TimeoutError:
        return False, "connection timed out"
    except Exception as e:
        return False, str(e)


def check_redis_cache() -> ServiceCheck:
    ok, detail = _ping_redis(settings.redis_url)
    return ServiceCheck(
        name="Redis (cache)",
        ok=ok,
        detail=detail,
        url_hint=settings.redis_url,
    )


def check_celery_broker() -> ServiceCheck:
    ok, detail = _ping_redis(settings.celery_broker_url)
    return ServiceCheck(
        name="Celery broker",
        ok=ok,
        detail=detail,
        url_hint=settings.celery_broker_url,
    )


def check_celery_result_backend() -> ServiceCheck:
    ok, detail = _ping_redis(settings.celery_result_backend)
    return ServiceCheck(
        name="Celery result backend",
        ok=ok,
        detail=detail,
        url_hint=settings.celery_result_backend,
    )


def run_infrastructure_checks() -> list[ServiceCheck]:
    return [
        check_redis_cache(),
        check_celery_broker(),
        check_celery_result_backend(),
    ]


def celery_is_ready(checks: Optional[list[ServiceCheck]] = None) -> bool:
    """Broker + result backend must both be up for .delay() to work."""
    checks = checks or run_infrastructure_checks()
    by_name = {c.name: c for c in checks}
    return (
        by_name.get("Celery broker", ServiceCheck("", False, "")).ok
        and by_name.get("Celery result backend", ServiceCheck("", False, "")).ok
    )


def log_startup_infrastructure() -> bool:
    """
    Print Redis/Celery status at API startup.
    Returns True if Celery can accept background jobs (broker + backend up).
    """
    checks = run_infrastructure_checks()
    celery_ready = celery_is_ready(checks)

    print("")
    print("── Infrastructure ──────────────────────────────────────")
    for check in checks:
        tag = "OK  " if check.ok else "WARN"
        print(f"  [{tag}] {check.name}: {check.detail}")
        if not check.ok:
            print(f"         → {check.url_hint}")

    if celery_ready:
        print("  [OK  ] Celery: broker ready (start workers separately if not running)")
        print("         celery -A app.tasks.celery_app worker --loglevel=info")
        print("         celery -A app.tasks.celery_app beat --loglevel=info  # optional")
    else:
        print("  [WARN] Celery: background jobs disabled until Redis broker/backend are up")
        print("         Ingestion, daily updates, and async admin tasks will fail on .delay()")

    print("──────────────────────────────────────────────────────")
    print("")
    return celery_ready
