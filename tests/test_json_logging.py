import io
import json
import logging

from app.logging import configure_logging


def test_json_logs_are_valid_and_redacted(monkeypatch):
    monkeypatch.setenv('LOG_FORMAT', 'json')
    monkeypatch.setenv('ADMIN_API_KEY', 'super-secret-admin-key-123456')
    stream = io.StringIO()
    configure_logging(stream=stream)
    try:
        raise ValueError('leak super-secret-admin-key-123456')
    except ValueError:
        logging.getLogger('t').error('key=%s', 'super-secret-admin-key-123456', exc_info=True)
    row = json.loads(stream.getvalue().strip())
    assert row['level'] == 'ERROR' and row['logger'] == 't'
    assert 'super-secret' not in stream.getvalue() and '[REDACTED]' in row['message']
    assert '[REDACTED]' in row['exception']
