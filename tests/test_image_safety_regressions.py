"""Image moderation, gallery work and shared custom-provider budget regressions."""
import asyncio
import os
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import httpx
import pytest

from app.bot.transport import TelegramSender
from app.maxbot.transport import MaxSender
from app.services import artwork, free_image_providers as free, illustrations, image_router
from app.services.errors import SendError
from app.services.image_providers import GenerationError, Provider
from app.vkbot.transport import VkSender
from tests.test_devotional_artwork import jpeg
from tests.test_image_versions_postgres import setup
from tests.test_postgres_integration import db as db  # noqa: F401

requires_db = pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='needs PostgreSQL')


def test_all_providers_use_reliability_score_and_priority_breaks_ties(monkeypatch):
    monkeypatch.setitem(image_router.RANKING, 'mode', 'auto')
    monkeypatch.setitem(image_router.RANKING, 'scores', {'openai': 0.0, 'gemini': 1.0, 'x-custom': 0.5})
    settings = SimpleNamespace(provider_order=('openai', 'gemini'))
    good, bad = Provider('gemini', 'g'), Provider('openai', 'o')
    custom = Provider('x-custom', 'x', custom={'priority': 10})
    assert image_router.rank(settings, good) < image_router.rank(settings, custom) < image_router.rank(settings, bad)
    monkeypatch.setitem(image_router.RANKING, 'scores', {})
    assert image_router.rank(settings, bad) < image_router.rank(settings, good)


def test_unknown_prices_and_paid_free_tiers_cannot_reserve_zero():
    spec = {'adapter': 'openai_compatible', 'base_url': 'https://api.example.com', 'model': 'expensive'}
    assert Provider('x-paid', 'key', custom=spec).reservation_usd is None
    assert free.reservation_for({**spec, 'billing_mode': 'free_only'}) is None
    assert free.reservation_for({**spec, 'billing_mode': 'metered', 'reservation_usd': '0'}) is None
    assert free.reservation_for({**spec, 'billing_mode': 'metered', 'reservation_usd': '0.75'}) == Decimal('0.75')
    free_spec = {'adapter': 'pollinations', 'billing_mode': 'free_only', 'model': 'flux'}
    assert free.reservation_for(free_spec) == 0
    for patch in ({'api_key': 'paid-key'}, {'base_url': 'https://paid.example.com'}, {'model': 'paid'}, {'extra': {'anything': True}}):
        assert free.reservation_for({**free_spec, **patch}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('adapter', free.ADAPTERS)
async def test_every_custom_adapter_rejects_private_dns_before_request(monkeypatch, adapter):
    monkeypatch.setattr(free, 'public_host', AsyncMock(return_value=False))
    spec = {'adapter': adapter, 'base_url': 'https://public.example.com', 'api_key': 'fake',
            'account_id': 'a' * 32, 'model': 'm', 'width': 1024, 'height': 768, 'extra': {}}
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: calls.append(request))) as client:
        with pytest.raises(GenerationError, match='response_invalid'):
            await free.generate(spec, 'scene', client=client)
    assert calls == []


@requires_db
@pytest.mark.asyncio
async def test_gallery_variant_reaches_real_worker_selector(db):
    c, _, edition, _, row = await setup(db)
    first = await illustrations.store(c, row, edition, jpeg(), 'original')
    await c.execute("INSERT INTO image_generation_jobs(image_id,prompt,prompt_variant,model,state) VALUES($1,'scene','symbolic','m','ready')", first)
    variant = await illustrations.request_variant(c, first)
    job = await c.fetchval('SELECT id FROM image_generation_jobs WHERE image_id=$1', variant)
    assert job in [item['id'] for item in await artwork.due_jobs(c)]
    assert await illustrations.request_variant(c, first) == variant
    assert await c.fetchval('SELECT count(*) FROM image_generation_jobs WHERE image_id=$1', variant) == 1


