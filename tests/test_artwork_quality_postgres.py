import os
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from app.services import artwork_repairs, illustrations, image_quality
from app.services import image_router as router
from app.worker.delivery import decoded, insert_payload, process_delivery
from tests.test_devotional_artwork import jpeg
from tests.test_image_router_postgres import config, job
from tests.test_image_versions_postgres import setup
from tests.test_postgres_integration import create_destination
from tests.test_postgres_integration import db as db

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="Real PostgreSQL required"),
]


async def test_rejected_paid_output_is_retained_but_never_published(db, monkeypatch):
    c, _, identifier = await job(db)
    monkeypatch.setattr(
        image_quality,
        "assess",
        AsyncMock(return_value=dict(verdict="rejected", reason="visible_text", metrics={})),
    )

    async def generator(*args, **kwargs):
        return jpeg(), {}, "charged-result"

    assert await router.process_job(c, identifier, config(), generator=generator) == "quality_text"
    assert await c.fetchval("SELECT status FROM verse_illustrations") == "pending"
    assert await c.fetchval("SELECT reserved_usd FROM image_generation_attempts") > 0
    assert bytes(await c.fetchval("SELECT rejected_data FROM image_quality_reviews")) == jpeg()
    monkeypatch.setattr(
        image_quality,
        "assess",
        AsyncMock(return_value=dict(verdict="approved", reason="no_significant_text", metrics={})),
    )
    assert await router.process_job(c, identifier, config(), generator=generator) == "ready"
    assert await c.fetchval("SELECT count(*) FROM image_generation_attempts") == 2


async def test_missing_ocr_holds_paid_result_without_automatic_duplicate(db, monkeypatch):
    c, _, identifier = await job(db)
    monkeypatch.setattr(
        image_quality,
        "assess",
        AsyncMock(return_value=dict(verdict="unavailable", reason="ocr_unavailable", metrics={})),
    )
    generator = AsyncMock(return_value=(jpeg(), {}, "charged"))
    assert (
        await router.process_job(c, identifier, config(), generator=generator)
        == "quality_unavailable"
    )
    assert (
        await router.process_job(c, identifier, config(), generator=generator)
        == "budget_or_not_due"
    )
    assert generator.await_count == 1
    assert bytes(await c.fetchval("SELECT rejected_data FROM image_quality_reviews")) == jpeg()


@pytest.mark.parametrize("error", [ValueError("no scene"), IndexError("empty model output")])
async def test_stability_does_not_reserve_money_when_free_scene_planner_fails(
    db, monkeypatch, error
):
    from app.services import visual_scene

    c, _, identifier = await job(db)
    monkeypatch.setattr(visual_scene, "describe", AsyncMock(side_effect=error))
    settings = replace(
        config(),
        key="",
        openai_keys=(),
        provider_order=("stability",),
        extra_keys={"stability": "fixture"},
    )
    assert await router.process_job(c, identifier, settings) == "providers_unavailable"
    assert await c.fetchval("SELECT count(*) FROM image_generation_attempts") == 0


async def test_repair_edits_all_originals_without_resending_or_advancing_reading(db):
    c, _, edition, _chat, row = await setup(db)
    old = await illustrations.store(c, row, edition, jpeg(), "defective")
    for user in (101, 202):
        if user != 101:
            await create_destination(c, user)
        destination = await c.fetchrow(
            "SELECT * FROM telegram_chats WHERE telegram_chat_id=$1", user
        )
        identifier = await insert_payload(
            c,
            destination,
            edition,
            "unchanged prayer",
            "original",
            "prayer",
            {"kind": "prayer"},
            frozen_chunks=[dict(kind="rich", text="unchanged prayer", image_id=old)],
        )
        await c.execute(
            "UPDATE delivery_log SET status='sent',next_chunk=1,telegram_message_ids=ARRAY[$2::bigint],sent_at=now() WHERE id=$1",
            identifier,
            user + 1000,
        )
    new = await illustrations.store(c, row, edition, jpeg(), "reviewed replacement")
    await image_quality.record(
        c, new, None, dict(verdict="approved", reason="human_and_ocr", metrics={})
    )
    first = await artwork_repairs.replace(c, old, new, "visible_text")
    assert len(first) == 2
    assert await artwork_repairs.replace(c, old, new, "visible_text") == first
    assert await c.fetchval("SELECT count(*) FROM delivery_log WHERE mode='illustration_edit'") == 2
    assert await c.fetchval("SELECT status FROM verse_illustrations WHERE id=$1", old) == "failed"

    class Sender:
        def __init__(self):
            self.sent = []

        async def send(self, chat_id, text, thread_id):
            assert (
                text["kind"] == "rich_edit"
                and text["text"] == "unchanged prayer"
                and text["image_id"] == new
            )
            self.sent.append((chat_id, text["message_id"]))
            return text["message_id"]

    sender = Sender()
    for identifier in first:
        assert await process_delivery(c, identifier, sender) == "sent"
    assert len(sender.sent) == 2
    assert await c.fetchval("SELECT count(*) FROM chat_reading_progress") == 0
    for delivery in await c.fetch("SELECT chunks FROM delivery_log WHERE mode='prayer'"):
        assert decoded(delivery["chunks"])[0]["image_id"] == old


async def test_repair_refuses_different_bible_source(db):
    c, _, edition, _chat, row = await setup(db)
    old = await illustrations.store(c, row, edition, jpeg(), "first")
    other = dict(row, text=row["text"] + " different")
    new = await illustrations.store(c, other, edition, jpeg(), "second")
    with pytest.raises(ValueError):
        await artwork_repairs.replace(c, old, new, "wrong source")


async def test_repair_preserves_current_language_and_never_sends_new_message(db, monkeypatch):
    from app.services import message_languages
    from tests.test_message_languages_postgres import card, sender
    from tests.test_postgres_integration import load_fixture

    c, settings, edition, chat, row = await setup(db)
    ru, _, _ = await load_fixture(c, db[2], "rus")
    old = await illustrations.store(c, row, edition, jpeg(), "bad page")
    _chunk, original = await card(c, edition, chat, row, image=old)
    await message_languages.select(c, chat["telegram_chat_id"], 501, original["id"], "s", ru["id"])
    before = await c.fetchrow("SELECT * FROM reading_cards WHERE id=$1", original["id"])
    new = await illustrations.store(c, row, edition, jpeg(), "reviewed")
    await image_quality.record(
        c, new, None, dict(verdict="approved", reason="human_and_ocr", metrics={})
    )
    edits = await artwork_repairs.replace(c, old, new, "visible_text")
    bot, transport = sender(c, settings, monkeypatch)
    assert len(edits) == 1 and await process_delivery(c, edits[0], transport) == "sent"
    bot.send_rich_message.assert_not_awaited()
    call = bot.edit_message_text.await_args.kwargs
    assert (
        call["message_id"] == 501
        and before["current_html"].replace("\n", "<br>") in call["rich_message"].html
    )
    after = await c.fetchrow("SELECT * FROM reading_cards WHERE id=$1", original["id"])
    assert (
        after["selected_translation_id"] == before["selected_translation_id"]
        and after["audio_id"] == before["audio_id"]
    )
