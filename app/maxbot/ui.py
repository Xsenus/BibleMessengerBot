"""Convert shared navigation to MAX's native inline keyboard attachment."""
from __future__ import annotations

from app.bot.ui import keyboard_command


def keyboard_attachment(markup, *, audio: tuple[int, int] | None = None, locale='ru') -> dict | None:
    rows = []
    if markup is not None:
        if hasattr(markup, 'inline_keyboard'):
            for row in markup.inline_keyboard:
                converted = []
                for button in row:
                    if button.callback_data:
                        converted.append({'type': 'callback', 'text': button.text,
                                          'payload': button.callback_data})
                    elif button.url and button.url.startswith('https://'):
                        converted.append({'type': 'link', 'text': button.text, 'url': button.url})
                    else:
                        raise ValueError('Unsupported MAX navigation button')
                if converted:
                    rows.append(converted)
        elif hasattr(markup, 'keyboard'):
            for row in markup.keyboard:
                converted = []
                for button in row:
                    command = keyboard_command(button.text)
                    if not command:
                        raise ValueError('Unknown shared navigation command')
                    converted.append({'type': 'callback', 'text': button.text,
                                      'payload': 'maxcmd:' + command})
                rows.append(converted)
        else:
            raise ValueError('Unsupported MAX keyboard')
    if audio:
        card_id, audio_id = audio
        rows.append([{'type': 'callback', 'text': '🔊 Слушать' if locale == 'ru' else '🔊 Listen',
                      'payload': f'maxaudio:{card_id}:{audio_id}'}])
    return {'type': 'inline_keyboard', 'payload': {'buttons': rows}} if rows else None
