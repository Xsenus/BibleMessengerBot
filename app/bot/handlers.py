"""Localized Telegram administration with authorization on every command/callback.

Groups and channels never inherit the latest administrator's language. Remote
management uses a private dialog with the bot, and permissions are rechecked.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo
from aiogram import F,Router
from aiogram.types import Message,CallbackQuery,InlineKeyboardMarkup,InlineKeyboardButton,ChatMemberUpdated
from aiogram.exceptions import TelegramAPIError
from app.bot.commands import parse_command,mode_name,encode_callback,decode_callback
from app.bot.transport import TelegramSender
from app.catalog.profiles import normalize_language_code
from app.services import bible
from app.services.accounts import upsert_user,upsert_chat,claim_owner
from app.services.destinations import configure_chat,ensure_resolved,cancel_queued
from app.services.errors import UserError,SendError
from app.services.formatting import escape,split_message
from app.services.i18n import available_ui,initial_ui,native_ui_name,tr,ui_for_language
from app.services.locks import chat_lock
from app.services.scheduling import parse_hhmm,next_occurrence,validate_timezone
from app.services.subscriptions import create_or_update_subscription,set_enabled,delete_subscriptions,list_subscriptions
from app.worker.delivery import enqueue_next,resolve_uncertain

router = Router(name='localized-destinations')
LOGGER = logging.getLogger(__name__)


def enum_value(value: Any) -> str:
    """Work with both aiogram string enums and validated plain strings."""
    return str(getattr(value,'value',value))


async def authorize(bot: Any, chat: Any, actor_id: int) -> None:
    """Verify the real user and the bot's current posting rights; fail closed."""
    if enum_value(chat.type)=='private':
        if chat.id != actor_id:
            raise UserError('forbidden')
        return
    try:
        actor = await bot.get_chat_member(chat.id,actor_id)
        me = await bot.get_me()
        member = await bot.get_chat_member(chat.id,me.id)
    except TelegramAPIError:
        raise UserError('forbidden') from None
    if enum_value(actor.status) not in {'creator','administrator'}:
        raise UserError('forbidden')
    # Administrator status is required to reliably verify other users' membership.
    if enum_value(member.status) not in {'creator','administrator'}:
        raise UserError('forbidden')
    if enum_value(chat.type)=='channel' and not getattr(member,'can_post_messages',False):
        raise UserError('forbidden')


async def register_context(connection: Any, user: Any, chat: Any, settings: Any) -> None:
    """Persist a private user's initial UI once; group defaults are deterministic."""
    await upsert_user(connection,user_id=user.id,username=user.username,first_name=user.first_name,
        last_name=user.last_name,language_code=user.language_code,default_timezone=settings.default_timezone)
    if await connection.fetchval('SELECT is_blocked FROM telegram_users WHERE telegram_user_id=$1',user.id):
        raise UserError('forbidden')
    await upsert_chat(connection,chat_id=chat.id,chat_type=enum_value(chat.type),title=chat.title,
        username=chat.username,registered_by=user.id,default_timezone=settings.default_timezone,
        ui_language=initial_ui(user.language_code) if enum_value(chat.type)=='private' else 'ru')


async def destination(connection: Any, bot: Any, current_chat: Any, actor_id: int,
                      target: str | int | None, settings: Any) -> Any:
    """Resolve remote destinations only from a private management dialog."""
    if target is not None and enum_value(current_chat.type)!='private' and str(target)!=str(current_chat.id):
        raise UserError('private_only')
    try:
        chat = await bot.get_chat(int(target) if str(target).lstrip('-').isdigit() else target) if target is not None else current_chat
    except TelegramAPIError:
        raise UserError('no_result') from None
    await authorize(bot,chat,actor_id)
    await upsert_chat(connection,chat_id=chat.id,chat_type=enum_value(chat.type),title=chat.title,
        username=chat.username,registered_by=actor_id,default_timezone=settings.default_timezone,
        ui_language='ru' if enum_value(chat.type)!='private' else 'en')
    await connection.execute('UPDATE telegram_chats SET is_active=true WHERE telegram_chat_id=$1',chat.id)
    return await connection.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=$1',chat.id)


