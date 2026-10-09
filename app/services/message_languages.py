"""Source-backed, paginated language choices belonging to one acknowledged message."""

from __future__ import annotations

import json
import re
from uuid import uuid4

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.services import bible
from app.services.errors import SendError, UserError
from app.services.formatting import escape, plain_text, split_message, split_wire_message
from app.services.i18n import tr, ui_for_language

PAGE_SIZE = 6


class ScriptureText(str):
    """Plain HTML plus structured references; never infer an address from prose."""

    def __new__(cls, text, edition, refs, *, locale='ru', spans=None, title_key=None):
        value = super().__new__(cls, text)
        value.edition_id = edition['id']
        value.refs = compact(refs)
        value.locale = locale
        value.spans = spans or [(0, len(plain_text(text)), ref) for ref in value.refs]
        value.title_key = title_key
        return value


def ref(row, *, whole=False):
    rows = row.get('reading_rows') or [row]
    return dict(book=row['book_code'], chapter=row['chapter'],
                first=None if whole else rows[0]['verse'],
                last=None if whole else (rows[-1].get('verse_end') or rows[-1]['verse']))


def compact(refs):
    result = []
    for item in refs:
        item = dict(item)
        if not re.fullmatch(r'[A-Z0-9]{3}', item['book']) or not 1 <= item['chapter'] <= 200:
            raise ValueError('Invalid scripture reference')
        if (item['first'] is None) != (item['last'] is None):
            raise ValueError('Incomplete scripture reference')
        if item['first'] is not None and not 1 <= item['first'] <= item['last'] <= 1000:
            raise ValueError('Invalid verse range')
        if result and result[-1]['book'] == item['book'] and result[-1]['chapter'] == item['chapter']:
            previous = result[-1]
            if previous['first'] is None:
                continue
            if item['first'] is None:
                result[-1] = item
                continue
            if previous['first'] <= item['first'] <= previous['last'] + 1:
                previous['last'] = max(previous['last'], item['last'])
                continue
        result.append(item)
    return result


def rows_text(header, rows, footer, edition, locale, *, separator='\n', whole=False):
    pieces, spans = [header], []
    offset = len(plain_text(header))
    for index, row in enumerate(rows):
        piece = (separator if index else '') + f"<b>[{bible.verse_label(row)}]</b> {escape(row['text'])}"
        size = len(plain_text(piece))
        spans.append((offset, offset + size, ref(row)))
        offset += size
        pieces.append(piece)
    pieces.append(footer)
    refs = [ref(rows[0], whole=True)] if whole else [ref(row) for row in rows]
    return ScriptureText(''.join(pieces), edition, refs, locale=locale, spans=spans)


def combine(pieces, *, separator='\n\n', footer='', title_key=None):
    if not pieces:
        return ''
    first = pieces[0]
    if not all(hasattr(piece, 'refs') for piece in pieces):
        raise ValueError('Scripture composition needs structured source references')
    html, refs, spans, offset = [], [], [], 0
    for index, piece in enumerate(pieces):
        if index:
            html.append(separator)
            offset += len(plain_text(separator))
        html.append(str(piece))
        refs.extend(piece.refs)
        spans.extend((start + offset, end + offset, item) for start, end, item in piece.spans)
        offset += len(plain_text(piece))
    return ScriptureText(''.join(html) + footer, {'id': first.edition_id}, refs,
                         locale=first.locale, spans=spans, title_key=title_key)


def with_title(source, key, locale):
    prefix = f"<b>{tr(locale, key)}</b>\n\n"
    shift = len(plain_text(prefix))
    return ScriptureText(prefix + str(source), {'id': source.edition_id}, source.refs,
                         locale=locale, title_key=key,
                         spans=[(a + shift, b + shift, item) for a, b, item in source.spans])


def inherit(target, source, *, row=None, edition=None, locale='ru'):
    if not hasattr(source, 'refs'):
        if row is None or edition is None:
            return target
        source = ScriptureText(str(source), edition, [ref(row, whole=row.get('artwork_scope') == 'chapter')], locale=locale)
    for name in ('edition_id', 'refs', 'spans', 'locale', 'title_key'):
        setattr(target, name, getattr(source, name))
    return target


def refs_for_piece(source, piece, start=0):
    full, visible = plain_text(str(source)), plain_text(piece)
    if visible == full:
        return source.refs, len(full)
    position = full.find(visible, start)
    if position < 0:
        raise ValueError('Outgoing card does not match its source snapshot')
    end = position + len(visible)
    refs = [item for left, right, item in source.spans if right > position and left < end]
    if not refs:
        # A header/footer-only fragment still belongs to its adjacent source verse.
        nearest = min(source.spans, key=lambda item: abs(item[0] - position))
        refs = [nearest[2]]
    return compact(refs), end


