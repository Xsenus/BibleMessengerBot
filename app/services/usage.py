"""Anonymous aggregate usage counters (what is used, never by whom).

Failures must never affect a user request, so recording swallows database errors.
"""
from __future__ import annotations

import logging
import re
from typing import Any

LOGGER = logging.getLogger(__name__)
EVENT = re.compile(r'^[a-z0-9_:.-]{1,40}$')


async def record(connection: Any, event: str, platform: str = 'telegram') -> None:
    """Increment today's (UTC) counter for an event such as 'cmd:daily' or 'cb:lang'."""
    if not EVENT.match(event):
        return
    try:
        await connection.execute(
            """INSERT INTO usage_events(day,platform,event,count) VALUES((now() AT TIME ZONE 'UTC')::date,$1,$2,1)
               ON CONFLICT(day,platform,event) DO UPDATE SET count=usage_events.count+1""", platform, event)
    except Exception as error:
        LOGGER.warning('usage counter skipped: %s', type(error).__name__)


async def top_events(connection: Any, days: int = 7, limit: int = 20) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        """SELECT platform,event,sum(count)::bigint AS total FROM usage_events
           WHERE day>(now() AT TIME ZONE 'UTC')::date-$1::int GROUP BY platform,event ORDER BY total DESC LIMIT $2""",
        days, limit)
    return [dict(r) for r in rows]
