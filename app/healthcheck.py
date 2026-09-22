"""Container-local process heartbeat probe; does not contact Telegram."""
from __future__ import annotations
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from app.config import Settings
from app.db import normalize_asyncpg_dsn


def container_started_at(proc_root: Path = Path('/proc')) -> datetime | None:
    """Read Linux PID 1 start time; reject heartbeats from an earlier container run.

    Kernel boot time is rounded to whole seconds. Add one second so rounding can
    delay readiness briefly but can never admit a heartbeat from before startup.
    This probe is for Docker/Linux and fails closed when process metadata is absent.
    """
    try:
        fields = (proc_root / '1' / 'stat').read_text().rpartition(')')[2].split()
        start_ticks = int(fields[19])  # proc(5) field 22, after PID and comm
        boot_line = next(line for line in (proc_root / 'stat').read_text().splitlines()
                         if line.startswith('btime '))
        boot_seconds = int(boot_line.split()[1])
        ticks_per_second = os.sysconf('SC_CLK_TCK')
        return datetime.fromtimestamp(boot_seconds + start_ticks / ticks_per_second + 1,
                                      tz=timezone.utc)
    except (OSError, ValueError, IndexError, StopIteration, AttributeError, ZeroDivisionError):
        return None


async def probe(service: str) -> bool:
    """Require a recent heartbeat produced after this container started."""
    if service not in {'bot','worker'}:
        return False
    started_at = container_started_at()
    if started_at is None:
        return False
    import asyncpg
    connection = await asyncpg.connect(normalize_asyncpg_dsn(Settings.from_env(require_bot_token=False).database_url),timeout=5)
    try:
        return bool(await connection.fetchval("SELECT last_seen>now()-interval '180 seconds' AND last_seen>=$2 FROM service_heartbeats WHERE service=$1",service,started_at))
    finally:
        await connection.close()


if __name__=='__main__':
    try:
        result = asyncio.run(probe(sys.argv[1]))
    except Exception:
        result = False
    raise SystemExit(0 if result else 1)
