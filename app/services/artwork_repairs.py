"""Quarantine defective assets and edit their original messages through the outbox."""

from __future__ import annotations

import json
import re

from app.services import bible, illustrations, image_quality
from app.services.locks import chat_lock

IDENTITY = ("translation_id", "book_code", "chapter", "verse", "text_sha256", "artwork_scope")


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def clean_prayer_label(text):
    return re.sub(r"\n<i>\d{2}:\d{2} · [A-Za-z_+-]+/[A-Za-z_+\-/]+</i>(?=\n)", "", text, count=1)


async def quarantine(connection, image_id, reason):
    async with connection.transaction():
        await connection.execute(
            "UPDATE verse_illustrations SET status='failed',updated_at=now() WHERE id=$1", image_id
        )
        await connection.execute(
            """INSERT INTO artwork_replacements(old_image_id,reason) VALUES($1,$2)
            ON CONFLICT(old_image_id) DO NOTHING""",
            image_id,
            reason,
        )
        await image_quality.record(
            connection,
            image_id,
            None,
            dict(verdict="rejected", reason=reason, metrics={"human_review": True}),
        )


async def replace(connection, old_id, new_id, reason):
    """Retain historical sends and paid attempts. Repeated calls cannot duplicate edits."""
    from app.worker.delivery import insert_payload

    old = await connection.fetchrow("SELECT * FROM verse_illustrations WHERE id=$1", old_id)
    new = await connection.fetchrow("SELECT * FROM verse_illustrations WHERE id=$1", new_id)
    if (
        not old
        or not new
        or old_id == new_id
        or new["status"] != "ready"
        or any(old[k] != new[k] for k in IDENTITY)
    ):
        raise ValueError("Replacement must have the same immutable Bible source")
    source = await illustrations.source_row(connection, new)
    edition = await bible.find_translation(connection, new["translation_id"])
    if (
        not source
        or not edition
        or illustrations.identity(source, edition)[4] != new["text_sha256"]
    ):
        raise ValueError("Replacement source has changed")
    if (
        await connection.fetchval(
            "SELECT verdict FROM image_quality_reviews WHERE image_id=$1 ORDER BY id DESC LIMIT 1",
            new_id,
        )
        != "approved"
    ):
        raise ValueError("Replacement requires a quality approval")
    async with connection.transaction():
        await connection.execute(
            "UPDATE verse_illustrations SET status='failed',updated_at=now() WHERE id=$1", old_id
        )
        await connection.execute(
            """INSERT INTO artwork_replacements(old_image_id,new_image_id,reason) VALUES($1,$2,$3)
            ON CONFLICT(old_image_id) DO UPDATE SET new_image_id=EXCLUDED.new_image_id,reason=EXCLUDED.reason""",
            old_id,
            new_id,
            reason,
        )
        await connection.execute(
            "UPDATE scheduled_readings SET image_id=$2 WHERE image_id=$1", old_id, new_id
        )
        await connection.execute(
            "UPDATE prayer_occurrences SET image_id=$2 WHERE image_id=$1", old_id, new_id
        )
        await connection.execute(
            "UPDATE illustration_requests SET image_id=$2 WHERE image_id=$1 AND state='waiting'",
            old_id,
            new_id,
        )
    # Find every acknowledged original, including prayer messages without reading cards.
    targets = {}
    for delivery in await connection.fetch("""SELECT telegram_chat_id,chunks,telegram_message_ids
        FROM delivery_log WHERE next_chunk>0 ORDER BY id"""):
        for index, chunk in enumerate(decoded(delivery["chunks"])):
            if not isinstance(chunk, dict) or chunk.get("image_id") != old_id:
                continue
            ids = delivery["telegram_message_ids"]
            if index >= len(ids):
                continue
            message = chunk.get("message_id") if chunk.get("kind") == "rich_edit" else ids[index]
            if message and chunk.get("kind") in {"rich", "rich_edit"}:
                targets[(delivery["telegram_chat_id"], message)] = chunk
    cards = await connection.fetch("SELECT * FROM reading_cards WHERE image_id=$1", old_id)
    for card in cards:
        if card["telegram_message_id"]:
            targets[(card["telegram_chat_id"], card["telegram_message_id"])] = dict(
                card_id=card["id"], text=card["current_html"]
            )
    identifiers = []
    for (chat_id, message), chunk in targets.items():
        async with chat_lock(connection, chat_id), connection.transaction():
            chat = await connection.fetchrow(
                "SELECT * FROM telegram_chats WHERE telegram_chat_id=$1", chat_id
            )
            if not chat or not chat["is_active"]:
                continue
            card_id = chunk.get("card_id")
            if card_id:
                card = await connection.fetchrow(
                    "SELECT * FROM reading_cards WHERE id=$1 AND telegram_chat_id=$2 FOR UPDATE",
                    card_id,
                    chat_id,
                )
                if (
                    not card
                    or card["telegram_message_id"] != message
                    or card["image_id"] not in {old_id, new_id}
                ):
                    continue
                await connection.execute(
                    "UPDATE reading_cards SET image_id=$2,updated_at=now() WHERE id=$1",
                    card_id,
                    new_id,
                )
                chunk = dict(chunk, text=card["current_html"])
            repaired = dict(chunk, kind="rich_edit", message_id=message, image_id=new_id)
            if not card_id:
                repaired["text"] = clean_prayer_label(repaired["text"])
            repaired.pop("request_id", None)
            identifier = await insert_payload(
                connection,
                chat,
                edition,
                repaired["text"],
                f"artwork-repair:{old_id}:{new_id}:{message}",
                "illustration_edit",
                dict(
                    kind="illustration_edit", repair_old_image_id=old_id, repair_new_image_id=new_id
                ),
                frozen_chunks=[repaired],
            )
            identifiers.append(identifier)
    # Unsent cards are also updated; their normal outbox send reads current card state.
    for card in cards:
        if not card["telegram_message_id"]:
            async with chat_lock(connection, card["telegram_chat_id"]):
                await connection.execute(
                    "UPDATE reading_cards SET image_id=$2,updated_at=now() WHERE id=$1 AND image_id=$3",
                    card["id"],
                    new_id,
                    old_id,
                )
    return identifiers


