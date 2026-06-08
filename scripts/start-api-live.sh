#!/usr/bin/env bash
# Start API against live Supabase DATABASE_URL (Docker bridge lacks IPv6).
set -euo pipefail
cd "$(dirname "$0")/.."
docker-compose down --remove-orphans 2>/dev/null || true
docker-compose -f docker-compose.live.yml up --build -d "$@"
echo ""
echo "Live stack started. API: http://localhost:8000/health"
echo "Logs: docker logs -f requi-backend_api_1"
