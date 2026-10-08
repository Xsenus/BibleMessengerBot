"""Fixed official image APIs. No keys or untrusted response bodies in exceptions."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from decimal import Decimal
from io import BytesIO
from urllib.parse import urlsplit

import httpx
from PIL import Image

NAMES = ("openai", "gemini", "bfl", "ideogram", "stability")
MODELS = dict(
    zip(
        NAMES,
        ("gpt-image-2", "gemini-nano-banana-2.1", "flux-2-pro", "ideogram-4", "stable-image-ultra"),
        strict=True,
    )
)
# Conservative internal reserve per submission, including failed/uncertain ones.
# Gemini's full bounded chapter input uses a larger reserve than short prompts.
RESERVES = dict(zip(NAMES, map(Decimal, ("0.10", "0.40", "0.10", "0.10", "0.10")), strict=True))
KEY_ENV = dict(
    zip(
        NAMES,
        (
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
            "BFL_API_KEY",
            "IDEOGRAM_API_KEY",
            "STABILITY_API_KEY",
        ),
        strict=True,
    )
)


class GenerationError(Exception):
    def __init__(self, code, uncertain=False, retry=False, retry_after=0):
        self.code, self.uncertain, self.retry = code, uncertain, retry
        self.retry_after = retry_after
        super().__init__(code)


@dataclass(frozen=True)
class Provider:
    name: str
    key: str = field(repr=False)

    @property
    def model(self):
        return MODELS[self.name]

    @property
    def reservation_usd(self):
        return RESERVES[self.name]


def check_status(response):
    if 200 <= response.status_code < 300:
        return
    status = response.status_code
    # Use stable error discriminators, never include the body in diagnostics.
    try:
        error = response.json().get("error", {})
        code = error.get("code", "") if isinstance(error, dict) else ""
        name = response.json().get("name", "")
    except (ValueError, AttributeError):
        code, name = "", ""
    if status == 402 or code in {
        "insufficient_quota",
        "billing_hard_limit_reached",
        "insufficient_credits",
    }:
        raise GenerationError("quota")
    if (
        code in {"moderation_blocked", "content_policy_violation"}
        or name == "content_moderation"
        or status == 451
    ):
        raise GenerationError("moderation")
    if status in {401, 403}:
        raise GenerationError("auth")
    if status == 429:
        try:
            delay = max(0, min(86400, int(response.headers.get("retry-after", "60"))))
        except ValueError:
            delay = 60
        raise GenerationError("rate_limited", retry=True, retry_after=delay)
    if status >= 500:
        raise GenerationError("server_uncertain", uncertain=True)
    raise GenerationError("rejected")


def official_url(url, provider, *, polling=False):
    """Never forward API credentials to a generated image URL or arbitrary host."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    valid = (
        (host == "bfl.ai" or host.endswith(".bfl.ai"))
        if provider == "bfl"
        else (host == "ideogram.ai" or host.endswith(".ideogram.ai"))
    )
    if polling:
        valid = valid and (host == "api.bfl.ai" or host.startswith("api."))
    if (
        not valid
        or parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
    ):
        raise GenerationError("response_invalid", uncertain=True)
    return url


async def download(client, url, provider):
    official_url(url, provider)
    # Signed delivery URLs receive no API-key header. Redirects are rejected.
    async with client.stream("GET", url, follow_redirects=False) as response:
        if response.status_code != 200:
            raise GenerationError("download_uncertain", uncertain=True)
        chunks, size = [], 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > 20 * 1024 * 1024:
                raise GenerationError("response_invalid", uncertain=True)
            chunks.append(chunk)
        return normalize_download(b"".join(chunks))


def normalize_download(data):
    """Bound provider PNGs before converting oversized files for storage/Telegram."""
    try:
        with Image.open(BytesIO(data)) as image:
            if (
                image.format not in {'PNG', 'JPEG', 'WEBP'}
                or image.width * image.height > 9_000_000
                or image.width < 256
                or image.height < 256
            ):
                raise ValueError('Unexpected generated dimensions or format')
            image.verify()
        if len(data) <= 5 * 1024 * 1024:
            return data
        with Image.open(BytesIO(data)) as image:
            # Preserve resolution; flatten alpha against white for the JPEG card.
            rgba = image.convert('RGBA')
            rgb = Image.new('RGB', image.size, 'white')
            rgb.paste(rgba, mask=rgba.getchannel('A'))
            for quality in (95, 92, 85, 80):
                output = BytesIO()
                rgb.save(output, format='JPEG', quality=quality, optimize=True)
                if output.tell() <= 5 * 1024 * 1024:
                    return output.getvalue()
            raise ValueError('Generated image exceeds storage limit')
    except (ValueError, OSError) as error:
        raise GenerationError('response_invalid', uncertain=True) from error


