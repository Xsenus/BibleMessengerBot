"""Общие помощники обработчиков Telegram: проверка прав, регистрация контекста, выбор целевого чата и отправка ответов."""
from __future__ import annotations
import logging
from typing import Any
from aiogram.types import InlineKeyboardButton
from aiogram.exceptions import TelegramAPIError
from app.bot.commands import encode_callback
from app.bot.transport import TelegramSender
from app.services.platforms import is_bridged
from app.services import illustrations
from app.services.accounts import upsert_user,upsert_chat
from app.services.errors import UserError,SendError
from app.services.i18n import initial_ui


LOGGER = logging.getLogger(__name__)

LOGGER = logging.getLogger(__name__)



def enum_value(value: Any) -> str:
    """Получить строковое значение и у перечислений aiogram, и у обычных строк."""
    return str(getattr(value,'value',value))


async def authorize(bot: Any, chat: Any, actor_id: int) -> None:
    """Проверить права на чат: в личном чате пользователь должен писать из своего чата; в группе и канале он обязан быть администратором, бот — тоже, а в канале у бота должно быть право публикации. Любая неопределённость — отказ (`forbidden`)."""
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


async def register_context(connection: Any, user: Any, chat: Any, settings: Any,
                           *, allow_blocked: bool = False) -> None:
    """Сохранить пользователя и чат (при первом контакте выбирается язык интерфейса по языку Telegram; для групп — язык по умолчанию). Заблокированному пользователю доступ закрыт, кроме команд поддержки (`allow_blocked`)."""
    await upsert_user(connection,user_id=user.id,username=user.username,first_name=user.first_name,
        last_name=user.last_name,language_code=user.language_code,default_timezone=settings.default_timezone)
    if not allow_blocked and await connection.fetchval('SELECT is_blocked FROM telegram_users WHERE telegram_user_id=$1',user.id):
        raise UserError('forbidden')
    await upsert_chat(connection,chat_id=chat.id,chat_type=enum_value(chat.type),title=chat.title,
        username=chat.username,registered_by=user.id,default_timezone=settings.default_timezone,
        ui_language=initial_ui(user.language_code) if enum_value(chat.type)=='private' else 'ru')


async def destination(connection: Any, bot: Any, current_chat: Any, actor_id: int,
                      target: str | int | None, settings: Any) -> Any:
    """Определить чат, которым управляет команда: текущий или удалённый (канал/группа по @имени или ID — только из личного диалога). Проверяет права, регистрирует чат и возвращает его настройки."""
    if target is not None and enum_value(current_chat.type)!='private' and str(target)!=str(current_chat.id):
        raise UserError('private_only')
    try:
        chat = current_chat if target is None or str(target)==str(current_chat.id) else await bot.get_chat(int(target) if str(target).lstrip('-').isdigit() else target)
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
    """Отправить ответ: в MAX — через очередь, в Telegram — безопасно разбив HTML на части и подготовив кнопки языков. Неопределённый результат отправки автоматически не повторяется."""
    if is_bridged(getattr(bot,'platform',None)):
        await bot.queue_response(connection,settings,chat_id,text,markup)
        return
    parts = illustrations.chunks(text,getattr(text,'image_id',None),settings.max_message_length)
    from app.services.message_languages import prepare
    parts = await prepare(connection,text,parts,{'telegram_chat_id':chat_id})
    sender = TelegramSender(bot,connection,settings)
    for index,part in enumerate(parts):
        try:
            message_id = await sender.send(chat_id,part,thread,reply_markup=markup if index==len(parts)-1 else None)
            if isinstance(part,dict) and part.get('kind')=='rich':
                await illustrations.bind_message(connection,chat_id,part.get('request_id'),message_id)
                if part.get('card_id'):
                    from app.services.message_languages import bind
                    await bind(connection,part['card_id'],chat_id,message_id)
        except SendError as error:
            LOGGER.warning('UI response to %s stopped: %s',chat_id,error.kind)
            return


def button(text: str, action: str, chat_id: int, value: str = '') -> InlineKeyboardButton:
    """Создать inline-кнопку; в её данные всегда входит целевой чат, права на который проверяются заново при нажатии."""
    return InlineKeyboardButton(text=text,callback_data=encode_callback(action,chat_id,value))


