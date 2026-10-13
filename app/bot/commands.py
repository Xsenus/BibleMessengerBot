"""Разбор команд и данных кнопок — чистые функции без обращений к сети и БД, их легко проверять тестами."""
from __future__ import annotations
from dataclasses import dataclass
import re
from app.services.errors import UserError

MANAGEMENT = {'settings','language','ui','translation','translations','register','subscribe','channel','daily','read','devotions',
    'pause','resume','unsubscribe','status','next','reset','resolve','thread','time','timezone','license','topics','voice'}
MODE_ALIASES = {'morning':'morning_verse','evening':'evening_verse','утро':'morning_verse','вечер':'evening_verse','order':'sequential','порядок':'sequential','verse':'verse_of_day','стих':'verse_of_day',
    'topic':'topic_of_day','тема':'topic_of_day','plan':'reading_plan','план':'reading_plan'}


@dataclass(frozen=True,slots=True)
class ParsedCommand:
    """Разобранная команда: имя, аргументы, необязательная цель (канал или группа) и упомянутый бот. Цель — только явный @канал или отрицательный числовой ID, но не произвольный личный получатель."""
    name: str
    arguments: tuple[str,...]
    target: str | None = None
    mentioned_bot: str | None = None


def parse_command(text: str) -> ParsedCommand:
    """Разобрать текст команды (до 4096 символов): `/имя[@бот] аргументы…`. Для управляющих команд последний аргумент вида `@канал` или `-100…` становится целью. Неверный формат — UserError('invalid')."""
    if len(text)>4096:
        raise UserError('invalid')
    tokens = text.strip().split()
    if not tokens or not re.fullmatch(r'/[A-Za-z_]+(?:@[A-Za-z0-9_]+)?',tokens[0]):
        raise UserError('invalid')
    name,_,mention = tokens.pop(0)[1:].partition('@')
    name = name.lower()
    target = None
    if name in MANAGEMENT and tokens and re.fullmatch(r'(?:@[A-Za-z][A-Za-z0-9_]{3,31}|-[1-9][0-9]{0,18})',tokens[-1]):
        target = tokens.pop()
    return ParsedCommand(name,tuple(tokens),target,mention or None)


def mode_name(value: str) -> str:
    """Привести режим рассылки к внутреннему имени, принимая привычные короткие названия (утро, вечер, порядок, стих, тема, план и т. п.); неизвестный режим — ошибка."""
    name = MODE_ALIASES.get(value.lower(),value.lower())
    if name not in {'sequential','verse_of_day','topic_of_day','reading_plan','morning_verse','evening_verse'}:
        raise UserError('invalid')
    return name


def encode_callback(action: str, chat_id: int, value: str = '') -> str:
    """Собрать данные кнопки `v1:действие:чат:значение`; Telegram ограничивает их 64 байтами (а не символами), двоеточие внутри запрещено."""
    text = f'v1:{action}:{chat_id}:{value}'
    if len(text.encode())>64 or ':' in action or ':' in value:
        raise ValueError('Invalid callback payload')
    return text


def decode_callback(text: str) -> tuple[str,int,str]:
    """Разобрать данные кнопки и отвергнуть поддельные формы до проверки прав и обращения к SQL. Возвращает (действие, чат, значение)."""
    if len(text.encode())>64:
        raise UserError('invalid')
    parts = text.split(':')
    if len(parts)!=4 or parts[0]!='v1' or not re.fullmatch(r'-?[1-9][0-9]{0,18}',parts[2]):
        raise UserError('invalid')
    return parts[1],int(parts[2]),parts[3]
