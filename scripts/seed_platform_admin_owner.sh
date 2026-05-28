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

if ! command -v docker-compose >/dev/null 2>&1; then
  echo "ERROR: docker-compose is required but not found in PATH." >&2
  exit 1
fi

echo "[seed] Ensuring api container is running..."
docker-compose up -d api >/dev/null

echo "[seed] Seeding platform admin owner..."
docker-compose exec -T \
  -e PLATFORM_ADMIN_SEED_EMAIL="${EMAIL}" \
  -e PLATFORM_ADMIN_SEED_PASSWORD="${PASSWORD}" \
  -e PLATFORM_ADMIN_SEED_FIRST_NAME="${FIRST_NAME}" \
  -e PLATFORM_ADMIN_SEED_LAST_NAME="${LAST_NAME}" \
  -e PYTHONPATH="/app" \
  api sh -lc 'cd /app && python scripts/seed_platform_admin.py'

echo "[seed] Done."

