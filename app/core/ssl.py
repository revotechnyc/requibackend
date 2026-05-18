"""TLS for uvicorn on port 8000 — same certbot files as Node https.createServer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Node: fs.readFileSync('/etc/letsencrypt/live/requi.io/...')
LETSENCRYPT_CERT = Path("/etc/letsencrypt/live/requi.io/fullchain.pem")
LETSENCRYPT_KEY = Path("/etc/letsencrypt/live/requi.io/privkey.pem")


def uvicorn_ssl_kwargs() -> dict[str, Any]:
    """Use certbot certs when present (production server). Otherwise plain HTTP (local dev)."""
    if LETSENCRYPT_CERT.is_file() and LETSENCRYPT_KEY.is_file():
        print(f"[SSL] HTTPS on port 8000 ({LETSENCRYPT_CERT.parent.name})")
        return {
            "ssl_certfile": str(LETSENCRYPT_CERT.resolve()),
            "ssl_keyfile": str(LETSENCRYPT_KEY.resolve()),
        }
    return {}
