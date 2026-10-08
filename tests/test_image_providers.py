import base64
import json

import httpx
import pytest

from app.logging import redact
from app.services import artwork, image_router
from app.services import image_providers as api
from tests.test_devotional_artwork import jpeg


def test_auto_configuration_missing_keys_order_and_redaction(monkeypatch):
    monkeypatch.setenv("ILLUSTRATION_PROVIDER", "auto")
    monkeypatch.setenv("IMAGE_PROVIDER_ORDER", "bfl,gemini,openai,ideogram,stability")
    for name in api.KEY_ENV.values():
        monkeypatch.delenv(name, raising=False)
    for name in ("GEMINI_API_KEY", "BFL_API_KEY"):
        monkeypatch.setenv(name, "secret-value-" + name)
        assert "secret-value-" not in redact("secret-value-" + name)
    config = artwork.ArtSettings.from_env()
    assert [p.name for p in image_router.providers(config)] == ["bfl", "gemini"]
    assert "secret-value-" not in repr(config)
    assert "secret-value-" not in repr(image_router.providers(config))
    monkeypatch.setenv("IMAGE_PROVIDER_ORDER", "openai,openai")
    with pytest.raises(ValueError):
        artwork.ArtSettings.from_env()


@pytest.mark.parametrize("name", ["gemini", "ideogram", "stability", "bfl"])
@pytest.mark.asyncio
async def test_official_request_and_result_contracts(name):
    image = jpeg()
    calls = []
    accepted = []

    async def ack(identifier, url):
        accepted.append((identifier, url))

    def handler(request):
        calls.append(request)
        if request.method == "GET" and "/sample" in request.url.path:
            assert not any(key in request.headers for key in ("x-key", "api-key", "authorization"))
            return httpx.Response(200, content=image)
        if name == "gemini":
            body = json.loads(request.content)
            assert body["model"] == "gemini-nano-banana-2.1"
            assert body["generation_config"]["max_output_tokens"] == 4096
            assert body["response_format"]["image_size"] == "1K"
            assert body["input"] == "original complete source"
            assert not body["store"] and "tools" not in body
            assert request.headers["x-goog-api-key"] == "secret"
            return httpx.Response(
                200,
                json={
                    "id": "gemini-id",
                    "steps": [
                        {
                            "type": "model_output",
                            "content": [
                                {"type": "image", "data": base64.b64encode(image).decode()}
                            ],
                        }
                    ],
                },
            )
        if name == "stability":
            assert request.url.path.endswith("/generate/ultra")
            assert request.headers["authorization"] == "Bearer secret"
            assert b'name="aspect_ratio"' in request.content and b"3:2" in request.content
            return httpx.Response(200, content=image)
        if name == "ideogram":
            assert request.url.path == "/v1/ideogram-v4/generate"
            assert request.headers["api-key"] == "secret"
            assert (
                b'name="text_prompt"' in request.content
                and b"original complete source" in request.content
            )
            return httpx.Response(
                200, json={"data": [{"is_image_safe": True, "url": "https://ideogram.ai/sample"}]}
            )
        if request.method == "POST":
            assert json.loads(request.content)["width"] == 1536
            return httpx.Response(
                200,
                json={
                    "id": "bfl-id",
                    "polling_url": "https://api.eu.bfl.ai/v1/get_result?id=bfl-id",
                },
            )
        assert accepted and request.url.host == "api.eu.bfl.ai"
        return httpx.Response(
            200, json={"status": "Ready", "result": {"sample": "https://delivery.eu.bfl.ai/sample"}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        data, _, _ = await api.generate(
            api.Provider(name, "secret"), "original complete source", client=client, accepted=ack
        )
    assert data == image
    assert len([r for r in calls if r.method == "POST"]) == 1


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (401, {}, "auth"),
        (402, {}, "quota"),
        (429, {}, "rate_limited"),
        (429, {"error": {"code": "insufficient_quota"}}, "quota"),
        (400, {"error": {"code": "moderation_blocked"}}, "moderation"),
        (500, {}, "server_uncertain"),
        (422, {}, "rejected"),
    ],
)
def test_actionable_error_classification_never_exposes_body(status, body, expected):
    body["private"] = "PRIVATE BODY"
    with pytest.raises(api.GenerationError) as error:
        api.check_status(httpx.Response(status, json=body, headers={"retry-after": "120"}))
    assert error.value.code == expected and "PRIVATE" not in str(error.value)
    if expected == "rate_limited":
        assert error.value.retry_after == 120


@pytest.mark.parametrize("name", ["gemini", "ideogram", "stability", "bfl"])
@pytest.mark.asyncio
async def test_slow_submission_is_uncertain_and_not_locally_repeated(name):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("private details")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(api.GenerationError) as error:
            await api.generate(api.Provider(name, "secret"), "source", client=client)
    assert count == 1 and error.value.uncertain


@pytest.mark.asyncio
async def test_bfl_pending_resumes_by_id_with_no_new_paid_post():
    methods = []

    def handler(request):
        methods.append(request.method)
        return httpx.Response(200, json={"status": "Pending"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(api.GenerationError, match="remote_pending"):
            await api.generate(
                api.Provider("bfl", "secret"),
                "source",
                client=client,
                resume={
                    "id": "saved-id",
                    "polling_url": "https://api.bfl.ai/v1/get_result?id=saved-id",
                },
            )
    assert methods == ["GET"]


@pytest.mark.parametrize(
    "url",
    [
        "http://ideogram.ai/image",
        "https://evil.test/image",
        "https://ideogram.ai.evil.test/image",
        "https://key@ideogram.ai/image",
        "https://127.0.0.1/image",
    ],
)
def test_image_urls_cannot_exfiltrate_keys_or_access_private_services(url):
    with pytest.raises(api.GenerationError):
        api.official_url(url, "ideogram")


@pytest.mark.asyncio
async def test_unsafe_result_or_oversized_download_is_not_accepted():
    def handler(request):
        return httpx.Response(200, content=b"x" * (5 * 1024 * 1024 + 1))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(api.GenerationError):
            await api.download(client, "https://ideogram.ai/sample", "ideogram")
