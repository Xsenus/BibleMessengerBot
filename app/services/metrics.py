"""Operational metrics in Prometheus text format, computed from PostgreSQL on demand.

No extra dependency and no in-process counters: every number is derived from durable
tables, so all services (and restarts) report consistent values.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

DELIVERY_STATUSES = ('pending', 'sending', 'sent', 'retry', 'failed', 'skipped', 'uncertain')
PROVIDERS = ('openai', 'gemini', 'bfl', 'ideogram', 'stability')


def _label(value: Any) -> str:
    return str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')


class Exposition:
    """Minimal text-format writer (HELP/TYPE emitted once per metric)."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._declared: set[str] = set()

    def add(self, name: str, help_text: str, value: float | int | Decimal, labels: dict[str, Any] | None = None,
            kind: str = 'gauge') -> None:
        if name not in self._declared:
            self._declared.add(name)
            self.lines += [f'# HELP {name} {help_text}', f'# TYPE {name} {kind}']
        tag = '{' + ','.join(f'{k}="{_label(v)}"' for k, v in sorted((labels or {}).items())) + '}' if labels else ''
        self.lines.append(f'{name}{tag} {float(value):.6g}')

    def render(self) -> str:
        return '\n'.join(self.lines) + '\n'


async def collect(connection: Any, monthly_budget_usd: Decimal | float = 10) -> Exposition:
    out = Exposition()
    counts = {r['status']: r['n'] for r in await connection.fetch('SELECT status,count(*) AS n FROM delivery_log GROUP BY status')}
    for status in DELIVERY_STATUSES:
        out.add('bible_deliveries', 'Deliveries by status', counts.get(status, 0), {'status': status})
    out.add('bible_oldest_outstanding_delivery_seconds', 'Age of the oldest delivery that is not yet sent',
            await connection.fetchval("""SELECT COALESCE(EXTRACT(EPOCH FROM now()-min(scheduled_for)),0) FROM delivery_log
                WHERE status IN ('pending','retry','sending','uncertain') AND scheduled_for<now()""") or 0)
    out.add('bible_deliveries_sent_last_hour', 'Deliveries completed during the last hour',
            await connection.fetchval("SELECT count(*) FROM delivery_log WHERE status='sent' AND sent_at>now()-interval '1 hour'"))
    out.add('bible_deliveries_failed_last_day', 'Deliveries that failed during the last 24 hours',
            await connection.fetchval("SELECT count(*) FROM delivery_log WHERE status='failed' AND updated_at>now()-interval '1 day'"))
    for row in await connection.fetch('SELECT service,EXTRACT(EPOCH FROM now()-last_seen) AS age FROM service_heartbeats'):
        out.add('bible_service_heartbeat_age_seconds', 'Seconds since a service last reported', row['age'], {'service': row['service']})
    out.add('bible_chats_active', 'Active destinations', await connection.fetchval('SELECT count(*) FROM telegram_chats WHERE is_active'))
    out.add('bible_subscriptions_enabled', 'Enabled subscriptions', await connection.fetchval('SELECT count(*) FROM subscriptions WHERE is_enabled'))
    spent = await connection.fetchval("""SELECT COALESCE(sum(reserved_usd),0) FROM image_generation_attempts
        WHERE state IN ('reserved','succeeded','uncertain') AND created_at>=date_trunc('month',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'""")
    out.add('bible_image_budget_spent_usd', 'Image budget reserved/spent this UTC month', spent or 0)
    out.add('bible_image_budget_limit_usd', 'Configured monthly image budget', Decimal(str(monthly_budget_usd)))
    for state, n in [(r['state'], r['n']) for r in await connection.fetch('SELECT state,count(*) AS n FROM image_generation_jobs GROUP BY state')]:
        out.add('bible_image_jobs', 'Image generation jobs by state', n, {'state': state})
    blocked = {r['provider'] for r in await connection.fetch('SELECT provider FROM image_provider_health WHERE blocked_until>now()')}
    for provider in PROVIDERS:
        out.add('bible_image_provider_blocked', '1 while a provider is temporarily blocked', int(provider in blocked), {'provider': provider})
    backups = {r['kind']: r['age'] for r in await connection.fetch(
        'SELECT kind,EXTRACT(EPOCH FROM now()-max(verified_at)) AS age FROM cloud_backup_receipts GROUP BY kind')}
    for kind, age in backups.items():
        out.add('bible_backup_age_seconds', 'Seconds since the last verified cloud backup', age, {'kind': kind})
    for row in await connection.fetch("""SELECT platform,event,sum(count)::bigint AS n FROM usage_events
            WHERE day>(now() AT TIME ZONE 'UTC')::date-7 GROUP BY platform,event ORDER BY n DESC LIMIT 30"""):
        out.add('bible_usage_events_7d', 'Anonymous feature usage during the last 7 days', row['n'],
                {'platform': row['platform'], 'event': row['event']})
    return out
