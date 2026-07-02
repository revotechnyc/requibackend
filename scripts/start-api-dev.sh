#!/usr/bin/env bash
# Start development API stack (api.dev.requi.io → host :8001).
# Isolated from production via compose project name "requi-dev".
#
# Usage:
#   ./scripts/start-api-dev.sh
#   ./scripts/start-api-dev.sh --build
#   ./scripts/start-api-dev.sh down
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-requi-dev}"
COMPOSE_FILE="docker-compose.dev.yml"

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

if [[ ! -f "${COMPOSE_FILE}" ]]; then
  echo "ERROR: ${COMPOSE_FILE} not found in $(pwd)" >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "ERROR: .env not found. Copy .env.example to .env and set dev DATABASE_URL / keys." >&2
  exit 1
fi

compose() {
  ${COMPOSE_CMD} -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" "$@"
}

if [[ "${1:-}" == "down" ]]; then
  compose down --remove-orphans
  echo "Dev stack stopped (project: ${COMPOSE_PROJECT_NAME})."
  exit 0
fi

if [[ "${1:-}" == "logs" ]]; then
  compose logs -f "${2:-api}"
  exit 0
fi

compose down --remove-orphans 2>/dev/null || true
compose up --build -d "$@"

API_CID="$(compose ps -q api 2>/dev/null | head -1 || true)"
API_NAME="$(docker ps --filter "id=${API_CID}" --format '{{.Names}}' 2>/dev/null || true)"

echo ""
echo "Dev stack started (project: ${COMPOSE_PROJECT_NAME}, file: ${COMPOSE_FILE})."
echo "  API:    http://localhost:8001/health"
echo "  Redis:  localhost:6381"
echo "  Flower: http://localhost:5556"
echo ""
echo "Stop:  ./scripts/start-api-dev.sh down"
if [[ -n "${API_NAME}" ]]; then
  echo "Logs:  docker logs -f ${API_NAME}"
else
  echo "Logs:  ./scripts/start-api-dev.sh logs api"
fi
