"""Command execution: parses a command and performs the requested action."""
from __future__ import annotations
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo
from aiogram.types import Message,ReplyKeyboardRemove
from app.bot.commands import mode_name
from app.bot.ui import main_keyboard,welcome_text,menu_hint,search_prompt
from app.services import bible
from app.services import passage,illustrations,readings
from app.services.accounts import claim_owner
from app.services.destinations import configure_chat,ensure_resolved,cancel_queued
from app.services.errors import UserError
from app.services.formatting import escape
from app.services.i18n import native_ui_name,tr,ui_for_language
from app.services.locks import chat_lock
from app.services.scheduling import parse_hhmm,next_reading,validate_timezone
from app.services.subscriptions import create_or_update_subscription,set_enabled,delete_subscriptions,list_subscriptions
from app.worker.delivery import enqueue_next,resolve_uncertain
from app.bot.common import destination,enum_value
from app.bot.menus import (change_language,change_voice,command_target_suffix,daily_confirmation,edition_menu,
    enable_devotions,help_text,language_menu,modes_menu,settings_keyboard,settings_text,status_text,ui_menu,voices_menu)

LOGGER = logging.getLogger(__name__)

LOGGER = logging.getLogger(__name__)




async def run_command(connection: Any, bot: Any, settings: Any, message: Message,
                      parsed: Any) -> tuple[str | None,Any]:
    """Domain routing; every publishing/settings action resolves and authorizes a destination."""
    user = message.from_user
    args = list(parsed.arguments)
    name = parsed.name
    chat = await destination(connection,bot,message.chat,user.id,parsed.target,settings)
    locale = chat['ui_language']
    if name=='voice':
        if not args:
            return voices_menu(chat)
        aliases={'давид':'david','мария':'mary','aidar':'david','kseniya':'mary'}
        if len(args)!=1:
            raise UserError('invalid')
        value=aliases.get(args[0].casefold(),args[0].casefold())
        chat=await change_voice(connection,chat,value)
        return voices_menu(chat)
    if name=='hide_keyboard':
        if enum_value(message.chat.type)!='private' or getattr(bot,'platform',None)=='max':
            raise UserError('invalid')
        await connection.execute('UPDATE telegram_chats SET keyboard_hidden=true WHERE telegram_chat_id=$1',message.chat.id)
        return ('Кнопки скрыты. Вернуть их: /menu. Команды доступны через кнопку «Меню».' if locale=='ru' else
                'Buttons hidden. Restore them with /menu. Commands remain available in Menu.'),ReplyKeyboardRemove()
    if name=='start' and enum_value(message.chat.type)=='private':
        if getattr(bot,'platform',None)!='max' and connection is not None:
            await connection.execute('UPDATE telegram_chats SET keyboard_hidden=false WHERE telegram_chat_id=$1',message.chat.id)
        edition = await bible.chat_translation(connection,chat['telegram_chat_id'])
        return welcome_text(locale,bible.display_title(edition) if edition else None),main_keyboard(locale,collapsible=getattr(bot,'platform',None)!='max')
    if name in {'start','settings','register'}:
        return await settings_text(connection,chat),settings_keyboard(chat)
    if name=='help':
        return help_text(locale),None
    if name=='menu_hint':
        return menu_hint(locale),None
    if name=='menu':
        if enum_value(message.chat.type)=='private' and getattr(bot,'platform',None)!='max':
            await connection.execute('UPDATE telegram_chats SET keyboard_hidden=false WHERE telegram_chat_id=$1',message.chat.id)
            return menu_hint(locale),main_keyboard(locale)
        return menu_hint(locale),None
    if name=='search' and not args:
        return search_prompt(locale),None
    if name=='read' and not args:
        return search_prompt(locale),None
    if name=='devotions':
        if args==['off']:
            for mode in ['morning_verse','evening_verse']:
                await set_enabled(connection,chat['telegram_chat_id'],False,mode)
            return ('Утренние и вечерние стихи отключены.' if locale=='ru' else 'Morning and evening verses disabled.'),settings_keyboard(chat)
        if args:raise UserError('invalid')
        return await enable_devotions(connection,chat,user.id),settings_keyboard(chat)
    if name=='daily':
        if args==['off']:
            await set_enabled(connection,chat['telegram_chat_id'],False,'verse_of_day')
            return ('🔕 Ежедневный стих отключён.' if locale=='ru' else '🔕 Daily verse disabled.'),settings_keyboard(chat)
        if len(args)>2:
            raise UserError('invalid')
        edition = await bible.chat_translation(connection,chat['telegram_chat_id'])
        if not edition:
            raise UserError('not_ready')
        sub = await create_or_update_subscription(connection,chat_id=chat['telegram_chat_id'],created_by=user.id,
            translation_id=edition['id'],mode='verse_of_day',
            send_time=parse_hhmm(args[0] if args else settings.default_send_time),
            timezone_name=args[1] if len(args)>1 else chat['timezone'])
        return daily_confirmation(sub,locale),settings_keyboard(chat)
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
    if name=='timezone':
        from app.bot.timezones import menu
        if not args:
            return menu(chat)
        if len(args)!=1:
            raise UserError('invalid')
        chat=await configure_chat(connection,chat_id=chat['telegram_chat_id'],actor_id=user.id,timezone_name=args[0])
        return await settings_text(connection,chat),settings_keyboard(chat)
    if name=='time':
        if not args:
            target = await command_target_suffix(connection,chat)
            return f"{tr(locale,'time')}: <code>/time 09:00 {escape(chat['timezone'])}{target}</code>",None
        if not 1<=len(args)<=2:
            raise UserError('invalid')
        clock,tz = parse_hhmm(args[0]),args[1] if len(args)==2 else chat['timezone']
        validate_timezone(tz)
        async with chat_lock(connection,chat['telegram_chat_id']),connection.transaction():
            chat = await configure_chat(connection,chat_id=chat['telegram_chat_id'],actor_id=user.id,timezone_name=tz)
            rows = await list_subscriptions(connection,chat['telegram_chat_id'])
            for row in rows:
                await connection.execute('UPDATE subscriptions SET send_time=$2,next_run_at=$3,revision=revision+1 WHERE id=$1',row['id'],row['send_time'] if row['mode'] in {'morning_verse','evening_verse'} else clock,
                    next_reading(row['mode'],row['send_time'] if row['mode'] in {'morning_verse','evening_verse'} else clock,tz,list(row['days_of_week'])) if row['is_enabled'] else None)
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
        return bible.license_details(edition,locale),None
    if name=='next':
        await enqueue_next(connection,chat,edition,f'{message.chat.id}:{message.message_id}',settings.max_message_length)
        # The delivery worker sends the reading; no separate queue receipt is needed.
        return None,None
    local_date = datetime.now(ZoneInfo(chat['timezone'])).date()
    row = None
    if name=='today':
        row = await readings.daily(connection,edition,str(chat['telegram_chat_id']),local_date)
    elif name=='random':
        row = await readings.choose(connection,edition)
    elif name=='topic':
        result = await bible.topic_verse(connection,edition,args[0] if args else None,str(chat['telegram_chat_id']),local_date)
        row = await readings.contextual(connection,edition,result[1]) if result else None
    elif name in {'search','read'}:
        result = await passage.lookup(connection,edition,' '.join(args))
        if result:
            text = await passage.render(connection,edition,result,locale)
            if result[0].first is None:
                text = await illustrations.decorate_chapter(connection,text,edition,result[1],result[0].chapter,
                    chat=chat,request_key=f"{message.chat.id}:{message.message_id}",
                    thread_id=message.message_thread_id,max_length=settings.max_message_length,rows=result[2])
            elif len(result[2])==1:
                text = await illustrations.decorate(connection,text,result[2][0],edition,chat=chat,
                    request_key=f"{message.chat.id}:{message.message_id}",thread_id=message.message_thread_id)
            return text,None
        if name=='read':
            return search_prompt(locale),None
        rows = await bible.search_verses(connection,edition['id'],' '.join(args),5)
        from app.services.message_languages import combine
        return combine([await bible.render_verse(connection,r,edition,ui_language=locale) for r in rows]) or tr(locale,'no_result'),None
    else:
        raise UserError('invalid')
    if row:
        text = await bible.render_verse(connection,row,edition,ui_language=locale)
        return await illustrations.decorate(connection,text,row,edition,chat=chat,
            request_key=f"{message.chat.id}:{message.message_id}",thread_id=message.message_thread_id),None
    return tr(locale,'no_result'),None


