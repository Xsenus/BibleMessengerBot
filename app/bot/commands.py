"""Pure command parsing, suitable for exhaustive offline validation."""
from __future__ import annotations
from dataclasses import dataclass
import re
from app.services.errors import UserError

MANAGEMENT = {'settings','language','ui','translation','translations','register','subscribe','channel',
    'pause','resume','unsubscribe','status','next','reset','resolve','thread','time','license','topics'}
MODE_ALIASES = {'order':'sequential','порядок':'sequential','verse':'verse_of_day','стих':'verse_of_day',
    'topic':'topic_of_day','тема':'topic_of_day','plan':'reading_plan','план':'reading_plan'}


@dataclass(frozen=True,slots=True)
class ParsedCommand:
    """Remote targets are explicit channel/group handles, never arbitrary private recipients."""
    name: str
    arguments: tuple[str,...]
    target: str | None = None
    mentioned_bot: str | None = None


def parse_command(text: str) -> ParsedCommand:
    """Parse a bounded command and an optional final @channel or negative numeric ID."""
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
    """Keep stable machine command names while accepting familiar short aliases."""
    name = MODE_ALIASES.get(value.lower(),value.lower())
    if name not in {'sequential','verse_of_day','topic_of_day','reading_plan'}:
        raise UserError('invalid')
    return name


def encode_callback(action: str, chat_id: int, value: str = '') -> str:
    """Telegram callback_data is at most 64 UTF-8 bytes, not 64 characters."""
    text = f'v1:{action}:{chat_id}:{value}'
    if len(text.encode())>64 or ':' in action or ':' in value:
        raise ValueError('Invalid callback payload')
    return text


def decode_callback(text: str) -> tuple[str,int,str]:
    """Reject fabricated shapes before they can reach authorization or SQL."""
    if len(text.encode())>64:
        raise UserError('invalid')
    parts = text.split(':')
    if len(parts)!=4 or parts[0]!='v1' or not re.fullmatch(r'-?[1-9][0-9]{0,18}',parts[2]):
        raise UserError('invalid')
    return parts[1],int(parts[2]),parts[3]
