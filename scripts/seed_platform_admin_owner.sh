#!/usr/bin/env bash
set -euo pipefail

# Seed / update the Requi SaaS Admin owner in the SERVER database.
#
# Usage (from repo root or from requi-backend):
#   bash requi-backend/scripts/seed_platform_admin_owner.sh
#   # or
#   cd requi-backend && bash scripts/seed_platform_admin_owner.sh
#
# Optional overrides:
#   PLATFORM_ADMIN_SEED_EMAIL="you@example.com" \
#   PLATFORM_ADMIN_SEED_PASSWORD="Secret123!" \
#   bash requi-backend/scripts/seed_platform_admin_owner.sh
#
# Notes:
# - Runs inside the `requi-backend` docker-compose `api` service so it targets the same DB as the server.
# - Does NOT alter customer org membership/roles; it ensures the platform-admin owner exists in `platform_admins`.

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

EMAIL="${PLATFORM_ADMIN_SEED_EMAIL:-nuvostudios99@gmail.com}"
PASSWORD="${PLATFORM_ADMIN_SEED_PASSWORD:-Ramadan21$}"
FIRST_NAME="${PLATFORM_ADMIN_SEED_FIRST_NAME:-Angelo}"
LAST_NAME="${PLATFORM_ADMIN_SEED_LAST_NAME:-Admin}"

cd "${BACKEND_DIR}"

COMPOSE_CMD="${COMPOSE_CMD:-}"
if [[ -z "${COMPOSE_CMD}" ]]; then
  if command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
  elif command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
  fi
fi

if [[ -z "${COMPOSE_CMD}" ]]; then
  echo "ERROR: Docker Compose not found." >&2
  echo "Install one of these, or set COMPOSE_CMD explicitly:" >&2
  echo "  - docker compose   (Docker Compose v2 plugin)" >&2
  echo "  - docker-compose   (legacy v1 binary)" >&2
  echo "" >&2
  echo "Example:" >&2
  echo "  COMPOSE_CMD='docker compose' bash scripts/seed_platform_admin_owner.sh" >&2
  exit 1
fi

echo "[seed] Ensuring api container is running..."
${COMPOSE_CMD} up -d api >/dev/null

echo "[seed] Seeding platform admin owner..."
${COMPOSE_CMD} exec -T \
  -e PLATFORM_ADMIN_SEED_EMAIL="${EMAIL}" \
  -e PLATFORM_ADMIN_SEED_PASSWORD="${PASSWORD}" \
  -e PLATFORM_ADMIN_SEED_FIRST_NAME="${FIRST_NAME}" \
  -e PLATFORM_ADMIN_SEED_LAST_NAME="${LAST_NAME}" \
  -e PYTHONPATH="/app" \
  api sh -lc 'cd /app && python scripts/seed_platform_admin.py'

echo "[seed] Done."

