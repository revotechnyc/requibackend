#!/usr/bin/env python3
"""Create or update the platform admin owner (run from requi-backend root)."""

import asyncio
import sys

from app.db.database import init_db
from app.db.platform_admin_seed import ensure_platform_admin_seed


async def main() -> None:
    await init_db()
    await ensure_platform_admin_seed()
    print("Platform admin seed complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"Seed failed: {exc}", file=sys.stderr)
        sys.exit(1)
