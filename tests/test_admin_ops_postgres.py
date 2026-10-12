"""Роли, журнал действий, шаблоны, предпросмотр, отчёты и импорт объявлений."""
import os

import pytest

from app.services import accounts, announcements, audit
from app.services.errors import UserError
from tests.test_admin_api_postgres import H, make, prepare
from tests.test_postgres_integration import db as db  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='needs PostgreSQL')]
ITEM = {'title': 'T', 'body': 'Привет', 'kind': 'once', 'start_at': '2099-01-01T10:00:00+00:00'}


async def test_roles_limit_what_an_admin_may_do(db):
    c = db[0]
    await prepare(c)                                         # 101 — владелец
    for user, role in ((202, 'viewer'), (303, 'editor')):
        await c.execute("INSERT INTO telegram_users(telegram_user_id,platform,timezone) VALUES($1,'telegram','UTC') ON CONFLICT DO NOTHING", user)
        code = await accounts.create_admin_invite(c, 101, role=role)
        assert await accounts.redeem_admin_invite(c, user, code)
    assert await accounts.role_of(c, 101) == 'owner' and await accounts.role_of(c, 202) == 'viewer'
    assert await accounts.role_of(c, 303) == 'editor' and await accounts.role_of(c, 999) is None
    chat = await c.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=101')
    with pytest.raises(UserError):
        await announcements.command(c, 202, chat, 'list')                    # наблюдатель не изменяет и не управляет
    text, _ = await announcements.command(c, 303, chat, 'list')              # редактор может
    assert text
    with pytest.raises(UserError):
        await accounts.create_admin_invite(c, 303)                           # приглашать может только владелец
    with pytest.raises(UserError):
        await accounts.create_admin_invite(c, 101, role='god')


async def test_audit_records_panel_and_hides_secrets(db, monkeypatch):
    c = db[0]
    await prepare(c)
    await audit.record(c, 101, 'bot', 'x.y', 'T', {'api_key': 'sk-123', 'note': 'ok'})
    entry = (await audit.recent(c, 5))[0]
    assert entry['details'] == {'api_key': '***', 'note': 'ok'}
    async with make(monkeypatch, c) as client:
        assert (await client.post('/api/admin/announcements', headers=H, json=ITEM)).status_code == 201
        assert (await client.post('/api/admin/announcements/preview', headers=H, json={'body': 'x'})).status_code == 200
        log = (await client.get('/api/admin/audit', headers=H)).json()['items']
    actions = [i['action'] for i in log]
    assert 'POST /announcements' in actions and not any('preview' in a for a in actions)


async def test_preview_templates_import_and_report(db, monkeypatch):
    c = db[0]
    await prepare(c)
    await c.execute("UPDATE telegram_chats SET platform='max' WHERE telegram_chat_id=202")
    async with make(monkeypatch, c) as client:
        preview = (await client.post('/api/admin/announcements/preview', headers=H, json={'title': 'A', 'body': '<b>x</b>'})).json()
        assert preview['platforms']['telegram']['recipients'] == 1 and preview['platforms']['max']['recipients'] == 1
        assert preview['platforms']['vk']['recipients'] == 0 and preview['total'] == 2
        assert (await client.post('/api/admin/announcement-templates', headers=H, json={'name': 'n', 'body': 'b'})).status_code == 201
        assert (await client.post('/api/admin/announcement-templates', headers=H, json={'name': 'n', 'body': 'b2'})).status_code == 201
        templates = (await client.get('/api/admin/announcement-templates', headers=H)).json()
        assert len(templates) == 1 and templates[0]['body'] == 'b2'
        bad = await client.post('/api/admin/announcements/import', headers=H, json=[ITEM, {**ITEM, 'kind': 'daily'}])
        assert bad.status_code == 422 and 'item_2' in bad.json()['detail']
        assert await c.fetchval('SELECT count(*) FROM announcements') == 0               # откат целиком
        ok = await client.post('/api/admin/announcements/import', headers=H, json=[ITEM, ITEM])
        assert ok.status_code == 201 and len(ok.json()['created']) == 2
        role = await client.put('/api/admin/admins/202/role', headers=H, json={'role': 'editor'})
        assert role.status_code == 404                                                    # 202 не администратор
        report = (await client.get('/api/admin/delivery-report', headers=H)).json()
        assert set(report) == {'per_day', 'errors', 'runs'}