async def reply(bot: Any, connection: Any, settings: Any, chat_id: int, text: str,
                markup: Any = None, thread: int | None = None) -> None:
    """Split HTML safely and never automatically replay an ambiguous UI response."""
    parts = split_message(text,settings.max_message_length)
    sender = TelegramSender(bot,connection,settings)
    for index,part in enumerate(parts):
        try:
            await sender.send(chat_id,part,thread,reply_markup=markup if index==len(parts)-1 else None)
        except SendError as error:
            LOGGER.warning('UI response to %s stopped: %s',chat_id,error.kind)
            return


def button(text: str, action: str, chat_id: int, value: str = '') -> InlineKeyboardButton:
    """All callbacks carry the destination, which is authorized again when clicked."""
    return InlineKeyboardButton(text=text,callback_data=encode_callback(action,chat_id,value))


def settings_keyboard(chat: Any) -> InlineKeyboardMarkup:
    """A language-independent action vocabulary with destination-localized labels."""
    locale,identifier = chat['ui_language'],chat['telegram_chat_id']
    rows = [[button(tr(locale,'language'),'langs',identifier),button(tr(locale,'ui_language'),'uilangs',identifier)],
        [button(tr(locale,'edition'),'editions',identifier,'0'),button(tr(locale,'mode'),'modes',identifier)],
        [button(tr(locale,'pause'),'pause',identifier),button(tr(locale,'resume'),'resume',identifier)],
        [button(tr(locale,'status'),'status',identifier),button(tr(locale,'time'),'timehelp',identifier)]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def settings_text(connection: Any, chat: Any) -> str:
    """Display exactly this destination's saved edition, locale and timezone."""
    locale = chat['ui_language']
    edition = await bible.chat_translation(connection,chat['telegram_chat_id'])
    lines = [f"<b>{tr(locale,'settings')}</b>",f"{tr(locale,'destination')}: <code>{chat['telegram_chat_id']}</code>",
        f"{tr(locale,'ui_language')}: {escape(native_ui_name(locale))}",
        f"{tr(locale,'language')}: {escape(edition['language_code'] if edition else '—')}",
        f"{tr(locale,'edition')}: {escape(edition['title'] if edition else '—')}",
        f"{tr(locale,'timezone')}: <code>{escape(chat['timezone'])}</code>"]
    if edition:
        coverage = edition['coverage'] if edition['coverage'] in {'full','nt','ot','partial'} else 'unverified'
        lines += [f"{tr(locale,'coverage')}: {tr(locale,coverage)}",f"{tr(locale,'books')}: {edition['book_count']} · {tr(locale,'verses')}: {edition['nonempty_verse_count']}"]
    else:
        lines.append(tr(locale,'not_ready'))
    return '\n'.join(lines)


async def status_text(connection: Any, chat: Any) -> str:
    """Show progress, queue IDs and ambiguous chunks without exposing private payloads."""
    locale = chat['ui_language']
    lines = [await settings_text(connection,chat),'']
    subs = await list_subscriptions(connection,chat['telegram_chat_id'])
    for sub in subs:
        state = tr(locale,'complete') if sub['completed'] else tr(locale,'enabled' if sub['is_enabled'] else 'disabled')
        lines += [f"<b>{tr(locale,sub['mode'])}</b>: {state}",
            f"{sub['send_time'].strftime('%H:%M')} {escape(sub['timezone'])} · {escape(sub['source_translation_id'])}",
            f"{tr(locale,'progress')}: {escape(sub['current_book_code'] or '—')} {sub['current_chapter'] or 0} · {sub['plan_day']}"]
    if not subs:
        lines.append(tr(locale,'no_subscriptions'))
    jobs = await connection.fetch("SELECT id,status,next_chunk,jsonb_array_length(chunks) AS total,telegram_message_ids FROM delivery_log WHERE telegram_chat_id=$1 AND status IN ('pending','retry','sending','uncertain','failed') ORDER BY id DESC LIMIT 10",chat['telegram_chat_id'])
    if jobs:
        lines.append('')
    for job in jobs:
        # Machine state identifiers intentionally remain stable for diagnostics.
        lines.append(f"<code>#{job['id']} {job['status']} {job['next_chunk']}/{job['total']}</code>")
        if job['status'] in {'uncertain','failed'}:
            lines += [tr(locale,'pending_review'),f"<code>/resolve {job['id']} sent {chat['telegram_chat_id']}</code>",
                f"<code>/resolve {job['id']} retry-duplicate-risk {chat['telegram_chat_id']}</code>",
                f"<code>/resolve {job['id']} cancel {chat['telegram_chat_id']}</code>"]
    return '\n'.join(lines)


def help_text(locale: str) -> str:
    """Localized descriptions plus stable copyable command syntax."""
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


async def run_command(connection: Any, bot: Any, settings: Any, message: Message,
                      parsed: Any) -> tuple[str,Any]:
    """Domain routing; every publishing/settings action resolves and authorizes a destination."""
    user = message.from_user
    args = list(parsed.arguments)
    name = parsed.name
    chat = await destination(connection,bot,message.chat,user.id,parsed.target,settings)
    locale = chat['ui_language']
    if name in {'start','settings','register'}:
        return ((tr(locale,'start')+'\n\n') if name=='start' else '')+await settings_text(connection,chat),settings_keyboard(chat)
    if name=='help':
        return help_text(locale),None
    if name=='claim':
        if enum_value(message.chat.type)!='private':
            raise UserError('private_only')
        ok = len(args)==1 and await claim_owner(connection,telegram_user_id=user.id,supplied_code=args[0],expected_code=settings.owner_claim_code)
        return tr(locale,'owner_ok' if ok else 'owner_fail'),None
    if name=='language':
        if not args:
            return await language_menu(connection,chat)
        if len(args)!=1:
            raise UserError('invalid')
        chat = await change_language(connection,chat,user.id,args[0])
        notice = '' if ui_for_language(args[0]) else tr(chat['ui_language'],'ui_fallback',ui=native_ui_name(chat['ui_language']))+'\n'
        return notice+await settings_text(connection,chat),settings_keyboard(chat)
    if name=='ui':
        if not args:
            return ui_menu(chat)
        locale_new = ui_for_language(args[0]) if len(args)==1 else None
        if not locale_new:
            raise UserError('unknown_language')
        chat = await configure_chat(connection,chat_id=chat['telegram_chat_id'],actor_id=user.id,ui_language=locale_new)
        return await settings_text(connection,chat),settings_keyboard(chat)
    if name=='translations':
        return await edition_menu(connection,chat,0,args[0] if args else None)
    if name=='translation':
        if len(args)!=1:
            raise UserError('invalid')
        edition = await bible.find_translation(connection,args[0])
        if not edition:
            raise UserError('no_result')
        chat = await configure_chat(connection,chat_id=chat['telegram_chat_id'],actor_id=user.id,translation_id=edition['id'])
        return await settings_text(connection,chat),settings_keyboard(chat)
    if name=='status':
        return await status_text(connection,chat),settings_keyboard(chat)
    if name in {'pause','resume','unsubscribe'}:
        if len(args)>1:
            raise UserError('invalid')
        mode = mode_name(args[0]) if args else None
        if name=='unsubscribe':
            await delete_subscriptions(connection,chat['telegram_chat_id'],mode)
        else:
            await set_enabled(connection,chat['telegram_chat_id'],name=='resume',mode)
        return tr(locale,'saved'),settings_keyboard(chat)
    if name=='thread':
        if len(args)!=1 or not (args[0]=='off' or args[0].isdigit() and int(args[0])>0):
            raise UserError('invalid')
        livechat = await bot.get_chat(chat['telegram_chat_id'])
        if args[0]!='off' and not getattr(livechat,'is_forum',False):
            raise UserError('invalid')
        chat = await configure_chat(connection,chat_id=chat['telegram_chat_id'],actor_id=user.id,set_thread=True,
            message_thread_id=None if args[0]=='off' else int(args[0]))
        return tr(locale,'saved'),settings_keyboard(chat)
    if name=='time':
        if not 1<=len(args)<=2:
            raise UserError('invalid')
        clock,tz = parse_hhmm(args[0]),args[1] if len(args)==2 else chat['timezone']
        validate_timezone(tz)
        async with chat_lock(connection,chat['telegram_chat_id']),connection.transaction():
            chat = await configure_chat(connection,chat_id=chat['telegram_chat_id'],actor_id=user.id,timezone_name=tz)
            rows = await list_subscriptions(connection,chat['telegram_chat_id'])
            for row in rows:
                await connection.execute('UPDATE subscriptions SET send_time=$2,next_run_at=$3,revision=revision+1 WHERE id=$1',row['id'],clock,
                    next_occurrence(clock,tz,list(row['days_of_week'])) if row['is_enabled'] else None)
        return tr(locale,'saved'),settings_keyboard(chat)
    if name in {'subscribe','channel'}:
        if not args:
            return modes_menu(chat)
        if not 1<=len(args)<=4:
            raise UserError('invalid')
        mode = mode_name(args[0])
        edition = await bible.chat_translation(connection,chat['telegram_chat_id'])
        if not edition:
            raise UserError('not_ready')
        await create_or_update_subscription(connection,chat_id=chat['telegram_chat_id'],created_by=user.id,
            translation_id=edition['id'],mode=mode,send_time=parse_hhmm(args[1] if len(args)>1 else settings.default_send_time),
            timezone_name=args[2] if len(args)>2 else chat['timezone'],plan_code=args[3] if len(args)>3 else None)
        return tr(locale,'saved'),settings_keyboard(chat)
    if name=='reset':
        if args!=['confirm']:
            raise UserError('invalid')
        async with chat_lock(connection,chat['telegram_chat_id']),connection.transaction():
            await ensure_resolved(connection,chat['telegram_chat_id'])
            await cancel_queued(connection,chat['telegram_chat_id'])
            await connection.execute('DELETE FROM chat_reading_progress WHERE telegram_chat_id=$1',chat['telegram_chat_id'])
            await connection.execute('''UPDATE subscriptions SET current_book_code=NULL,current_chapter=NULL,current_verse=NULL,
                plan_day=0,completed=false,is_enabled=false,next_run_at=NULL,revision=revision+1 WHERE telegram_chat_id=$1''',chat['telegram_chat_id'])
            await connection.execute("INSERT INTO operator_events(actor_id,chat_id,action) VALUES($1,$2,'reset_progress')",user.id,chat['telegram_chat_id'])
        return tr(locale,'saved')+'\n/resume',settings_keyboard(chat)
    if name=='resolve':
        if len(args)!=2 or not args[0].isdigit():
            raise UserError('invalid')
        await resolve_uncertain(connection,int(args[0]),chat['telegram_chat_id'],user.id,args[1])
        return tr(locale,'saved'),settings_keyboard(chat)
    if name=='topics':
        topics = await connection.fetch('SELECT code,title_ru,title_en FROM topics WHERE is_active ORDER BY code')
        # Codes are stable identifiers; avoid presenting untranslated prose as a localized interface.
        return tr(locale,'topics')+'\n'+'\n'.join(f"<code>/topic {t['code']}</code>" for t in topics),None
    edition = await bible.chat_translation(connection,chat['telegram_chat_id'])
    if not edition:
        raise UserError('not_ready')
    if name=='license':
        return bible.attribution(edition,locale),None
    if name=='next':
        identifier = await enqueue_next(connection,chat,edition,f'{message.chat.id}:{message.message_id}',settings.max_message_length)
        return tr(locale,'queued')+f' <code>#{identifier}</code>',None
    local_date = datetime.now(ZoneInfo(chat['timezone'])).date()
    row = None
    if name=='today':
        row = await bible.verse_of_day(connection,edition,str(chat['telegram_chat_id']),local_date)
    elif name=='random':
        row = await bible.random_verse(connection,edition)
    elif name=='topic':
        result = await bible.topic_verse(connection,edition,args[0] if args else None,str(chat['telegram_chat_id']),local_date)
        row = result[1] if result else None
    elif name=='search':
        rows = await bible.search_verses(connection,edition['id'],' '.join(args),5)
        return '\n\n'.join([await bible.render_verse(connection,r,edition,ui_language=locale) for r in rows]) or tr(locale,'no_result'),None
    else:
        raise UserError('invalid')
    return await bible.render_verse(connection,row,edition,ui_language=locale) if row else tr(locale,'no_result'),None


async def language_menu(connection: Any, chat: Any, page: int=0) -> tuple[str,Any]:
    """Page through ALL actually imported languages, including the all-open corpus."""
    editions = await bible.list_translations(connection)
    codes = sorted({e.language_code for e in editions})
    page=max(0,min(page,max((len(codes)-1)//36,0)))
    chosen=codes[page*36:page*36+36]
    rows = [[button(code,'lang',chat['telegram_chat_id'],code) for code in chosen[i:i+3]] for i in range(0,len(chosen),3)]
    navigation=[]
    for delta,key in [(-1,'previous_page'),(1,'next_page')]:
        if 0<=page+delta and (page+delta)*36<len(codes):
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
        lines.append(f"<b>{escape(item.language_code)} · {escape(item.title)}</b>\n<code>{escape(item.source_id)}</code> · {tr(locale,coverage)} · {item.books}")
        rows.append([button(f'{item.language_code} · {item.short_title}'[:50],'edition',identifier,str(item.id))])
    # Filter state is encoded explicitly, so next-page clicks cannot accidentally change language scope.
    nav = []
    for delta,key in [(-1,'previous_page'),(1,'next_page')]:
        if 0<=page+delta and (page+delta)*8<len(editions):
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


@router.message(F.text.startswith('/'))
async def command_handler(message: Message,bot: Any,db_pool: Any,settings: Any) -> None:
    """Anonymous administrators must use private chat under their real identity."""
    if not message.from_user or message.from_user.is_bot or message.sender_chat:
        return
    async with db_pool.acquire() as connection:
        locale = initial_ui(message.from_user.language_code)
        try:
            parsed = parse_command(message.text or '')
            if parsed.mentioned_bot:
                me = await bot.get_me()
                if parsed.mentioned_bot.lower()!=(me.username or '').lower():
                    return
            await register_context(connection,message.from_user,message.chat,settings)
            locale = await connection.fetchval('SELECT ui_language FROM telegram_chats WHERE telegram_chat_id=$1',message.chat.id)
            text,markup = await run_command(connection,bot,settings,message,parsed)
        except UserError as error:
            text,markup = tr(locale,error.key),None
        except (ValueError,KeyError):
            text,markup = tr(locale,'invalid'),None
        except TelegramAPIError:
            text,markup = tr(locale,'forbidden'),None
        except Exception as error:
            LOGGER.error('Command failed: %s',type(error).__name__)
            text,markup = tr(locale,'not_ready'),None
        await reply(bot,connection,settings,message.chat.id,text,markup,message.message_thread_id)


@router.callback_query(F.data.startswith('v1:'))
async def callback_handler(callback: CallbackQuery,bot: Any,db_pool: Any,settings: Any) -> None:
    """Callbacks are stateless and reauthorize the actor for the encoded destination."""
    if not callback.message or not isinstance(callback.message,Message):
        await callback.answer()
        return
    await callback.answer()  # Stop the spinner before potentially slow permission/network checks.
    async with db_pool.acquire() as connection:
        locale = initial_ui(callback.from_user.language_code)
        try:
            action,chat_id,value = decode_callback(callback.data or '')
            await register_context(connection,callback.from_user,callback.message.chat,settings)
            chat = await destination(connection,bot,callback.message.chat,callback.from_user.id,chat_id,settings)
            locale = chat['ui_language']
            if action=='lang':
                chat = await change_language(connection,chat,callback.from_user.id,value)
                text,markup = await settings_text(connection,chat),settings_keyboard(chat)
                if not ui_for_language(value):
                    text = tr(chat['ui_language'],'ui_fallback',ui=native_ui_name(chat['ui_language']))+'\n'+text
            elif action=='ui':
                chat = await configure_chat(connection,chat_id=chat_id,actor_id=callback.from_user.id,ui_language=value)
                text,markup = await settings_text(connection,chat),settings_keyboard(chat)
            elif action=='edition':
                chat = await configure_chat(connection,chat_id=chat_id,actor_id=callback.from_user.id,translation_id=int(value))
                text,markup = await settings_text(connection,chat),settings_keyboard(chat)
            elif action in {'pause','resume'}:
                await set_enabled(connection,chat_id,action=='resume')
                text,markup = tr(locale,'saved'),settings_keyboard(chat)
            elif action in {'mode','plan'}:
                edition = await bible.chat_translation(connection,chat_id)
                if not edition:
                    raise UserError('not_ready')
                await create_or_update_subscription(connection,chat_id=chat_id,created_by=callback.from_user.id,
                    translation_id=edition['id'],mode=mode_name(value) if action=='mode' else 'reading_plan',
                    send_time=parse_hhmm(settings.default_send_time),timezone_name=chat['timezone'],
                    plan_code=value if action=='plan' else None)
                text,markup = tr(locale,'saved'),settings_keyboard(chat)
            elif action=='plans':
                from app.services.plans import PLANS
                text = tr(locale,'plan')
                markup = InlineKeyboardMarkup(inline_keyboard=[[button(code,'plan',chat_id,code)] for code in PLANS])
            elif action=='langs':
                text,markup = await language_menu(connection,chat,int(value or '0'))
            elif action=='uilangs':
                text,markup = ui_menu(chat)
            elif action=='editions':
                page,_,language = value.partition(',')
                text,markup = await edition_menu(connection,chat,int(page),language or None)
            elif action=='modes':
                text,markup = modes_menu(chat)
            elif action=='status':
                text,markup = await status_text(connection,chat),settings_keyboard(chat)
            elif action=='timehelp':
                suffix = f" {chat_id}" if chat_id<0 else ''
                text,markup = f"{tr(locale,'time')}: <code>/time 09:00 {escape(chat['timezone'])}{suffix}</code>",None
            elif action=='settings':
                text,markup = await settings_text(connection,chat),settings_keyboard(chat)
            else:
                raise UserError('invalid')
        except UserError as error:
            text,markup = tr(locale,error.key),None
        except (ValueError,KeyError):
            text,markup = tr(locale,'invalid'),None
        except TelegramAPIError:
            text,markup = tr(locale,'forbidden'),None
        except Exception as error:
            LOGGER.error('Callback failed: %s',type(error).__name__)
            text,markup = tr(locale,'not_ready'),None
        await reply(bot,connection,settings,callback.message.chat.id,text,markup,callback.message.message_thread_id)


@router.my_chat_member()
async def membership_handler(event: ChatMemberUpdated,db_pool: Any) -> None:
    """Stop delivery when removed or posting rights are revoked, without deleting progress."""
    status = enum_value(event.new_chat_member.status)
    active = status in {'administrator','creator'}
    if enum_value(event.chat.type)=='private':
        active = status=='member'
    if enum_value(event.chat.type)=='channel':
        active = active and getattr(event.new_chat_member,'can_post_messages',False)
    async with db_pool.acquire() as connection,chat_lock(connection,event.chat.id),connection.transaction():
        await connection.execute('UPDATE telegram_chats SET is_active=$2 WHERE telegram_chat_id=$1',event.chat.id,active)
        if not active:
            await connection.execute('UPDATE subscriptions SET is_enabled=false,next_run_at=NULL WHERE telegram_chat_id=$1',event.chat.id)


@router.message(F.migrate_to_chat_id)
async def migration_notice(message: Message,db_pool: Any) -> None:
    """Fail closed on Telegram group-ID migration; preserve history for explicit relinking."""
    async with db_pool.acquire() as connection,chat_lock(connection,message.chat.id),connection.transaction():
        await connection.execute('UPDATE telegram_chats SET is_active=false WHERE telegram_chat_id=$1',message.chat.id)
        await connection.execute('UPDATE subscriptions SET is_enabled=false,next_run_at=NULL WHERE telegram_chat_id=$1',message.chat.id)
        await connection.execute("INSERT INTO operator_events(chat_id,action,details) VALUES($1,'group_id_migrated',jsonb_build_object('new_chat_id',$2::bigint))",message.chat.id,message.migrate_to_chat_id)