async def prepare(connection, text, chunks, chat, *, source_key=None):
    if not hasattr(text, 'refs'):
        return chunks
    key, offset, result = source_key or uuid4().hex, 0, []
    for index, chunk in enumerate(chunks):
        piece = chunk.get('text', chunk.get('caption', '')) if isinstance(chunk, dict) else chunk
        if not piece:
            raise ValueError('Scripture cards require visible text')
        refs, offset = refs_for_piece(text, piece, offset)
        image = chunk.get('image_id') if isinstance(chunk, dict) else None
        request = chunk.get('request_id') if isinstance(chunk, dict) else None
        identifier = await connection.fetchval("""INSERT INTO reading_cards(
            telegram_chat_id,source_key,source_translation_id,selected_translation_id,refs,
            original_html,current_html,ui_language,image_id,request_id,title_key)
            VALUES($1,$2,$3,$3,$4::jsonb,$5,$5,$6,$7,$8,$9)
            ON CONFLICT(telegram_chat_id,source_key) DO UPDATE SET source_key=EXCLUDED.source_key
            RETURNING id""", chat['telegram_chat_id'], f'{key}:{index}', text.edition_id,
            json.dumps(refs), str(piece), text.locale, image, request, text.title_key)
        from app.services.speech import attach
        await attach(connection,identifier)
        result.append(dict(kind='rich', text=str(piece), image_id=image, request_id=request, card_id=identifier))
    return result


async def bind(connection, card_id, chat_id, message_id):
    await connection.execute("""UPDATE reading_cards SET telegram_message_id=$3,updated_at=now()
        WHERE id=$1 AND telegram_chat_id=$2 AND (telegram_message_id IS NULL OR telegram_message_id=$3)""",
        card_id, chat_id, message_id)


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


async def available(connection, card):
    """Only audited editions covering every requested verse; never substitute neighbors."""
    editions = await connection.fetch("""SELECT t.*,l.code AS language_code FROM translations t
        JOIN languages l ON l.id=t.language_id WHERE t.is_active AND t.verse_count>0
        AND t.audit_status IN ('passed','passed_with_warnings') AND NOT EXISTS (
          SELECT 1 FROM jsonb_to_recordset($1::jsonb) AS r(book text,chapter int,first int,last int)
          WHERE NOT EXISTS (SELECT 1 FROM verses v WHERE v.translation_id=t.id
            AND v.book_code=r.book AND v.chapter=r.chapter AND v.text<>'' AND NOT v.is_range_continuation)
          OR (r.first IS NOT NULL AND EXISTS (
            SELECT 1 FROM generate_series(r.first,r.last) n WHERE NOT EXISTS (
              SELECT 1 FROM verses v WHERE v.translation_id=t.id AND v.book_code=r.book
              AND v.chapter=r.chapter AND v.text<>'' AND NOT v.is_range_continuation
              AND v.verse<=n AND COALESCE(v.verse_end,v.verse)>=n))))
        ORDER BY (t.id=$2) DESC,t.canonical_66_complete DESC,t.nonempty_verse_count DESC,t.id""",
        json.dumps(decoded(card['refs'])), card['source_translation_id'])
    chosen = {}
    for edition in editions:
        chosen.setdefault(edition['language_code'], edition)
    source = next((e['language_code'] for e in editions if e['id'] == card['source_translation_id']), None)
    return sorted(chosen.values(), key=lambda e: (e['language_code'] != source, e['language_code']))


def pages(text, platform='telegram'):
    if platform == 'max':
        return split_wire_message(str(text), 3600)
    if len(text.encode('utf-8')) <= 24000:
        return [str(text)]
    return split_message(str(text), 3900)


async def render(connection, card, edition):
    if edition['id'] == card['source_translation_id']:
        return card['original_html']
    locale = ui_for_language(edition['language_code']) or card['ui_language']
    sections = []
    for item in decoded(card['refs']):
        rows = await connection.fetch("""SELECT book_code,chapter,verse,verse_end,text
            FROM verses WHERE translation_id=$1 AND book_code=$2 AND chapter=$3
            AND text<>'' AND NOT is_range_continuation
            AND ($4::int IS NULL OR (verse<=$5 AND COALESCE(verse_end,verse)>=$4)) ORDER BY verse""",
            edition['id'], item['book'], item['chapter'], item['first'], item['last'])
        if not rows:
            raise UserError('no_result')
        name = await bible._book_name(connection, item['book'], edition['language_code'], edition['id'])
        suffix = '' if item['first'] is None else ':' + bible.verse_label(dict(verse=item['first'], verse_end=item['last']))
        body = '\n\n'.join(f"<b>[{bible.verse_label(row)}]</b> {escape(row['text'])}" for row in rows)
        sections.append(f"<b>{escape(name)} {item['chapter']}{suffix}</b>\n\n{body}")
    prefix = f"<b>{escape(tr(locale, card['title_key']))}</b>\n\n" if card['title_key'] else ''
    return prefix + '\n\n'.join(sections) + '\n\n' + bible.attribution(edition, locale)


