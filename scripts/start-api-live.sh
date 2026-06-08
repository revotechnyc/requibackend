#!/usr/bin/env bash
# Start API against live Supabase DATABASE_URL (Docker bridge lacks IPv6).
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE_CMD="${COMPOSE_CMD:-}"
if [[ -z "${COMPOSE_CMD}" ]]; then
  if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
  fi
fi

if [[ -z "${COMPOSE_CMD}" ]]; then
  echo "ERROR: Docker Compose not found. Install 'docker compose' (v2) or set COMPOSE_CMD." >&2
  exit 1
fi

${COMPOSE_CMD} down --remove-orphans 2>/dev/null || true
${COMPOSE_CMD} -f docker-compose.live.yml up --build -d "$@"
echo ""
echo "Live stack started. API: http://localhost:8000/health"
echo "Logs: docker logs -f requi-backend_api_1"
