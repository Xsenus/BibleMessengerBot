"""Seed static books, topics, and reading plan metadata."""

from __future__ import annotations

import json
from pathlib import Path

from typing import Any

from app.catalog.importer import seed_books


async def seed_static_content(connection: Any) -> None:
    await seed_books(connection)
    root = Path(__file__).resolve().parents[2]
    topics = json.loads((root / "data" / "topics.json").read_text(encoding="utf-8"))
    for topic in topics:
        await connection.execute(
            """
            INSERT INTO topics(code, title_ru, title_en, description_ru)
            VALUES($1, $2, $3, $2)
            ON CONFLICT (code) DO UPDATE SET
                title_ru=EXCLUDED.title_ru, title_en=EXCLUDED.title_en,
                description_ru=EXCLUDED.description_ru, is_active=true
            """,
            topic["code"],
            topic["title_ru"],
            topic["title_en"],
        )
        await connection.execute("DELETE FROM topic_references WHERE topic_code=$1", topic["code"])
        await connection.executemany(
            """
            INSERT INTO topic_references(
                topic_code, book_code, chapter, verse_from, verse_to, weight
            ) VALUES($1, $2, $3, $4, $5, $6)
            """,
            [
                (
                    topic["code"],
                    ref["book_code"],
                    ref["chapter"],
                    ref["verse_from"],
                    ref["verse_to"],
                    ref.get("weight", 100),
                )
                for ref in topic["references"]
            ],
        )

    plans = [
        ("bible-90", "Библия за 90 дней", "Последовательное чтение повышенного темпа", 90),
        ("bible-180", "Библия за 180 дней", "Последовательное чтение среднего темпа", 180),
        ("bible-365", "Библия за год", "Последовательное чтение на каждый день года", 365),
        ("new-testament-90", "Новый Завет за 90 дней", "Чтение Нового Завета", 90),
        ("psalms-150", "Псалтирь за 150 дней", "По одному псалму в день", 150),
    ]
    await connection.executemany(
        """
        INSERT INTO reading_plans(code, name, description, duration_days, language_code)
        VALUES($1, $2, $3, $4, 'ru')
        ON CONFLICT (code) DO UPDATE SET
            name=EXCLUDED.name, description=EXCLUDED.description,
            duration_days=EXCLUDED.duration_days, is_active=true
        """,
        plans,
    )
