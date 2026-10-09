"""Untrusted webhook validation and stable deduplication keys."""
from __future__ import annotations

import json

import pytest

from app.maxbot.events import authorized, parse_event


def fixture_event():
    return {'update_type': 'message_created', 'timestamp': 123,
            'message': {'sender': {'user_id': 101, 'first_name': 'Fixture'},
                        'recipient': {'chat_id': 303, 'chat_type': 'dialog'},
                        'body': {'mid': 'mid.fixture', 'text': '/random'}}}


def test_deduplication_uses_message_identity_not_resend_timestamp():
    event = fixture_event()
    key, decoded = parse_event(json.dumps(event).encode())
    assert decoded == event
    event['timestamp'] = 456
    assert parse_event(json.dumps(event).encode())[0] == key
    event['message']['body']['mid'] = 'mid.next'
    assert parse_event(json.dumps(event).encode())[0] != key


def test_callback_key_is_callback_id():
    event = {'update_type': 'message_callback', 'timestamp': 123,
             'callback': {'callback_id': 'callback.fixture', 'user': {'user_id': 101},
                          'payload': 'v1:pause:101:'}, 'message': None}
    key, _ = parse_event(json.dumps(event).encode())
    event['timestamp'] += 1
    assert parse_event(json.dumps(event).encode())[0] == key


@pytest.mark.parametrize('raw', [b'[]', b'null', b'{', b'{}', b'x' * 65537,
                               b'{"update_type":"message_created","timestamp":true}'],
                         ids=['array', 'null', 'broken', 'missing', 'oversize', 'boolean-clock'])
def test_invalid_envelopes(raw):
    with pytest.raises(ValueError):
        parse_event(raw)


@pytest.mark.parametrize(('path', 'value'), [
    (('message', 'sender', 'user_id'), True), (('message', 'recipient', 'chat_id'), 0),
    (('message', 'recipient', 'chat_type'), 'private'), (('message', 'body', 'mid'), 12),
    (('message', 'body', 'text'), 'x' * 4001),
], ids=['boolean-actor', 'zero-chat', 'wrong-type', 'numeric-mid', 'oversize-text'])
def test_malformed_message_fields(path, value):
    event = fixture_event()
    target = event
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        parse_event(json.dumps(event).encode())


def test_webhook_secret_cannot_be_empty_or_default():
    assert not authorized('', '')
    assert not authorized('default', 'default')
    assert not authorized('fixture-very-private-secret', 'wrong')
    assert authorized('fixture-very-private-secret', 'fixture-very-private-secret')
