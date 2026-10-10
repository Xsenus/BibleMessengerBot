"""Localized Telegram administration with authorization on every command/callback.

Groups and channels never inherit the latest administrator's language. Remote
management uses a private dialog with the bot, and permissions are rechecked.
"""
from __future__ import annotations
import logging
from typing import Any
from aiogram import F,Router
from aiogram.types import Message,CallbackQuery,InlineKeyboardMarkup,ChatMemberUpdated,ReplyKeyboardRemove
from aiogram.exceptions import TelegramAPIError
from app.bot.commands import parse_command,mode_name,decode_callback
from app.bot.donations import handle_command as handle_donation_command
from app.bot.transport import TelegramSender
from app.bot.ui import keyboard_command,send_welcome
from app.services import bible
from app.services import passage,illustrations
from app.services.destinations import configure_chat
from app.services.errors import UserError
from app.services.formatting import escape
from app.services.i18n import initial_ui,native_ui_name,tr,ui_for_language
from app.services.locks import chat_lock
from app.services.scheduling import parse_hhmm
from app.services.subscriptions import create_or_update_subscription,set_enabled
from app.bot.common import authorize,button,destination,enum_value,register_context,reply  # noqa: F401
from app.bot.dispatch import run_command
from app.bot.menus import (  # noqa: F401 - re-exported for tests and callers
    change_language,change_voice,command_target_suffix,daily_confirmation,edition_menu,
    enable_devotions,help_text,language_menu,modes_menu,settings_keyboard,settings_text,status_text,ui_menu,voices_menu)

router = Router(name='localized-destinations')
LOGGER = logging.getLogger(__name__)


@router.message(F.text.startswith('/'))
async def command_handler(message: Message,bot: Any,db_pool: Any,settings: Any) -> None:
    """Anonymous administrators must use private chat under their real identity."""
    if not message.from_user or message.from_user.is_bot or message.sender_chat:
        return
    donation_command = None
    async with db_pool.acquire() as connection:
        locale = initial_ui(message.from_user.language_code)
        try:
            parsed = parse_command(message.text or '')
            if parsed.mentioned_bot:
                me = await bot.get_me()
                if parsed.mentioned_bot.lower()!=(me.username or '').lower():
                    return
            await register_context(connection,message.from_user,message.chat,settings,
                allow_blocked=parsed.name in {'paysupport','donations','terms'})
            locale = await connection.fetchval('SELECT ui_language FROM telegram_chats WHERE telegram_chat_id=$1',message.chat.id)
            if parsed.name in {'donate','donations','paysupport','terms'}:
                donation_command = (parsed.name,' '.join(parsed.arguments))
            elif parsed.name=='start' and parsed.arguments==('donate',):
                donation_command = ('donate','')
            else:
                text,markup = await run_command(connection,bot,settings,message,parsed)
                if parsed.name=='start' and enum_value(message.chat.type)=='private':
                    if await send_welcome(bot,connection,settings,message.chat.id,text,markup):
                        return
        except UserError as error:
            text,markup = tr(locale,error.key),None
        except (ValueError,KeyError):
            text,markup = tr(locale,'invalid'),None
        except TelegramAPIError:
            text,markup = tr(locale,'forbidden'),None
        except Exception as error:
            LOGGER.error('Command failed: %s',type(error).__name__)
            text,markup = tr(locale,'not_ready'),None
        if donation_command is None and text is not None:
            if markup is None and enum_value(message.chat.type)=='private' and not isinstance(text,illustrations.ReadingText) and not hasattr(text,'refs'):
                hidden = await connection.fetchval('SELECT keyboard_hidden FROM telegram_chats WHERE telegram_chat_id=$1',message.chat.id)
                markup = ReplyKeyboardRemove() if hidden else None
            await reply(bot,connection,settings,message.chat.id,text,markup,message.message_thread_id)
    if donation_command is not None:
        # Release the shared connection before the payment flow acquires its own.
        await handle_donation_command(message,bot,settings,db_pool,*donation_command)


@router.message(F.chat.type=='private',F.text,~F.text.startswith('/'))
async def private_text_handler(message: Message,bot: Any,db_pool: Any,settings: Any) -> None:
    """Route exact navigation labels through the usual authorization and error handling."""
    if enum_value(message.chat.type)!='private':
        return
    content = message.text or ''
    command = keyboard_command(content)
    if command is None:
        try:
            reference = passage.parse_reference(content)
        except UserError:
            reference = True
        command = '/read '+content if reference else '/menu_hint'
    await command_handler(message.model_copy(update={'text':command}),bot,db_pool,settings)


@router.callback_query(F.data.startswith('lc:'))
async def message_language_handler(callback: CallbackQuery,bot: Any,db_pool: Any,settings: Any) -> None:
    """Change one acknowledged Bible message without altering chat preferences."""
    import re
    from app.services.message_languages import select
    from app.worker.delivery import process_delivery

    if not isinstance(callback.message,Message):
        await callback.answer()
        return
    locale = initial_ui(callback.from_user.language_code)
    error_text = None
    delivery = None
    async with db_pool.acquire() as connection:
        try:
            match = re.fullmatch(r'lc:([1-9][0-9]{0,17}):([sptc]):([0-9]{1,18})',callback.data or '')
            if not match:
                raise UserError('invalid')
            await authorize(bot,callback.message.chat,callback.from_user.id)
            await register_context(connection,callback.from_user,callback.message.chat,settings)
            locale = await connection.fetchval('SELECT ui_language FROM telegram_chats WHERE telegram_chat_id=$1',callback.message.chat.id) or locale
            async with chat_lock(connection,callback.message.chat.id):
                delivery = await select(connection,callback.message.chat.id,callback.message.message_id,
                                        int(match[1]),match[2],int(match[3]))
        except UserError as error:
            error_text = tr(locale,error.key)
        except (ValueError,KeyError,OverflowError):
            error_text = tr(locale,'invalid')
        except Exception as error:
            LOGGER.error('Message language callback failed: %s',type(error).__name__)
            error_text = tr(locale,'not_ready')
        await callback.answer(text=error_text,show_alert=bool(error_text))
        if delivery is not None:
            # Durable outbox remains eligible if the callback process stops here.
            await process_delivery(connection,delivery,TelegramSender(bot,connection,settings))


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
            if action=='voices':
                text,markup=voices_menu(chat)
            elif action=='voice':
                chat=await change_voice(connection,chat,value)
                text,markup=voices_menu(chat)
            elif action=='lang':
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
            elif action=='devotions':
                text,markup = await enable_devotions(connection,chat,callback.from_user.id),settings_keyboard(chat)
            elif action=='daily':
                edition = await bible.chat_translation(connection,chat_id)
                if not edition:
                    raise UserError('not_ready')
                sub = await create_or_update_subscription(connection,chat_id=chat_id,created_by=callback.from_user.id,
                    translation_id=edition['id'],mode='verse_of_day',
                    send_time=parse_hhmm(settings.default_send_time),timezone_name=chat['timezone'])
                text,markup = daily_confirmation(sub,locale),settings_keyboard(chat)
            elif action=='zones':
                from app.bot.timezones import menu
                text,markup=menu(chat)
            elif action=='setzone':
                chat=await configure_chat(connection,chat_id=chat_id,actor_id=callback.from_user.id,timezone_name=value)
                text,markup=await settings_text(connection,chat),settings_keyboard(chat)
            elif action=='timehelp':
                suffix = await command_target_suffix(connection,chat)
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
