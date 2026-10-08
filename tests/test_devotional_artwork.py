"""Meaningful provider contracts, cost limits and authenticated backup integrity."""

import base64
from datetime import date
from io import BytesIO

import httpx
import pytest
from cryptography.exceptions import InvalidTag
from PIL import Image

from app.bot.commands import mode_name, parse_command
from app.services import artwork, cloud_backup, devotionals


def jpeg():
    stream = BytesIO()
    Image.new("RGB", (256, 256), "blue").save(stream, format="JPEG")
    return stream.getvalue()


def test_context_changes_prompt_without_changing_verse():
    row = {"book_code": "PSA", "chapter": 1, "verse": 1, "text": "fixture text"}
    morning = artwork.prompt(
        row, {"title": "fixture"}, slot="morning_verse", day=date(2026, 10, 8), variant="historical"
    )
    evening = artwork.prompt(
        row, {"title": "fixture"}, slot="evening_verse", day=date(2026, 10, 8), variant="watercolor"
    )
    assert "dawn" in morning and "twilight" in evening and morning != evening
    assert "<verse>fixture text</verse>" in morning
    assert "No lettering" in morning
    assert devotionals.theme_for(date(2026, 10, 8), "morning_verse") != devotionals.theme_for(
        date(2026, 10, 15), "morning_verse"
    )


@pytest.mark.parametrize("alias,mode", [("morning", "morning_verse"), ("evening", "evening_verse")])
def test_devotional_command_modes_and_destination(alias, mode):
    assert mode_name(alias) == mode
    assert parse_command("/devotions off @example_channel").target == "@example_channel"


@pytest.mark.asyncio
async def test_openai_generation_contract_and_valid_bytes():
    image = jpeg()
    calls = []

    def response(request):
        import json

        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "data": [{"b64_json": base64.b64encode(image).decode()}],
                "usage": {"output_tokens": 100},
            },
            headers={"x-request-id": "fixture"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        data, usage, request = await artwork.generate(
            artwork.ArtSettings(key="fixture-secret"), "fixture prompt", client=client
        )
    assert data == image and request == "fixture" and usage["output_tokens"] == 100
    assert calls[0] == {
        "model": "gpt-image-2",
        "prompt": "fixture prompt",
        "n": 1,
        "quality": "medium",
        "size": "1536x1024",
        "output_format": "jpeg",
        "output_compression": 90,
    }
    assert "fixture-secret" not in repr(artwork.ArtSettings(key="fixture-secret"))


@pytest.mark.parametrize(
    "status,code,uncertain",
    [(401, "auth", False), (429, "rate_limited", False), (500, "server_uncertain", True)],
)
@pytest.mark.asyncio
async def test_provider_errors_are_classified_without_logging_body(status, code, uncertain):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(status, json={"error": "SECRET"}))
    ) as client:
        with pytest.raises(artwork.GenerationError) as caught:
            await artwork.generate(artwork.ArtSettings(key="secret"), "fixture", client=client)
    assert caught.value.code == code and caught.value.uncertain == uncertain
    assert "SECRET" not in str(caught.value)


@pytest.mark.asyncio
async def test_timeout_is_uncertain_not_paid_auto_retry():
    def timeout(request):
        raise httpx.ReadTimeout("fixture")

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(artwork.GenerationError) as error:
            await artwork.generate(artwork.ArtSettings(key="fixture"), "fixture", client=client)
    assert error.value.uncertain


def test_database_encryption_roundtrip_tamper_and_existing_file(tmp_path):
    original = tmp_path / "original.dump"
    original.write_bytes(b"PGDMP" + b"private-data" * 100000)
    encrypted = tmp_path / "backup.aesgcm"
    key = "ab" * 32
    cloud_backup.encrypt_file(original, encrypted, key)
    assert b"private-data" not in encrypted.read_bytes()
    restored = tmp_path / "restored.dump"
    cloud_backup.decrypt_file(encrypted, restored, key)
    assert restored.read_bytes() == original.read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        cloud_backup.decrypt_file(encrypted, restored, key)
    data = bytearray(encrypted.read_bytes())
    data[30] ^= 1
    encrypted.write_bytes(data)
    output = tmp_path / "tampered.dump"
    with pytest.raises(InvalidTag):
        cloud_backup.decrypt_file(encrypted, output, key)
    assert not output.exists() and not output.with_name(output.name + ".part").exists()


def test_cloud_copy_requires_actual_read_back_integrity():
    class S3:
        def put_object(self, **kwargs):
            self.kw = kwargs

        def head_object(self, **kwargs):
            return {"ContentLength": len(self.kw["Body"]), "Metadata": self.kw["Metadata"]}

        def get_object(self, **kwargs):
            return {"Body": BytesIO(b"WRONG")}

    with pytest.raises(ValueError, match="checksum mismatch"):
        cloud_backup.upload_checked(
            S3(), "fixture-bucket", cloud_backup.PREFIX + "images/fixture", b"correct"
        )


def test_local_retention_leaves_foreign_files_and_newest(tmp_path):
    import os

    from app.services.cloud_backup import prune_local

    foreign = tmp_path / "another-project.dump"
    foreign.write_bytes(b"foreign")
    for index in range(3):
        own = tmp_path / f"biblebot-2026100{index + 1}T000000Z-123.dump"
        own.write_bytes(b"snapshot")
        os.utime(own, (100 + index, 100 + index))
    assert prune_local(tmp_path, keep=1) == 2
    assert foreign.read_bytes() == b"foreign"
    assert (tmp_path / "biblebot-20261003T000000Z-123.dump").exists()


def test_thematic_pool_prefers_devotional_books_without_borrowing_coordinates():
    from app.services.devotionals import thematic_candidates

    narrative = {"book_code": "JDG", "chapter": 19, "verse": 6}
    wisdom = {"book_code": "PRO", "chapter": 3, "verse": 5}
    assert thematic_candidates([narrative, wisdom], set()) == [wisdom]
    assert thematic_candidates([narrative, wisdom], {("PRO", 3, 5)}) == [narrative]


def test_new_provider_secrets_are_redacted(monkeypatch):
    from app.logging import redact

    for key in ["OPENAI_API_KEY", "S3_ACCESS_KEY", "S3_SECRET_KEY", "BACKUP_ENCRYPTION_KEY"]:
        secret = "private-value-" + key
        monkeypatch.setenv(key, secret)
        assert secret not in redact("provider error " + secret)
