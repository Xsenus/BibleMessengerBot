"""Молитвослов: готовые подборки отрывков Библии для молитвы вслух (о прощении, исцелении, семье и т. д.).

Состав подборок задан здесь, а тексты берутся из издания Библии чата, поэтому молитва показывается в том переводе и на том языке,
которые выбрал пользователь. Единственный собственный текст — молитва о прощении грехов (из памятки). Номера псалмов заданы
по синодальной нумерации; в изданиях с еврейской нумерацией (на 1 больше с 10-го псалма) номер пересчитывается автоматически.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiogram.types import InlineKeyboardMarkup

from app.bot.common import button
from app.services import bible, passage
from app.services.errors import UserError
from app.services.formatting import escape
from app.services.message_languages import combine, rows_text


@dataclass(frozen=True)
class Ref:
    """Отрывок: книга, глава и необязательный диапазон стихов (без стихов — глава целиком)."""
    book: str
    chapter: int
    first: int | None = None
    last: int | None = None


@dataclass(frozen=True)
class Prayer:
    code: str
    emoji: str
    title: dict[str, str]
    refs: tuple[Ref, ...]
    text: dict[str, str] | None = None       # собственный текст молитвы (после отрывков)
    intro: dict[str, str] | None = None      # пояснение перед молитвой


SINNER_RU = ('Всевышний Бог, Отец Небесный! Благодарю Тебя за Сына Твоего Единственного, Иисуса Христа! '
             'Я верю, что Он умер за мои грехи и воскрес! Господи, прости меня! Иисус Христос, войди в моё сердце '
             'и возьми мою жизнь в Свои руки! Воскреси меня от прежнего образа жизни к новой жизни по воле Твоей! '
             'Стань моим Господом и Спасителем. Спаси меня и сохрани! Благодарю и славлю Тебя! '
             'Во имя Отца, и Сына, и Святого Духа. Аминь.')
SINNER_EN = ('Almighty God, Heavenly Father! Thank You for Your only Son, Jesus Christ! '
             'I believe that He died for my sins and rose again! Lord, forgive me! Jesus Christ, come into my heart '
             'and take my life into Your hands! Raise me from my old way of life to a new life according to Your will! '
             'Become my Lord and Savior. Save me and keep me! I thank and praise You! '
             'In the name of the Father, and the Son, and the Holy Spirit. Amen.')

PRAYERS: tuple[Prayer, ...] = (
    Prayer('forgive', '🕊', {'ru': 'О прощении грехов', 'en': 'Forgiveness of sins'}, (Ref('JHN', 3, 16, 16),),
           text={'ru': SINNER_RU, 'en': SINNER_EN},
           intro={'ru': 'Чтобы Господь простил ваши грехи, необходимо искренне доверить свою жизнь Богу и попросить у Него прощения в молитве.',
                  'en': 'For the Lord to forgive your sins, sincerely entrust your life to God and ask Him for forgiveness in prayer.'}),
    Prayer('ourfather', '🙏', {'ru': 'Отче наш', 'en': "The Lord's Prayer"}, (Ref('MAT', 6, 9, 13),)),
    Prayer('healing', '💚', {'ru': 'Об исцелении', 'en': 'Healing'}, (Ref('PSA', 6), Ref('ISA', 53, 4, 6), Ref('PSA', 102))),
    Prayer('family', '👨‍👩‍👧', {'ru': 'За семью и детей', 'en': 'Family and children'}, (Ref('PSA', 1), Ref('PSA', 100), Ref('PSA', 127))),
    Prayer('prosperity', '🌾', {'ru': 'О благополучии и достатке', 'en': 'Prosperity and provision'}, (Ref('PSA', 22), Ref('PSA', 111))),
    Prayer('protection', '🛡', {'ru': 'О мире и защите', 'en': 'Peace and protection'}, (Ref('PSA', 90),),
           intro={'ru': 'В стране, городе, своей жизни.', 'en': 'For your country, city and your own life.'}),
    Prayer('commandments', '📜', {'ru': '10 заповедей', 'en': 'The Ten Commandments'}, (Ref('EXO', 20, 2, 17),)),
)
BY_CODE = {prayer.code: prayer for prayer in PRAYERS}
ALOUD = {'ru': 'Молиться вслух', 'en': 'Pray aloud'}


def lang(locale: str) -> str:
    """Язык подписей: русский или английский (для остальных языков интерфейса — английский)."""
    return 'ru' if locale == 'ru' else 'en'


def menu(chat: Any) -> tuple[str, InlineKeyboardMarkup]:
    """Список молитв кнопками."""
    code = lang(chat['ui_language'])
    chat_id = chat['telegram_chat_id']
    text = ('🙏 <b>Молитвослов</b>\nВыберите молитву: тексты берутся из вашего издания Библии. Молитесь вслух.' if code == 'ru'
            else '🙏 <b>Prayer book</b>\nChoose a prayer: the texts come from your Bible edition. Pray aloud.')
    rows = [[button(f"{prayer.emoji} {prayer.title[code]}", 'pb', chat_id, prayer.code)] for prayer in PRAYERS]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def uses_synodal_psalms(connection: Any, edition: Any) -> bool | None:
    """Синодальная ли нумерация псалмов в издании: в ней 22-й псалом («Господь — Пастырь мой») короткий, в еврейской — длинный (31 стих)."""
    count = await connection.fetchval(
        "SELECT max(verse) FROM verses WHERE translation_id=$1 AND book_code='PSA' AND chapter=22 AND text<>''", edition['id'])
    return None if count is None else count <= 10


async def piece_for(connection: Any, edition: Any, ref: Ref, synodal: bool | None, locale: str, header_prefix: str = '') -> Any | None:
    """Текст одного отрывка в издании чата; None, если в издании его нет."""
    chapter = ref.chapter + 1 if ref.book == 'PSA' and ref.chapter >= 10 and synodal is False else ref.chapter
    try:
        rows = await passage.fetch_rows(connection, edition, ref.book, chapter, ref.first, ref.last)
    except UserError:
        return None
    name = await bible._book_name(connection, ref.book, edition['language_code'], edition['id'])
    label = f'{name} {chapter}' + (f':{ref.first}' + (f'–{ref.last}' if ref.last != ref.first else '') if ref.first else '')
    return rows_text(f'{header_prefix}<b>{escape(label)}</b>\n\n', rows, '', edition, locale, whole=ref.first is None)


async def show(connection: Any, chat: Any, edition: Any, code: str) -> tuple[Any, InlineKeyboardMarkup]:
    """Молитва по коду: заголовок, отрывки в издании чата, при необходимости — собственный текст; под ней кнопка возврата в список."""
    prayer = BY_CODE.get(code)
    if prayer is None:
        raise UserError('invalid')
    locale = chat['ui_language']
    code_lang = lang(locale)
    synodal = await uses_synodal_psalms(connection, edition)
    head = f"{prayer.emoji} <b>{escape(prayer.title[code_lang])}</b>\n<i>{ALOUD[code_lang]}</i>\n"
    if prayer.intro:
        head += f"{escape(prayer.intro[code_lang])}\n"
    head += '\n'
    pieces = []
    for index, ref in enumerate(prayer.refs):
        piece = await piece_for(connection, edition, ref, synodal, locale, head if index == 0 else '')
        if piece is not None:
            pieces.append(piece)
    if not pieces:
        raise UserError('no_result')
    footer = '\n\n' + bible.attribution(edition, locale)
    if prayer.text:
        body = '\n\n'.join(str(piece) for piece in pieces) + f"\n\n🙏 {escape(prayer.text[code_lang])}" + footer
        result: Any = body
    else:
        result = combine(pieces, footer=footer)
    back = InlineKeyboardMarkup(inline_keyboard=[[button('◀ Молитвослов' if code_lang == 'ru' else '◀ Prayer book', 'pb', chat['telegram_chat_id'], '')]])
    return result, back
