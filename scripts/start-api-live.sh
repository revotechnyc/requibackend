#!/usr/bin/env bash
# Start API against live Supabase.
# - Local machine (IPv6): docker-compose.live.yml (host network + direct DATABASE_URL)
# - AWS EC2 (IPv4 only): docker-compose.production.yml when DATABASE_POOLER_URL is set
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

COMPOSE_FILE="docker-compose.live.yml"
if grep -qE '^DATABASE_POOLER_URL=.+' .env 2>/dev/null; then
  COMPOSE_FILE="docker-compose.production.yml"
  echo "Using production compose (IPv4 Session Pooler from DATABASE_POOLER_URL)"
else
  echo "Using live compose (host network — needs IPv6 for direct Supabase DATABASE_URL)"
  echo "On AWS EC2, set DATABASE_POOLER_URL in .env (Supabase → Connect → Session)."
fi

${COMPOSE_CMD} down --remove-orphans 2>/dev/null || true
${COMPOSE_CMD} -f "${COMPOSE_FILE}" up --build -d "$@"

API_CID="$(${COMPOSE_CMD} -f "${COMPOSE_FILE}" ps -q api 2>/dev/null | head -1 || true)"
API_NAME="$(docker ps --filter "id=${API_CID}" --format '{{.Names}}' 2>/dev/null || true)"

echo ""
echo "Stack started (${COMPOSE_FILE})."
echo "Health: curl http://localhost:8000/health"
if [[ -n "${API_NAME}" ]]; then
  echo "Logs:  docker logs -f ${API_NAME}"
else
  echo "Logs:  docker compose -f ${COMPOSE_FILE} logs -f api"
fi
