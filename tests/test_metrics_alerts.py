import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.alerts import due, evaluate, samples_from
from app.services.metrics import Exposition, collect


class FakeConnection:
    async def fetch(self, sql, *args):
        if 'usage_events' in sql:
            return []
        if 'FROM delivery_log GROUP BY status' in sql:
            return [{'status': 'sent', 'n': 5}, {'status': 'uncertain', 'n': 1}]
        if 'service_heartbeats' in sql:
            return [{'service': 'worker', 'age': 500.0}]
        if 'image_generation_jobs' in sql:
            return [{'state': 'ready', 'n': 2}]
        if 'image_provider_health' in sql:
            return [{'provider': 'gemini'}]
        if 'cloud_backup_receipts' in sql:
            return [{'kind': 'database', 'age': 200000.0}]
        return []

    async def fetchval(self, sql, *args):
        if 'image_generation_attempts' in sql:
            return Decimal('9.1')
        if 'min(scheduled_for)' in sql:
            return 4000
        return 3


def test_exposition_format_and_escaping():
    e = Exposition()
    e.add('x_total', 'help', 3, {'a': 'q"\n'})
    e.add('x_total', 'help', 4, {'a': 'b'})
    text = e.render()
    assert text.count('# HELP x_total') == 1
    assert 'x_total{a="q\\" "} 3' in text and text.endswith('\n')


@pytest.mark.asyncio
async def test_collect_and_evaluate_roundtrip():
    exposition = await collect(FakeConnection(), Decimal(10))
    alerts = evaluate(samples_from(exposition))
    assert set(alerts) == {'heartbeat:worker', 'backup:database', 'provider:gemini', 'delivery:stuck',
                           'delivery:uncertain', 'budget:images'}


def test_healthy_state_has_no_alerts():
    assert evaluate({'bible_service_heartbeat_age_seconds|worker': 10, 'bible_deliveries|uncertain': 0,
                     'bible_image_budget_limit_usd': 10, 'bible_image_budget_spent_usd': 1}) == {}


def test_due_dedups_repeats_and_reports_recovery():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    repeat = timedelta(hours=6)
    msgs, state = due({}, {'a': 'A'}, now, repeat)
    assert msgs == ['A']
    msgs, state = due(state, {'a': 'A'}, now + timedelta(hours=1), repeat)
    assert msgs == [] and 'a' in state
    msgs, _ = due(state, {'a': 'A'}, now + timedelta(hours=7), repeat)
    assert msgs == ['A']
    msgs, state = due(state, {}, now + timedelta(hours=8), repeat)
    assert msgs == ['Восстановлено: a'] and state == {}


from tests.test_postgres_integration import db as db  # noqa: E402,F401
from tests.test_postgres_integration import create_destination  # noqa: E402


@pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='needs PostgreSQL')
@pytest.mark.asyncio
async def test_collect_runs_against_real_schema(db):
    db = db[0]
    await create_destination(db)
    await db.execute("INSERT INTO service_heartbeats(service) VALUES('worker')")
    text = (await collect(db, 10)).render()
    assert 'bible_chats_active 1' in text
    assert 'bible_service_heartbeat_age_seconds{service="worker"}' in text
    assert 'bible_image_budget_spent_usd 0' in text
    from app.services import usage
    await usage.record(db, 'cmd:daily')
    await usage.record(db, 'cmd:daily')
    await usage.record(db, 'Not Valid!')
    assert await usage.top_events(db) == [{'platform': 'telegram', 'event': 'cmd:daily', 'total': 2}]
    assert 'bible_usage_events_7d{event="cmd:daily",platform="telegram"} 2' in (await collect(db, 10)).render()
    assert evaluate(samples_from(await collect(db, 10))) == {}