async def generate(provider, prompt, *, client=None, accepted=None, resume=None):
    """One paid submission, or one free poll of an already persisted BFL task."""
    if not provider.key:
        raise GenerationError("missing_key")
    if len(prompt.encode("utf-8")) > 160000:
        raise GenerationError("prompt_too_large")
    owned = client is None
    client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(240, connect=15), follow_redirects=False
    )
    try:
        try:
            if provider.name == "gemini":
                response = await client.post(
                    "https://generativelanguage.googleapis.com/v1beta/interactions",
                    headers={"x-goog-api-key": provider.key},
                    json={
                        "model": provider.model,
                        "input": prompt,
                        "store": False,
                        "generation_config": {"max_output_tokens": 4096},
                        "response_format": {
                            "type": "image",
                            "mime_type": "image/jpeg",
                            "aspect_ratio": "3:2",
                            "image_size": "1K",
                        },
                    },
                )
                check_status(response)
                payload = response.json()
                if payload.get("finish_reason") in {"SAFETY", "IMAGE_SAFETY", "PROHIBITED_CONTENT"}:
                    raise GenerationError("moderation")
                images = [
                    block
                    for step in payload.get("steps", [])
                    if step.get("type") == "model_output"
                    for block in step.get("content", [])
                    if block.get("type") == "image"
                ]
                if not images:
                    raise GenerationError("no_image", uncertain=True)
                data = base64.b64decode(images[0]["data"], validate=True)
                usage, request_id = payload.get("usage", {}), payload.get("id")
            elif provider.name == "ideogram":
                response = await client.post(
                    "https://api.ideogram.ai/v1/ideogram-v4/generate",
                    headers={"Api-Key": provider.key},
                    files={
                        "text_prompt": (None, prompt),
                        "rendering_speed": (None, "DEFAULT"),
                    },
                )
                # In this API 422 explicitly means the prompt failed safety checks.
                if response.status_code == 422:
                    raise GenerationError("moderation")
                check_status(response)
                payload = response.json()
                image = payload["data"][0]
                if not image.get("is_image_safe", True):
                    raise GenerationError("moderation")
                data = await download(client, image["url"], "ideogram")
                usage, request_id = {}, payload.get("request_id")
            elif provider.name == "stability":
                response = await client.post(
                    "https://api.stability.ai/v2beta/stable-image/generate/ultra",
                    headers={"Authorization": "Bearer " + provider.key, "Accept": "image/*"},
                    files={
                        "prompt": (None, prompt),
                        "aspect_ratio": (None, "3:2"),
                        "output_format": (None, "jpeg"),
                    },
                )
                check_status(response)
                data, usage, request_id = response.content, {}, response.headers.get("x-request-id")
            elif provider.name == "bfl":
                if resume:
                    task = resume
                else:
                    response = await client.post(
                        "https://api.bfl.ai/v1/flux-2-pro",
                        headers={"x-key": provider.key},
                        json={
                            "prompt": prompt,
                            "width": 1536,
                            "height": 1024,
                            "output_format": "jpeg",
                        },
                    )
                    check_status(response)
                    task = response.json()
                    official_url(task["polling_url"], "bfl", polling=True)
                    if not isinstance(task["id"], str) or not task["id"]:
                        raise GenerationError("response_invalid", uncertain=True)
                    if accepted is None:
                        raise GenerationError("persistence_uncertain", uncertain=True)
                    await accepted(task["id"], task["polling_url"])
                response = await client.get(
                    official_url(task["polling_url"], "bfl", polling=True),
                    headers={"x-key": provider.key},
                )
                # A poll failure never submits another paid task.
                if response.status_code != 200:
                    raise GenerationError("remote_pending", retry=True)
                result = response.json()
                if result["status"] in {"Pending", "Processing"}:
                    raise GenerationError("remote_pending", retry=True)
                if result["status"] in {"Request Moderated", "Content Moderated"}:
                    raise GenerationError("moderation")
                if result["status"] in {"Error", "Failed", "Task not found"}:
                    raise GenerationError("remote_failed")
                if result["status"] != "Ready":
                    raise GenerationError("remote_pending", retry=True)
                data = await download(client, result["result"]["sample"], "bfl")
                usage, request_id = {}, task["id"]
            else:
                raise ValueError("Unsupported provider")
        except httpx.TransportError as error:
            raise GenerationError("transport_uncertain", uncertain=True) from error
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise GenerationError("response_invalid", uncertain=True) from error
        from app.services.artwork import validate_image

        try:
            validate_image(data)
        except (ValueError, OSError) as error:
            raise GenerationError("response_invalid", uncertain=True) from error
        return data, usage, request_id
    finally:
        if owned:
            await client.aclose()
