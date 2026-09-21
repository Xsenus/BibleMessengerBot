"""Container-local process heartbeat probe; does not contact Telegram."""
from __future__ import annotations
import asyncio
import sys
from app.config import Settings
from app.db import normalize_asyncpg_dsn


async def probe(service: str) -> bool:
    """Validate only known services, with a bounded database timeout."""
    if service not in {'bot','worker'}:
        return False
    import asyncpg
    connection = await asyncpg.connect(normalize_asyncpg_dsn(Settings.from_env(require_bot_token=False).database_url),timeout=5)
    try:
        return bool(await connection.fetchval("SELECT last_seen>now()-interval '180 seconds' FROM service_heartbeats WHERE service=$1",service))
    finally:
        await connection.close()


if __name__=='__main__':
    try:
        result = asyncio.run(probe(sys.argv[1]))
    except Exception:
        result = False
    raise SystemExit(0 if result else 1)
