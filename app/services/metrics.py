"""Метрики для оператора в формате Prometheus, вычисляемые из PostgreSQL по запросу.

Счётчиков в памяти процессов нет: все числа берутся из постоянных таблиц, поэтому одинаковы для всех служб и переживают перезапуск.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

DELIVERY_STATUSES = ('pending', 'sending', 'sent', 'retry', 'failed', 'skipped', 'uncertain')
PROVIDERS = ('openai', 'gemini', 'bfl', 'ideogram', 'stability')


def _label(value: Any) -> str:
    """Подготовить значение метки: экранировать обратную косую, кавычки и переводы строк."""
    return str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')


class Exposition:
    """Составитель текста метрик: описания HELP/TYPE выводятся один раз на метрику."""

    def __init__(self) -> None:
        """Подготовить пустой список строк и множество уже объявленных метрик."""
        self.lines: list[str] = []
        self._declared: set[str] = set()

    def add(self, name: str, help_text: str, value: float | int | Decimal, labels: dict[str, Any] | None = None,
            kind: str = 'gauge') -> None:
        """Добавить значение метрики с метками и (при первом добавлении) строки HELP и TYPE."""
        if name not in self._declared:
            self._declared.add(name)
            self.lines += [f'# HELP {name} {help_text}', f'# TYPE {name} {kind}']
        tag = '{' + ','.join(f'{k}="{_label(v)}"' for k, v in sorted((labels or {}).items())) + '}' if labels else ''
        self.lines.append(f'{name}{tag} {float(value):.6g}')

    def render(self) -> str:
        """Собрать текст метрик целиком (строки через перевод строки, с завершающим переводом)."""
        return '\n'.join(self.lines) + '\n'


async def collect(connection: Any, monthly_budget_usd: Decimal | float = 10) -> Exposition:
    """Собрать метрики: доставки по статусам и возраст самой старой неотправленной, отправленные за час и неудачные за сутки, сигналы жизни служб, число чатов и подписок, расход бюджета картинок за месяц, задания и блокировки провайдеров, возраст бэкапов и анонимное использование функций за 7 дней."""
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
    for row in await connection.fetch("SELECT name,runs,errors,last_duration_ms,EXTRACT(EPOCH FROM now()-COALESCE(last_ok_at,'epoch'))::double precision AS age FROM loop_stats"):
        label = {'loop': row['name']}
        out.add('bible_loop_duration_ms', 'Duration of the last pass of a service loop', row['last_duration_ms'], label)
        out.add('bible_loop_errors_total', 'Failed passes of a service loop since start of statistics', row['errors'], label)
        out.add('bible_loop_last_ok_age_seconds', 'Seconds since a service loop last finished without error', row['age'], label)
    out.add('bible_announcements_active', 'Active scheduled announcements',
            await connection.fetchval("SELECT count(*) FROM announcements WHERE status='active'"))
    out.add('bible_announcements_overdue', 'Active announcements whose next run is more than 15 minutes late',
            await connection.fetchval("SELECT count(*) FROM announcements WHERE status='active' AND next_run_at<now()-interval '15 minutes'"))
    out.add('bible_source_posts_new', 'Collected source posts waiting for a decision',
            await connection.fetchval("SELECT count(*) FROM source_posts WHERE status='new'"))
    out.add('bible_video_feeds_failing', 'Enabled video feeds whose last poll failed',
            await connection.fetchval('SELECT count(*) FROM video_feeds WHERE is_enabled AND error_count>0'))
    out.add('bible_vk_inbox_pending', 'VK events waiting to be processed',
            await connection.fetchval("SELECT count(*) FROM vk_inbox WHERE status IN ('pending','processing')"))
    return out
