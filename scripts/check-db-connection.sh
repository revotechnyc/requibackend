#!/usr/bin/env bash
# Quick DB connectivity check (run on server before/after deploy).
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
if [[ -x ./venv/bin/python ]]; then
  PYTHON="./venv/bin/python"
elif docker compose ps -q api >/dev/null 2>&1 && [[ -n "$(docker compose ps -q api 2>/dev/null)" ]]; then
  CID="$(docker compose ps -q api | head -1)"
  docker exec "$CID" python -c "
import asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from app.core.config import settings
from app.db.database import get_async_database_url, _engine_connect_args
import re

async def main():
    url = settings.database_url or ''
    host = re.sub(r'.*@([^/]+)/.*', r'\\1', url)
    host = re.sub(r':[^:@]+$', '', host)
    print('DB host:', host)
    print('Using pooler:', 'pooler.supabase.com' in url)
    engine = create_async_engine(get_async_database_url(), connect_args=_engine_connect_args(), pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            v = await conn.execute(text('SELECT 1'))
            print('Connection: OK', v.scalar())
    except Exception as e:
        print('Connection: FAILED')
        print(e)
        if '101' in str(e) or 'Network is unreachable' in str(e):
            print('')
            print('Hint: AWS EC2 is IPv4-only. Set DATABASE_POOLER_URL in .env')
            print('(Supabase Dashboard → Connect → Session pooler string).')
        raise SystemExit(1)
    finally:
        await engine.dispose()

asyncio.run(main())
"
  exit $?
fi

"$PYTHON" -c "
import asyncio, re
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from app.core.config import settings
from app.db.database import get_async_database_url, _engine_connect_args

async def main():
    url = settings.database_url or ''
    print('DB host:', re.sub(r'.*@([^/]+)/.*', r'\\1', url))
    print('Using pooler:', 'pooler.supabase.com' in url)
    engine = create_async_engine(get_async_database_url(), connect_args=_engine_connect_args())
    async with engine.connect() as c:
        print('Connection: OK', (await c.execute(text('SELECT 1'))).scalar())
    await engine.dispose()

asyncio.run(main())
"
