"""Resolve chapter and verse addresses against the selected edition's numbering."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.services.errors import UserError

PATTERN = re.compile(
    r"^(.+?)\s*([1-9][0-9]{0,2})(?:\s*[:.,]\s*([1-9][0-9]{0,2})(?:\s*[-–—]\s*([1-9][0-9]{0,2}))?)?$"
)
SHORT_RU = {
    "GEN": "Быт Бытия",
    "EXO": "Исх Исхода",
    "LEV": "Лев Левита",
    "NUM": "Чис Чисел",
    "DEU": "Втор Второзакония",
    "JOS": "Нав ИисНав ИисусаНавина",
    "JDG": "Суд Судей",
    "RUT": "Руф Руфи",
    "1SA": "1Цар 1Сам 1Царств",
    "2SA": "2Цар 2Сам 2Царств",
    "1KI": "3Цар 3Царств",
    "2KI": "4Цар 4Царств",
    "1CH": "1Пар",
    "2CH": "2Пар",
    "EZR": "Езд Ездры",
    "NEH": "Неем Неемии",
    "EST": "Есф Есфири",
    "JOB": "Иов Иова",
    "PSA": "Пс Псалом Псалмы Псалмов Псалтирь",
    "PRO": "Прит Притч Притчи",
    "ECC": "Еккл Екклесиаста",
    "SNG": "Песн ПесньПесней",
    "ISA": "Ис Исаии",
    "JER": "Иер Иеремии",
    "LAM": "Плач",
    "EZK": "Иез Иезекииля",
    "DAN": "Дан Даниила",
    "HOS": "Ос Осии",
    "JOL": "Иоил Иоиля",
    "AMO": "Ам Амоса",
    "OBA": "Авд Авдия",
    "JON": "Ион Ионы",
    "MIC": "Мих Михея",
    "NAM": "Наум Наума",
    "HAB": "Авв Аввакума",
    "ZEP": "Соф Софонии",
    "HAG": "Агг Аггея",
    "ZEC": "Зах Захарии",
    "MAL": "Мал Малахии",
    "MAT": "Мф Матф Матфея",
    "MRK": "Мк Мар Марка",
    "LUK": "Лк Лук Луки",
    "JHN": "Ин Иоан Иоанна",
    "ACT": "Деян Деяния",
    "ROM": "Рим Римлянам",
    "1CO": "1Кор 1Коринфянам",
    "2CO": "2Кор 2Коринфянам",
    "GAL": "Гал Галатам",
    "EPH": "Еф Ефесянам",
    "PHP": "Флп Филиппийцам",
    "COL": "Кол Колоссянам",
    "1TH": "1Фес 1Сол 1Фессалоникийцам",
    "2TH": "2Фес 2Сол 2Фессалоникийцам",
    "1TI": "1Тим 1Тимофею",
    "2TI": "2Тим 2Тимофею",
    "TIT": "Тит Титу",
    "PHM": "Флм Филимону",
    "HEB": "Евр Евреям",
    "JAS": "Иак Иакова",
    "1PE": "1Пет 1Петра",
    "2PE": "2Пет 2Петра",
    "1JN": "1Ин 1Иоан 1Иоанна",
    "2JN": "2Ин 2Иоан 2Иоанна",
    "3JN": "3Ин 3Иоан 3Иоанна",
    "JUD": "Иуд Иуды",
    "REV": "Откр Откровение Апокалипсис",
}
SHORT_EN = {
    "GEN": "Gen Ge",
    "EXO": "Ex Exod",
    "LEV": "Lev",
    "NUM": "Num",
    "DEU": "Deut",
    "PSA": "Ps Psa Psalm Psalms",
    "PRO": "Prov",
    "ISA": "Isa",
    "EZK": "Ezek",
    "MAT": "Mt Matt",
    "MRK": "Mk Mark",
    "LUK": "Lk Luke",
    "JHN": "Jn John",
    "ACT": "Acts",
    "ROM": "Rom",
    "1CO": "1Cor",
    "2CO": "2Cor",
    "PHP": "Phil",
    "1TH": "1Thess",
    "2TH": "2Thess",
    "1TI": "1Tim",
    "2TI": "2Tim",
    "JAS": "Jas James",
    "1PE": "1Pet",
    "2PE": "2Pet",
    "1JN": "1John",
    "2JN": "2John",
    "3JN": "3John",
    "REV": "Rev Revelation",
}


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return "".join(c for c in value if c.isalnum())


@dataclass(frozen=True)
class Reference:
    book: str
    chapter: int
    first: int | None = None
    last: int | None = None


def parse_reference(query: str) -> Reference | None:
    if not 1 <= len(query.strip()) <= 200:
        return None
    match = PATTERN.fullmatch(query.strip())
    if not match:
        return None
    book, chapter, first, last = match.groups()
    if not re.fullmatch(r"[1-4]?[^\W\d_]+", normalized(book)):
        return None
    if first and last and int(last) < int(first):
        raise UserError("invalid")
    return Reference(
        book.strip(),
        int(chapter),
        int(first) if first else None,
        int(last or first) if first else None,
    )


@lru_cache(maxsize=1)
def aliases() -> dict[str, str]:
    result = {}
    rows = json.loads(
        (Path(__file__).resolve().parents[2] / "data/books.json").read_text(encoding="utf-8")
    )
    for row in rows:
        for name in [
            row["code"],
            row["default_name"],
            *row["names"].values(),
            *SHORT_RU.get(row["code"], "").split(),
            *SHORT_EN.get(row["code"], "").split(),
        ]:
            result[normalized(name)] = row["code"]
    return result


async def resolve_book(connection: Any, translation_id: int, name: str) -> str | None:
    key = normalized(name)
    if key in aliases():
        return aliases()[key]
    rows = await connection.fetch(
        """SELECT book_code,name FROM translation_book_names WHERE translation_id=$1
        UNION SELECT book_code,name FROM book_names""",
        translation_id,
    )
    matches = {r["book_code"] for r in rows if normalized(r["name"]) == key}
    return matches.pop() if len(matches) == 1 else None


async def lookup(
    connection: Any, translation: Any, query: str
) -> tuple[Reference, str, list[Any]] | None:
    reference = parse_reference(query)
    if reference is None:
        return None
    book = await resolve_book(connection, translation["id"], reference.book)
    if not book:
        raise UserError("no_result")
    rows = await connection.fetch(
        """SELECT book_code,chapter,verse,verse_end,text,is_range_continuation
        FROM verses WHERE translation_id=$1 AND book_code=$2 AND chapter=$3 AND text<>''
        AND NOT is_range_continuation AND ($4::int IS NULL OR
            (verse<=$5 AND COALESCE(verse_end,verse)>=$4)) ORDER BY verse""",
        translation["id"],
        book,
        reference.chapter,
        reference.first,
        reference.last,
    )
    if not rows:
        raise UserError("no_result")
    # A non-existent requested endpoint must not silently return a different verse.
    if reference.first is not None and (
        not any(r["verse"] <= reference.first <= (r["verse_end"] or r["verse"]) for r in rows)
        or not any(r["verse"] <= reference.last <= (r["verse_end"] or r["verse"]) for r in rows)
    ):
        raise UserError("no_result")
    return reference, book, rows


async def render(connection: Any, translation: Any, result: tuple, locale: str) -> str:
    from app.services import bible

    reference, book, rows = result
    name = await bible._book_name(connection, book, translation["language_code"], translation["id"])
    from app.services.message_languages import rows_text
    return rows_text(f"<b>{bible.escape(name)} {reference.chapter}</b>\n\n", rows,
        '\n\n'+bible.attribution(translation, locale), translation, locale, whole=reference.first is None)