@requires_db
@pytest.mark.asyncio
async def test_quarantine_blocks_late_edits_and_cached_media_on_every_platform(db):
    c, settings, edition, chat, row = await setup(db)
    await artwork.request_image(c, row, edition, chat, 'reading', 'quarantine-regression')
    request = await c.fetchrow("SELECT * FROM illustration_requests WHERE request_key='quarantine-regression'")
    await illustrations.bind_message(c, 101, request['id'], 1234)
    image = await illustrations.store(c, row, edition, jpeg(), 'original', image_id=request['image_id'])
    await c.execute("UPDATE verse_illustrations SET telegram_file_id='cached',telegram_bot_id=99 WHERE id=$1", image)
    assert await illustrations.set_quarantine(c, image, True)
    assert await artwork.dispatch_requests(c) == 0
    assert await c.fetchval("SELECT count(*) FROM delivery_log WHERE mode='illustration_edit'") == 0
    bot = SimpleNamespace(id=99, send_photo=AsyncMock(),
                          send_rich_message=AsyncMock(return_value=SimpleNamespace(message_id=77)),
                          edit_message_text=AsyncMock(return_value=SimpleNamespace(message_id=1234)))
    telegram = TelegramSender(bot, c, settings)
    assert await telegram.send(101, {'kind': 'rich', 'text': 'reading', 'image_id': image}) == 77
    assert await telegram.send(101, {'kind': 'rich_edit', 'text': 'reading', 'image_id': image, 'message_id': 1234}) == 1234
    for call in (bot.send_rich_message.await_args, bot.edit_message_text.await_args):
        rich = call.kwargs['rich_message']
        assert rich.html == 'reading' and not rich.media
    assert await telegram.send(101, {'kind': 'photo', 'caption': 'legacy reading', 'image_id': image}) == 77
    assert bot.send_rich_message.await_args.kwargs['rich_message'].html == 'legacy reading'
    with pytest.raises(SendError):
        await telegram.send(101, {'kind': 'photo', 'caption': '', 'image_id': image})
    bot.send_photo.assert_not_awaited()
    client = SimpleNamespace(upload=AsyncMock())
    for sender in (MaxSender(client, 99, c), VkSender(client, 99, c)):
        with pytest.raises(SendError):
            if isinstance(sender, VkSender):
                await sender.media('image', image, 101)
            else:
                await sender.media('image', image)
    client.upload.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['max', 'vk'])
@pytest.mark.parametrize('editing', [False, True])
async def test_quarantined_rich_card_sends_text_and_edits_remove_media(platform, editing, monkeypatch):
    from app.maxbot import transport as max_transport
    from app.vkbot import transport as vk_transport
    module = max_transport if platform == 'max' else vk_transport
    c = SimpleNamespace(fetchrow=AsyncMock(side_effect=[{'ui_language': 'ru', 'chat_type': 'private'}, {'quarantined_at': 'known'}]),
                        execute=AsyncMock())
    client = SimpleNamespace(send=AsyncMock(return_value='mid' if platform == 'max' else 77), edit=AsyncMock(), upload=AsyncMock())
    sender = MaxSender(client, 99, c) if platform == 'max' else VkSender(client, 99, c)
    sender.media = AsyncMock()
    monkeypatch.setattr(module, 'external', AsyncMock(return_value=101))
    monkeypatch.setattr(module, 'wait_send_slot', AsyncMock())
    if platform == 'max':
        monkeypatch.setattr(module, 'message_external', AsyncMock(return_value='mid.original'))
        monkeypatch.setattr(module, 'remember_message', AsyncMock(return_value=77))
    else:
        sender.remember_pages = AsyncMock()
    chunk = {'kind': 'rich_edit' if editing else 'rich', 'text': 'reading', 'image_id': 17, 'message_id': 1234}
    assert await sender.send(101, chunk) == (1234 if editing else 77)
    sender.media.assert_not_awaited()
    client.upload.assert_not_awaited()
    if platform == 'max':
        call = client.edit.await_args.args[1] if editing else client.send.await_args.kwargs['body']
        assert call['text'] == 'reading' and call['attachments'] == []
    else:
        call = client.edit.await_args.kwargs if editing else client.send.await_args.kwargs
        assert call['message'] == 'reading' and call['attachment'] == ('' if editing else None)


@pytest.mark.asyncio
async def test_vk_api_transmits_empty_attachment_for_explicit_removal():
    from urllib.parse import parse_qs
    from app.vkbot.client import VkClient
    from app.vkbot.config import VkSettings
    seen = []
    def handler(request):
        seen.append(parse_qs(request.content.decode(), keep_blank_values=True))
        return httpx.Response(200, json={'response': 1})
    client = VkClient(VkSettings(token='fake-' * 6, group_id=1), transport=httpx.MockTransport(handler))
    try:
        await client.edit(101, 1234, message='reading', attachment='')
        assert seen[0]['attachment'] == ['']
    finally:
        await client.close()