async def clean_sent_prayers(connection):
    """Remove technical timezone labels from original prayer invitations, once."""
    from app.worker.delivery import insert_payload

    identifiers = []
    for row in await connection.fetch(
        "SELECT * FROM delivery_log WHERE mode='prayer' AND next_chunk>0 ORDER BY id"
    ):
        for index, part in enumerate(decoded(row["chunks"])):
            if (
                not isinstance(part, dict)
                or part.get("kind") != "rich"
                or index >= len(row["telegram_message_ids"])
            ):
                continue
            text = clean_prayer_label(part["text"])
            if text == part["text"]:
                continue
            async with chat_lock(connection, row["telegram_chat_id"]), connection.transaction():
                chat = await connection.fetchrow(
                    "SELECT * FROM telegram_chats WHERE telegram_chat_id=$1",
                    row["telegram_chat_id"],
                )
                edition = await bible.find_translation(connection, row["translation_id"])
                if not chat or not chat["is_active"] or not edition:
                    continue
                image = part.get("image_id")
                if image:
                    image = await connection.fetchval(
                        "SELECT COALESCE(r.new_image_id,i.id) FROM verse_illustrations i LEFT JOIN artwork_replacements r ON r.old_image_id=i.id WHERE i.id=$1",
                        image,
                    )
                    if not await connection.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM verse_illustrations WHERE id=$1 AND status='ready')",
                        image,
                    ):
                        continue
                message = row["telegram_message_ids"][index]
                identifiers.append(
                    await insert_payload(
                        connection,
                        chat,
                        edition,
                        text,
                        f"prayer-format-v2:{message}",
                        "illustration_edit",
                        dict(kind="illustration_edit"),
                        frozen_chunks=[
                            dict(
                                part,
                                kind="rich_edit",
                                message_id=message,
                                image_id=image,
                                text=text,
                            )
                        ],
                    )
                )
    return identifiers
