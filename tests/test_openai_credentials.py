import json

import httpx
import pytest

from app.logging import redact
from app.services import artwork, image_router
from app.services import image_providers as api


@pytest.mark.parametrize('value', ['second-fixture,primary-fixture, third-fixture',
                                  json.dumps(['second-fixture', 'primary-fixture', 'third-fixture'])])
def test_keys_are_deduplicated_and_redacted(monkeypatch, value):
    monkeypatch.setenv('OPENAI_API_KEY', 'primary-fixture')
    monkeypatch.setenv('OPENAI_API_KEYS', value)
    settings = artwork.ArtSettings.from_env()
    keys = [p.key for p in image_router.providers(settings) if p.name == 'openai']
    assert keys == ['primary-fixture', 'second-fixture', 'third-fixture']
    for key in keys:
        assert key not in repr(settings)
        assert key not in redact('an error with '+key)


@pytest.mark.parametrize('value', ['["secret", 2]', '{', '[' , 'two keys', ','.join('fixture-'+str(i) for i in range(33))])
def test_invalid_pool_does_not_expose_credentials(monkeypatch, value):
    monkeypatch.setenv('OPENAI_API_KEYS', value)
    with pytest.raises(ValueError) as error:
        artwork.ArtSettings.from_env()
    assert value not in str(error.value)


@pytest.mark.parametrize('code', ['credit_balance_exhausted', 'project_spend_limit_exceeded',
                                'organization_spend_limit_exceeded', 'organization_usage_limit_exceeded'])
def test_billing_errors_are_not_treated_as_temporary_rate_limits(code):
    with pytest.raises(api.GenerationError, match='quota'):
        api.check_status(httpx.Response(429, json={'error': {'code':code}}))
