"""Optional TLS for uvicorn (Let's Encrypt / certbot), same paths as Node https.createServer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.config import settings


def uvicorn_ssl_kwargs() -> dict[str, Any]:
    """
    Return ssl_certfile / ssl_keyfile for uvicorn when SSL_ENABLED=true.

    Example .env (production on requi.io):
      SSL_ENABLED=true
      SSL_CERTFILE=/etc/letsencrypt/live/requi.io/fullchain.pem
      SSL_KEYFILE=/etc/letsencrypt/live/requi.io/privkey.pem
      SSL_PORT=443
    """
    if not settings.ssl_enabled:
        return {}

    certfile = (settings.ssl_certfile or "").strip()
    keyfile = (settings.ssl_keyfile or "").strip()
    if not certfile or not keyfile:
        raise ValueError(
            "SSL_ENABLED=true requires SSL_CERTFILE and SSL_KEYFILE in .env "
            "(e.g. certbot paths under /etc/letsencrypt/live/<domain>/)"
        )

    cert_path = Path(certfile)
    key_path = Path(keyfile)
    if not cert_path.is_file():
        raise FileNotFoundError(
            f"SSL certificate not found: {certfile}. "
            "Run certbot or set SSL_CERTFILE to fullchain.pem."
        )
    if not key_path.is_file():
        raise FileNotFoundError(
            f"SSL private key not found: {keyfile}. "
            "Run certbot or set SSL_KEYFILE to privkey.pem."
        )

    return {
        "ssl_certfile": str(cert_path.resolve()),
        "ssl_keyfile": str(key_path.resolve()),
    }
