"""Indexed Bible lookup; text, numbering, provenance and destination UI are distinct."""
from __future__ import annotations
import hashlib
import secrets
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urlparse
from app.catalog.profiles import normalize_language_code
from app.services.formatting import escape
from app.services.i18n import tr, ui_for_language


@dataclass(frozen=True, slots=True)
class TranslationChoice:
    """An actually imported and audited edition, not a planned catalog entry."""
    id: int
    language_code: str
    source_id: str
    title: str
    short_title: str
    coverage: str
    license_type: str
    books: int = 0
    verses: int = 0


async def list_translations(connection: Any) -> list[TranslationChoice]:
    """Advertise usable editions only, with physical counts and measured coverage."""
    rows = await connection.fetch("""SELECT t.*,l.code AS language_code FROM translations t
        JOIN languages l ON l.id=t.language_id WHERE t.is_active AND t.verse_count>0
        AND t.audit_status IN ('passed','passed_with_warnings') ORDER BY l.code,t.title""")
    return [TranslationChoice(r['id'],r['language_code'],r['source_translation_id'],r['title'],
        r['short_title'] or r['title'],r['coverage'],r['license_type'],r['book_count'],r['verse_count']) for r in rows]


async def find_translation(connection: Any, identifier: str | int | None,
                           *, preferred_language: str | None = None) -> Any:
    """Never substitute a different edition/language for an explicit selection."""
    base = """SELECT t.*,l.code AS language_code,l.text_direction FROM translations t
        JOIN languages l ON l.id=t.language_id WHERE t.is_active AND t.verse_count>0
        AND t.audit_status IN ('passed','passed_with_warnings') """
    if identifier is not None:
        if isinstance(identifier,int) or str(identifier).isdigit():
            return await connection.fetchrow(base+'AND t.id=$1',int(identifier))
        return await connection.fetchrow(base+'AND lower(t.source_translation_id)=lower($1)',str(identifier))
    language = normalize_language_code(preferred_language)
    if not language:
        return None
    return await connection.fetchrow(base+'''AND l.code=$1
        ORDER BY t.canonical_66_complete DESC,t.nonempty_verse_count DESC,t.source_translation_id LIMIT 1''',language)


async def chat_translation(connection: Any, telegram_chat_id: int,
                           *, fallback_language: str | None = None) -> Any:
    """Groups use their own saved language, never that of the latest message author."""
    row = await connection.fetchrow('SELECT default_translation_id,bible_language_code,ui_language FROM telegram_chats WHERE telegram_chat_id=$1',telegram_chat_id)
    if not row:
        return None
    language = row['bible_language_code'] or normalize_language_code(row['ui_language'])
    return await find_translation(connection,row['default_translation_id'],preferred_language=language)


async def user_translation(connection: Any, telegram_user_id: int,
                           telegram_language_code: str | None = None) -> Any:
    """Compatibility helper; private chats share the destination settings mechanism."""
    return await chat_translation(connection,telegram_user_id)


async def _book_name(connection: Any, book_code: str, language_code: str) -> str:
    """Use a localized book name when present, otherwise the language-neutral USFM code."""
    locale = ui_for_language(language_code) or language_code
    return await connection.fetchval('SELECT name FROM book_names WHERE book_code=$1 AND language_code=$2',book_code,locale) or book_code


def verse_label(row: Any) -> str:
    """Show verse ranges as ranges, not as an incorrectly attributed single verse."""
    last = row.get('verse_end') or row['verse']
    return str(row['verse']) if last == row['verse'] else f"{row['verse']}–{last}"


def _safe_link(value: str | None) -> str | None:
    parsed = urlparse(value or '')
    return value if parsed.scheme in {'http','https'} and parsed.hostname and not parsed.username else None


def attribution(translation: Any, ui_language: str) -> str:
    """Keep the edition's license, rights holder and source with redistributed text."""
    title = translation.get('short_title') or translation['title']
    lines = [f'<i>{escape(title)}</i>']
    rights = list(dict.fromkeys(x for x in (
        translation.get('copyright_notice'),translation.get('copyright_holder'),translation.get('translated_by')) if x))
    if rights:
        lines.append(escape(' · '.join(rights)))
    license_name = translation['license_type']
    license_url = _safe_link(translation.get('license_url'))
    license_part = f'<a href="{escape(license_url).replace(chr(34), "&quot;")}">{escape(license_name)}</a>' if license_url else escape(license_name)
    source_url = _safe_link(translation.get('publication_url')) or _safe_link(translation.get('source_file_url'))
    source = f'<a href="{escape(source_url).replace(chr(34), "&quot;")}">eBible / BibleNLP</a>' if source_url else 'eBible / BibleNLP'
    lines.append(f"{tr(ui_language,'source')}: {source} · {license_part}")
    lines.append(f"<i>{tr(ui_language,'numbering_note')}</i>")
    return '\n'.join(lines)


async def render_verse(connection: Any, row: Any, translation: Any,
                       *, ui_language: str = 'ru') -> str:
    """Escape verse text verbatim and retain the source reference range."""
    name = await _book_name(connection,row['book_code'],translation['language_code'])
    return (f"<blockquote>{escape(row['text'])}</blockquote>\n"
            f"<b>{escape(name)} {row['chapter']}:{verse_label(row)}</b>\n"+attribution(translation,ui_language))


async def _ordinal_verse(connection: Any, translation: Any, ordinal: int) -> Any:
    """Dense ordinals avoid repeated random OFFSET scans over the entire edition."""
    return await connection.fetchrow('''SELECT book_code,chapter,verse,verse_end,text FROM verses
        WHERE translation_id=$1 AND ordinal=$2''',translation['id'],ordinal)