async def keyboard(connection, card, *, editions=None, text_pages=None):
    editions = editions if editions is not None else await available(connection, card)
    count = max(1, (len(editions) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(card['language_page'], count - 1)
    visible = editions[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    def button(label, action, value):
        return InlineKeyboardButton(text=label, callback_data=f"lc:{card['id']}:{action}:{value}")
    buttons = []
    for edition in visible:
        code = edition['language_code']
        name = {'hbo': 'עברית', 'grc': 'Ἑλληνική'}.get(code) or bible.display_language(code, ui_for_language(code) or card['ui_language'])
        if edition['id'] == card['selected_translation_id']:
            name = '✓ ' + name
        buttons.append(button(name, 's', edition['id']))
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    if count > 1:
        rows.append([button('◀', 'p', (page - 1) % count), button(f'🌐 {page + 1}/{count}', 'p', page), button('▶', 'p', (page + 1) % count)])
    text_pages = text_pages or pages(card['current_html'], card.get('platform','telegram'))
    if len(text_pages) > 1:
        current = min(card['text_page'], len(text_pages) - 1)
        rows.append([button('‹', 't', (current - 1) % len(text_pages)), button(f'📖 {current + 1}/{len(text_pages)}', 't', current), button('›', 't', (current + 1) % len(text_pages))])
    references=decoded(card['refs'])
    if len({(r['book'],r['chapter']) for r in references})==1 and any(r['first'] is not None for r in references):
        rows.append([button('📖 Вся глава' if card['ui_language']=='ru' else '📖 Full chapter','c',0)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def outgoing(connection, chat_id, chunk):
    card = await connection.fetchrow('''SELECT r.*, c.timezone AS prayer_timezone
        FROM reading_cards r JOIN telegram_chats c USING (telegram_chat_id)
        WHERE r.id=$1 AND r.telegram_chat_id=$2''', chunk['card_id'], chat_id)
    if not card or (chunk['kind'] == 'rich_edit' and card['telegram_message_id'] != chunk.get('message_id')):
        raise SendError('rejected')
    content = pages(card['current_html'], card.get('platform','telegram'))
    result = dict(chunk, text=content[min(card['text_page'], len(content) - 1)], image_id=card['image_id'])
    if card.get('prayer_at'):
        from app.services.prayers import reminder
        result['text']=reminder(card['prayer_at'],card['ui_language'],timezone_name=card['prayer_timezone'])+'\n\n'+result['text']
    from app.services.speech import media
    audio = await media(connection,card)
    result['audio_id'] = audio['id'] if audio else None
    return result, await keyboard(connection, card, text_pages=content)


async def select(connection, chat_id, message_id, card_id, action, value):
    """Called after authorization while holding the same chat lock as delivery."""
    from app.worker.delivery import insert_payload

    async with connection.transaction():
        card = await connection.fetchrow("""SELECT * FROM reading_cards
            WHERE id=$1 AND telegram_chat_id=$2 AND telegram_message_id=$3 FOR UPDATE""", card_id, chat_id, message_id)
        if not card:
            raise UserError('invalid')
        if action=='c':
            references=decoded(card['refs'])
            if value!=0 or len({(r['book'],r['chapter']) for r in references})!=1:
                raise UserError('invalid')
            reference=references[0]
            edition=await bible.find_translation(connection,card['selected_translation_id'])
            if not edition:
                raise UserError('no_result')
            text=await bible.render_chapter(connection,edition,reference['book'],reference['chapter'],ui_language=card['ui_language'])
            if not text:
                raise UserError('no_result')
            chat=await connection.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=$1',chat_id)
            key=f"context:{card_id}:{card['revision']}:{edition['id']}"
            from app.services.illustrations import decorate_chapter
            text=await decorate_chapter(connection,text,edition,reference['book'],reference['chapter'],chat=chat,request_key=key)
            return await insert_payload(connection,chat,edition,text,key,'manual',{'kind':'reading'})
        editions = await available(connection, card)
        state = dict(card)
        if action == 's':
            edition = next((e for e in editions if e['id'] == value), None)
            if not edition:
                raise UserError('no_result')
            state.update(selected_translation_id=value, current_html=await render(connection, card, edition), text_page=0)
        elif action == 'p':
            if not 0 <= value < max(1, (len(editions) + PAGE_SIZE - 1) // PAGE_SIZE):
                raise UserError('invalid')
            state['language_page'] = value
        elif action == 't':
            if not 0 <= value < len(pages(card['current_html'], card.get('platform','telegram'))):
                raise UserError('invalid')
            state['text_page'] = value
        else:
            raise UserError('invalid')
        state['revision'] += 1
        await connection.execute("""UPDATE reading_cards SET selected_translation_id=$2,current_html=$3,
            language_page=$4,text_page=$5,revision=$6,updated_at=now() WHERE id=$1""",
            card_id, state['selected_translation_id'], state['current_html'], state['language_page'], state['text_page'], state['revision'])
        if action in {'s','t'}:
            from app.services.speech import attach
            await attach(connection,card_id)
        chat = await connection.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=$1', chat_id)
        edition = await bible.find_translation(connection, card['source_translation_id'])
        if not edition:
            raise UserError('no_result')
        return await insert_payload(connection, chat, edition, state['current_html'],
            f"card:{card_id}:{state['revision']}", 'illustration_edit', {'kind': 'illustration_edit'},
            frozen_chunks=[dict(kind='rich_edit', message_id=message_id, card_id=card_id,
                                image_id=state['image_id'], text=state['current_html'])])
