"""Reject malformed Stars updates before accessing persistence."""
from __future__ import annotations

import re
from unittest.mock import AsyncMock

import pytest

from app.services import donations

PAYLOAD = 'donation:v1:' + 'a' * 32


@pytest.mark.asyncio
@pytest.mark.parametrize('amount', [0, -1, 2501, True, False, 1.5, '25', None])
async def test_invalid_donation_amount_never_reaches_database(amount):
    with pytest.raises(ValueError):
        await donations.create_order(None, 101, amount)


@pytest.mark.asyncio
@pytest.mark.parametrize('user', [0, -1, True, 2**63, '101', None])
async def test_invalid_user_never_creates_donation(user):
    with pytest.raises(ValueError):
        await donations.create_order(None, user, 25)


@pytest.mark.asyncio
async def test_orders_have_independent_bounded_unpredictable_payloads():
    connection = AsyncMock()
    payloads = set()
    for _ in range(30):
        await donations.create_order(connection, 101, 25)
        payload = connection.fetchrow.await_args.args[1]
        assert re.fullmatch(r'donation:v1:[A-Za-z0-9_-]{32}', payload)
        assert len(payload.encode()) <= 128
        payloads.add(payload)
    assert len(payloads) == 30
    assert donations.MIN_AMOUNT == 1 and donations.MAX_AMOUNT == 2500
    assert donations.PRESET_AMOUNTS == (25, 50, 100, 250, 500)


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [
    ('payload', ''), ('payload', 'arbitrary'), ('payload', PAYLOAD + ':101'),
    ('payload', None), ('user_id', True), ('user_id', -1),
    ('currency', 'USD'), ('currency', 'xtr'), ('total_amount', 0),
    ('total_amount', True), ('query_id', ''), ('query_id', 'a' * 2049),
    ('query_id', 'a\x00b'),
])
async def test_invalid_checkout_returns_false(field, value):
    data = {'payload':PAYLOAD, 'user_id':101, 'currency':'XTR', 'total_amount':25, 'query_id':'query'}
    data[field] = value
    assert not await donations.validate_checkout(None, **data)


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [
    ('payload', 'bad'), ('user_id', -1), ('currency', 'USD'),
    ('total_amount', 2501), ('charge_id', ''), ('charge_id', 'a\x00b'),
])
async def test_invalid_payment_raises_without_database_access(field, value):
    data = {'payload':PAYLOAD, 'user_id':101, 'currency':'XTR', 'total_amount':25, 'charge_id':'charge'}
    data[field] = value
    with pytest.raises(ValueError):
        await donations.record_payment(None, **data)


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['', '  \n', None, 'x' * 4001, 'text\x00text'])
async def test_support_requires_bounded_real_text(text):
    with pytest.raises(ValueError):
        await donations.create_support_request(None, 101, text)


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', [0, 51, True, -1, '10'])
async def test_history_limit_is_bounded(limit):
    with pytest.raises(ValueError):
        await donations.list_user_donations(None, 101, limit)


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('payload','bad'), ('currency','USD'), ('total_amount',False), ('charge_id','')])
async def test_refund_validation_is_not_weaker_than_payment(field, value):
    data = {'payload':PAYLOAD, 'currency':'XTR', 'total_amount':25, 'charge_id':'charge'}
    data[field] = value
    with pytest.raises(ValueError):
        await donations.record_refund(None, **data)