async def random_verse(connection: Any, translation: Any) -> Any:
    """Choose one visible source verse/range uniformly within the selected edition."""
    count = translation['nonempty_verse_count']
    return await _ordinal_verse(connection,translation,secrets.randbelow(count)+1) if count else None


async def verse_of_day(connection: Any, translation: Any, seed: str, on_date: date | None = None) -> Any:
    """Stable daily selection for the specified destination's local date."""
    count = translation['nonempty_verse_count']
    if not count:
        return None
    digest = hashlib.sha256(f"{on_date or date.today()}:{seed}:{translation['source_translation_id']}".encode()).digest()
    return await _ordinal_verse(connection,translation,int.from_bytes(digest[:8],'big') % count + 1)


async def topic_verse(connection: Any, translation: Any, topic_code: str | None = None,
                      seed: str = '', on_date: date | None = None) -> tuple[str, Any] | None:
    """Choose only themes that have actual text in this particular edition."""
    rows = await connection.fetch('''SELECT tr.topic_code,v.book_code,v.chapter,v.verse,v.verse_end,v.text
        FROM topic_references tr JOIN topics t ON t.code=tr.topic_code AND t.is_active
        JOIN verses v ON v.translation_id=$1 AND v.book_code=tr.book_code AND v.chapter=tr.chapter
        AND v.verse BETWEEN tr.verse_from AND tr.verse_to
        WHERE v.text<>'' AND NOT v.is_range_continuation AND ($2::text IS NULL OR tr.topic_code=$2)
        ORDER BY tr.topic_code,v.source_line''',translation['id'],topic_code)
    if not rows:
        return None
    digest = hashlib.sha256(f'{on_date or date.today()}:{seed}:{topic_code or "all"}'.encode()).digest()
    row = rows[int.from_bytes(digest[:8],'big') % len(rows)]
    return row['topic_code'],row


async def chapter_rows(connection: Any, translation_id: int, book_code: str, chapter: int) -> list[Any]:
    """Read a chapter by its composite primary key."""
    return await connection.fetch('''SELECT book_code,chapter,verse,verse_end,text,is_range_continuation
        FROM verses WHERE translation_id=$1 AND book_code=$2 AND chapter=$3 ORDER BY verse''',translation_id,book_code,chapter)


async def render_chapter(connection: Any, translation: Any, book_code: str, chapter: int,
                         *, ui_language: str = 'ru', with_attribution: bool = True) -> str | None:
    """Render visible anchors; <range> continuation tokens are never sent as Bible text."""
    rows = await chapter_rows(connection,translation['id'],book_code,chapter)
    visible = [r for r in rows if r['text'] and not r['is_range_continuation']]
    if not visible:
        return None
    name = await _book_name(connection,book_code,translation['language_code'])
    body = '\n'.join(f"<b>{verse_label(r)}</b> {escape(r['text'])}" for r in visible)
    return f'<b>{escape(name)} {chapter}</b>\n\n{body}'+('\n\n'+attribution(translation,ui_language) if with_attribution else '')


async def first_chapter(connection: Any, translation_id: int, *, testament: str | None = None) -> Any:
    """Use the materialized chapter index instead of a verses GROUP BY per message."""
    return await connection.fetchrow('''SELECT c.book_code,c.chapter FROM translation_chapters c
        JOIN books b ON b.code=c.book_code WHERE c.translation_id=$1
        AND ($2::text IS NULL OR b.testament=$2) ORDER BY c.position LIMIT 1''',translation_id,testament)


async def next_chapter_reference(connection: Any, translation_id: int, book_code: str | None,
    chapter: int | None, *, testament: str | None = None) -> tuple[str,int] | None:
    """Return the chapter after the last fully delivered chapter."""
    position = 0
    if book_code and chapter:
        position = await connection.fetchval('SELECT position FROM translation_chapters WHERE translation_id=$1 AND book_code=$2 AND chapter=$3',translation_id,book_code,chapter)
        if position is None:
            raise ValueError('Saved progress does not exist in the current edition; explicit reset required')
    row = await connection.fetchrow('''SELECT c.book_code,c.chapter FROM translation_chapters c JOIN books b ON b.code=c.book_code
        WHERE c.translation_id=$1 AND c.position>$2 AND ($3::text IS NULL OR b.testament=$3)
        ORDER BY c.position LIMIT 1''',translation_id,position,testament)
    return (row['book_code'],row['chapter']) if row else None


async def next_for_user(connection: Any, telegram_user_id: int, translation: Any,
                        *, plan_code: str = 'sequential') -> tuple[str,int,str] | None:
    """Read-only preview for compatibility; progress is committed only by the outbox."""
    saved = await connection.fetchrow('SELECT * FROM chat_reading_progress WHERE telegram_chat_id=$1 AND translation_id=$2',telegram_user_id,translation['id'])
    reference = await next_chapter_reference(connection,translation['id'],saved['book_code'] if saved else None,saved['chapter'] if saved else None)
    if not reference:
        return None
    text = await render_chapter(connection,translation,*reference)
    return (*reference,text) if text else None


async def search_verses(connection: Any, translation_id: int, query: str, limit: int = 10) -> list[Any]:
    """Literal substring search: SQL wildcards in user input have no special meaning."""
    query = query.strip()
    if not 2 <= len(query) <= 200:
        return []
    escaped_query = query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
    return await connection.fetch("""SELECT book_code,chapter,verse,verse_end,text FROM verses
        WHERE translation_id=$1 AND text<>'' AND search_text LIKE lower($2) ESCAPE '\\'
        ORDER BY source_line LIMIT $3""",translation_id,'%'+escaped_query+'%',min(max(limit,1),20))