@requires_db
@pytest.mark.asyncio
async def test_custom_reserves_and_panel_probes_share_budget_concurrently(db, monkeypatch):
    from tests.test_image_router_postgres import job
    monkeypatch.setenv('ADMIN_API_KEY', 'a' * 32)
    c, settings, identifier = await job(db)
    await c.execute("""INSERT INTO image_custom_providers(slug,adapter,base_url,model,billing_mode,reservation_usd)
        VALUES('paid','openai_compatible','https://api.example.com','model','unknown',0)""")
    await image_router.load_custom(c)
    limited = artwork.ArtSettings(provider='manual', provider_order=(), monthly_usd=Decimal('0.75'))
    assert (await image_router.reserve(c, identifier, limited))[0] is None
    await c.execute("UPDATE image_custom_providers SET billing_mode='metered',reservation_usd=0.75")
    await image_router.load_custom(c)
    provider = image_router.providers(limited)[0]
    other = await asyncpg.connect(settings.database_url)
    try:
        results = await asyncio.gather(image_router.reserve(c, identifier, limited), image_router.reserve_test(other, provider, limited))
        assert sum(result[0] is not None for result in results) == 1
        assert sum(record['monthly'] for record in await image_router.budget_usage(c)) == Decimal('0.75')
        await c.execute("UPDATE image_generation_jobs SET state='retry'")
        await c.execute("UPDATE image_generation_attempts SET state='uncertain'")
        await c.execute("UPDATE image_provider_test_attempts SET state='uncertain'")
        assert (await image_router.reserve_test(c, provider, limited))[0] is None
        assert (await image_router.reserve(c, identifier, limited))[0] is None
        assert (await image_router.reserve_test(c, provider, replace(limited, monthly_usd=Decimal('1.50'))))[0] is not None
    finally:
        await other.close()


@requires_db
@pytest.mark.asyncio
async def test_panel_probes_cannot_bypass_cost_or_erase_uncertain_reservations(db, monkeypatch):
    from tests.test_admin_api_postgres import H, make
    monkeypatch.setenv('ADMIN_API_KEY', 'k' * 32)
    monkeypatch.setenv('IMAGE_MONTHLY_BUDGET_USD', '0.50')
    c = db[0]
    generator = AsyncMock(side_effect=GenerationError('transport_error', uncertain=True))
    monkeypatch.setattr(free, 'generate', generator)
    async with make(monkeypatch, c) as client:
        payload = {'slug': 'paid-probe', 'adapter': 'openai_compatible', 'base_url': 'https://api.example.com/v1', 'model': 'paid', 'api_key': 'fake'}
        created = await client.post('/api/admin/image-providers', headers=H, json=payload)
        pid = created.json()['id']
        path = f'/api/admin/image-providers/{pid}'
        assert (await client.post(path + '/test', headers=H)).json()['error'] == 'pricing_unknown'
        generator.assert_not_awaited()
        assert (await client.patch(path, headers=H, json={'billing_mode': 'free_only'})).status_code == 422
        assert (await client.patch(path, headers=H, json={'billing_mode': 'metered', 'reservation_usd': '0.50'})).status_code == 200
        assert (await client.post(path + '/test', headers=H)).json()['error'] == 'transport_error'
        assert await c.fetchval('SELECT state FROM image_provider_test_attempts') == 'uncertain'
        assert (await client.post(path + '/test', headers=H)).json()['error'] == 'monthly_budget'
        assert generator.await_count == 1
        anonymous = (await client.post('/api/admin/image-providers', headers=H, json={
            'slug': 'anonymous', 'adapter': 'pollinations', 'model': 'flux', 'billing_mode': 'free_only'})).json()['id']
        for patch in ({'api_key': 'billable'}, {'model': 'paid'}, {'base_url': 'https://paid.example.com'}, {'extra': {'no_response_format': True}}):
            assert (await client.patch(f'/api/admin/image-providers/{anonymous}', headers=H, json=patch)).status_code == 422
