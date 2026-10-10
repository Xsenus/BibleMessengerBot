"""Menus, keyboards and status/settings text for Telegram handlers."""
from __future__ import annotations
import logging
from typing import Any
from aiogram.types import InlineKeyboardMarkup
from app.bot.ui import onboarding_help
from app.catalog.profiles import normalize_language_code
from app.services import bible
from app.services.destinations import configure_chat
from app.services.errors import UserError
from app.services.formatting import escape
from app.services.i18n import available_ui,native_ui_name,tr,ui_for_language
from app.services.locks import chat_lock
from app.services.scheduling import parse_hhmm
from app.services.subscriptions import create_or_update_subscription,list_subscriptions
from app.bot.common import button

LOGGER = logging.getLogger(__name__)

LOGGER = logging.getLogger(__name__)




def settings_keyboard(chat: Any) -> InlineKeyboardMarkup:
    """A language-independent action vocabulary with destination-localized labels."""
    locale,identifier = chat['ui_language'],chat['telegram_chat_id']
    rows = [[button(tr(locale,'language'),'langs',identifier),button(tr(locale,'ui_language'),'uilangs',identifier)],
        [button(tr(locale,'edition'),'editions',identifier,'0'),button(tr(locale,'mode'),'modes',identifier)],
        [button(tr(locale,'pause'),'pause',identifier),button(tr(locale,'resume'),'resume',identifier)],
        [button(tr(locale,'status'),'status',identifier),button(tr(locale,'time'),'timehelp',identifier)]]
    rows.insert(0,[button('🌍 Мой город / часовой пояс' if locale=='ru' else '🌍 My city / time zone','zones',identifier)])
    rows.append([button('🔔 Стих каждый день' if locale=='ru' else '🔔 Daily verse','daily',identifier)])
    rows.append([button('🌅 Утро и вечер' if locale=='ru' else '🌅 Morning and evening','devotions',identifier)])
    rows.append([button('🎙 Голос русского чтения' if locale=='ru' else '🎙 Russian narration voice','voices',identifier)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def voices_menu(chat):
    locale=chat['ui_language']
    chosen=chat.get('audio_voice','david')
    names={'david':'Давид' if locale=='ru' else 'David','mary':'Мария' if locale=='ru' else 'Mary'}
    text=('Выберите голос русского чтения. По умолчанию — Давид.' if locale=='ru' else 'Choose a Russian narration voice. David is the default.')
    rows=[[button(('✓ ' if key==chosen else '')+name,'voice',chat['telegram_chat_id'],key)] for key,name in names.items()]
    return text,InlineKeyboardMarkup(inline_keyboard=rows)


async def change_voice(connection,chat,value):
    if value not in {'david','mary'}:
        raise UserError('invalid')
    # Voice preferences do not change reading progress, schedule revisions or artwork.
    return await connection.fetchrow('UPDATE telegram_chats SET audio_voice=$2,updated_at=now() WHERE telegram_chat_id=$1 RETURNING *',chat['telegram_chat_id'],value)


def daily_confirmation(subscription: Any, locale: str) -> str:
    clock = subscription['send_time'].strftime('%H:%M')
    zone = escape(subscription['timezone'])
    if locale=='ru':
        return f'🔔 <b>Ежедневный стих включён</b>\nКаждый день в <b>{clock}</b> · {zone}\n\nИзменить время: /time · Отключить: /daily off'
    return f'🔔 <b>Daily verse enabled</b>\nEvery day at <b>{clock}</b> · {zone}\n\nChange schedule: /time · Turn off: /daily off'


async def enable_devotions(connection: Any,chat: Any,actor_id:int) -> str:
    edition=await bible.chat_translation(connection,chat['telegram_chat_id'])
    if not edition:raise UserError('not_ready')
    async with chat_lock(connection,chat['telegram_chat_id']),connection.transaction():
        for mode,clock in [('morning_verse','09:38'),('evening_verse','21:13')]:
            await create_or_update_subscription(connection,chat_id=chat['telegram_chat_id'],created_by=actor_id,
                translation_id=edition['id'],mode=mode,send_time=parse_hhmm(clock),timezone_name=chat['timezone'])
    if chat['ui_language']=='ru':
        return f"🌅 <b>Утро и вечер включены</b>\nЧтение: 09:00 и 21:00\n🕊 Молитва: 09:38 и 21:13 · {escape(chat['timezone'])}\nКартинка и все доступные озвучки готовятся заранее.\nОтключить: /devotions off"
    return f"🌅 <b>Morning and evening enabled</b>\nReading: 09:00 and 21:00\n🕊 Prayer: 09:38 and 21:13 · {escape(chat['timezone'])}\nArtwork and all available narrations are prepared in advance.\nTurn off: /devotions off"


async def settings_text(connection: Any, chat: Any) -> str:
    """Display exactly this destination's saved edition, locale and timezone."""
    locale = chat['ui_language']
    edition = await bible.chat_translation(connection,chat['telegram_chat_id'])
    display_id = chat['telegram_chat_id']
    if chat.get('platform','telegram') == 'max':
        from app.maxbot.identities import external
        display_id = await external(connection,'chat',display_id)
    lines = [f"<b>{tr(locale,'settings')}</b>",f"{tr(locale,'destination')}: <code>{display_id}</code>",
        f"{tr(locale,'ui_language')}: {escape(native_ui_name(locale))}",
        f"{tr(locale,'language')}: {escape(bible.display_language(edition['language_code'],locale) if edition else '—')}",
        f"{tr(locale,'edition')}: {escape(bible.display_title(edition) if edition else '—')}",
        f"{tr(locale,'timezone')}: <code>{escape(chat['timezone'])}</code>"]
    if edition:
        coverage = edition['coverage'] if edition['coverage'] in {'full','nt','ot','partial'} else 'unverified'
        lines += [f"{tr(locale,'coverage')}: {tr(locale,coverage)}",f"{tr(locale,'books')}: {edition['book_count']} · {tr(locale,'verses')}: {edition['nonempty_verse_count']}"]
    else:
        lines.append(tr(locale,'not_ready'))
    return '\n'.join(lines)


async def command_target_suffix(connection: Any, chat: Any) -> str:
    """Expose copyable destination syntax, never a MAX internal database key."""
    if chat.get('platform','telegram') == 'max':
        if chat['chat_type']=='private':
            return ''
        from app.maxbot.identities import external
        return f" chat:{await external(connection,'chat',chat['telegram_chat_id'])}"
    return f" {chat['telegram_chat_id']}" if chat['telegram_chat_id'] < 0 else ''


async def status_text(connection: Any, chat: Any) -> str:
    """Show progress, queue IDs and ambiguous chunks without exposing private payloads."""
    locale = chat['ui_language']
    lines = [await settings_text(connection,chat),'']
    subs = await list_subscriptions(connection,chat['telegram_chat_id'])
    for sub in subs:
        state = tr(locale,'complete') if sub['completed'] else tr(locale,'enabled' if sub['is_enabled'] else 'disabled')
        lines += [f"<b>{tr(locale,sub['mode'])}</b>: {state}",
            f"{sub['send_time'].strftime('%H:%M')} {escape(sub['timezone'])} · {escape(bible.display_title({'title':sub['translation_title'],'source_translation_id':sub['source_translation_id'],'source_name':sub.get('translation_source_name')}))}",
            f"{tr(locale,'progress')}: {escape(sub['current_book_code'] or '—')} {sub['current_chapter'] or 0} · {sub['plan_day']}"]
        if sub['mode'] in {'morning_verse','evening_verse'}:
            hour=sub['send_time'].replace(minute=0).strftime('%H:%M')
            lines.append(('Чтение: ' if locale=='ru' else 'Reading: ')+hour+(' · указанное выше время — молитва' if locale=='ru' else ' · time above is prayer'))
    if not subs:
        lines.append(tr(locale,'no_subscriptions'))
    jobs = await connection.fetch("SELECT id,status,next_chunk,jsonb_array_length(chunks) AS total,telegram_message_ids FROM delivery_log WHERE telegram_chat_id=$1 AND status IN ('pending','retry','sending','uncertain','failed') ORDER BY id DESC LIMIT 10",chat['telegram_chat_id'])
    if jobs:
        lines.append('')
    for job in jobs:
        # Machine state identifiers intentionally remain stable for diagnostics.
        lines.append(f"<code>#{job['id']} {job['status']} {job['next_chunk']}/{job['total']}</code>")
        if job['status'] in {'uncertain','failed'}:
            target = await command_target_suffix(connection,chat)
            lines += [tr(locale,'pending_review'),f"<code>/resolve {job['id']} sent{target}</code>",
                f"<code>/resolve {job['id']} retry-duplicate-risk{target}</code>",
                f"<code>/resolve {job['id']} cancel{target}</code>"]
    preparations=await connection.fetch("""SELECT r.state,count(*) AS count FROM scheduled_readings r JOIN delivery_log d ON d.id=r.delivery_id
        WHERE d.telegram_chat_id=$1 AND d.status IN ('pending','retry') GROUP BY r.state""",chat['telegram_chat_id'])
    for prepared in preparations:
        lines.append(('Подготовка чтений: ' if locale=='ru' else 'Reading preparation: ')+f"{prepared['state']} · {prepared['count']}")
    return '\n'.join(lines)


def help_text(locale: str) -> str:
    """Localized descriptions plus stable copyable command syntax."""
    friendly = onboarding_help(locale)
    if friendly:
        return friendly
    lines = [f"<b>{tr(locale,'help')}</b>"]
    for command,key in [('settings','settings'),('today','today'),('next','next'),('random','random'),('topics','topics'),
        ('translations','edition'),('status','status'),('pause','pause'),('resume','resume'),('unsubscribe','unsubscribe')]:
        lines.append(f'/{command} — {tr(locale,key)}')
    lines += ['',f"{tr(locale,'language')}: <code>/language ru</code>",
        f"{tr(locale,'ui_language')}: <code>/ui en</code>",
        f"{tr(locale,'edition')}: <code>/translation russyn</code>",
        f"{tr(locale,'time')}: <code>/time 09:00 Europe/Moscow</code>",
        '<code>/subscribe sequential 09:00 Europe/Moscow</code>',
        '<code>/subscribe reading_plan 09:00 Europe/Moscow bible-365</code>',
        f"{tr(locale,'search')}: <code>/search …</code>",
        f"{tr(locale,'destination')}: <code>/settings @channel</code>",
        '<code>/language en @channel</code>',
        '<code>/subscribe verse 09:00 Europe/Amsterdam @channel</code>',
        '<code>/thread 123 @group</code> · <code>/thread off @group</code>',
        f"{tr(locale,'reset')}: <code>/reset confirm</code>",
        f"{tr(locale,'license')}: /license"]
    return '\n'.join(lines)


async def change_language(connection: Any, chat: Any, actor_id: int, language: str) -> Any:
    """Choose an imported edition in the requested language, without cross-language fallback."""
    edition = await bible.find_translation(connection,None,preferred_language=language)
    if not edition:
        raise UserError('unknown_language')
    return await configure_chat(connection,chat_id=chat['telegram_chat_id'],actor_id=actor_id,
        ui_language=ui_for_language(language),translation_id=edition['id'])




async def language_menu(connection: Any, chat: Any, page: int=0) -> tuple[str,Any]:
    """Page through ALL actually imported languages, including the all-open corpus."""
    editions = await bible.list_translations(connection)
    codes = sorted({e.language_code for e in editions})
    page=max(0,min(page,max((len(codes)-1)//36,0)))
    chosen=codes[page*36:page*36+36]
    rows = [[button(bible.display_language(code,chat['ui_language']),'lang',chat['telegram_chat_id'],code) for code in chosen[i:i+3]] for i in range(0,len(chosen),3)]
    navigation=[]
    for delta,key in [(-1,'previous_page'),(1,'next_page')]:
        if page+delta >= 0 and (page+delta)*36<len(codes):
            navigation.append(button(tr(chat['ui_language'],key),'langs',chat['telegram_chat_id'],str(page+delta)))
    if navigation:rows.append(navigation)
    rows.append([button(tr(chat['ui_language'],'back'),'settings',chat['telegram_chat_id'])])
    return tr(chat['ui_language'],'language')+f' · {page+1}/{max((len(codes)+35)//36,1)}\n<code>/language ISO</code>',InlineKeyboardMarkup(inline_keyboard=rows)


def ui_menu(chat: Any) -> tuple[str,Any]:
    """All advertised UI choices have complete bundled message-key sets."""
    pairs = list(available_ui().items())
    rows = [[button(name,'ui',chat['telegram_chat_id'],code) for code,name in pairs[i:i+2]] for i in range(0,len(pairs),2)]
    return tr(chat['ui_language'],'ui_language'),InlineKeyboardMarkup(inline_keyboard=rows)


async def edition_menu(connection: Any, chat: Any, page: int, language: str | None = None) -> tuple[str,Any]:
    """Show physical coverage for each edition; selections use immutable numeric DB ids."""
    editions = await bible.list_translations(connection)
    language = normalize_language_code(language)
    if language:
        editions = [e for e in editions if e.language_code==language]
    page = max(0,min(page,max((len(editions)-1)//8,0)))
    chosen = editions[page*8:page*8+8]
    locale,identifier = chat['ui_language'],chat['telegram_chat_id']
    lines = [tr(locale,'edition')]
    rows = []
    for item in chosen:
        coverage = item.coverage if item.coverage in {'full','nt','ot','partial'} else 'unverified'
        lines.append(f"<b>{escape(item.title)}</b>\n{escape(bible.display_language(item.language_code,locale))} · {tr(locale,coverage)} · {tr(locale,'books')}: {item.books}\n<code>/translation {escape(item.source_id)}</code>")
        rows.append([button(item.short_title[:50],'edition',identifier,str(item.id))])
    # Filter state is encoded explicitly, so next-page clicks cannot accidentally change language scope.
    nav = []
    for delta,key in [(-1,'previous_page'),(1,'next_page')]:
        if page+delta >= 0 and (page+delta)*8<len(editions):
            nav.append(button(tr(locale,key),'editions',identifier,f'{page+delta},{language or ""}'))
    if nav:
        rows.append(nav)
    rows.append([button(tr(locale,'back'),'settings',identifier)])
    return '\n\n'.join(lines) if chosen else tr(locale,'not_ready'),InlineKeyboardMarkup(inline_keyboard=rows)


def modes_menu(chat: Any) -> tuple[str,Any]:
    """One-click daily modes plus an explicit plan selection submenu."""
    locale,identifier = chat['ui_language'],chat['telegram_chat_id']
    rows = [[button(tr(locale,key),'mode',identifier,key)] for key in ['sequential','verse_of_day','topic_of_day']]
    rows.append([button(tr(locale,'reading_plan'),'plans',identifier)])
    return tr(locale,'mode'),InlineKeyboardMarkup(inline_keyboard=rows)


