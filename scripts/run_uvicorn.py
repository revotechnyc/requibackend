#!/usr/bin/env python3
"""
Production/staging entrypoint for the API (HTTP or HTTPS).

  # Local HTTP (default)
  python scripts/run_uvicorn.py

  # HTTPS with certbot (set SSL_* in .env first)
  python scripts/run_uvicorn.py

Docker / systemd can use the same script instead of bare uvicorn CLI.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running from repo root: requi-backend/
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.ssl import uvicorn_ssl_kwargs  # noqa: E402


def main() -> None:
    ssl_kwargs = uvicorn_ssl_kwargs()
    workers = 1 if settings.is_development or ssl_kwargs else 4

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.server_port,
        workers=workers,
        reload=settings.is_development and not ssl_kwargs,
        **ssl_kwargs,
    )


if __name__ == "__main__":
    main()
