"""Convert shared navigation to MAX's native inline keyboard attachment."""
from __future__ import annotations

from app.bot.ui import button_label, keyboard_command


def keyboard_attachment(markup, *, audio: tuple[int, int] | None = None, locale='ru', navigation=False) -> dict | None:
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
    if navigation:
        present = {button.get('payload') for row in rows for button in row}
        shortcuts = [('next', '📖'), ('random', '🎲'), ('search', '🔎'), ('menu', '☰')]
        buttons = []
        for command, emoji in shortcuts:
            payload = 'maxcmd:/' + command
            if payload not in present:
                label = ('☰ Меню' if locale == 'ru' else '☰ Menu') if command == 'menu' else button_label(locale, command, emoji)
                buttons.append({'type': 'callback', 'text': label, 'payload': payload})
        rows.extend(buttons[index:index + 2] for index in range(0, len(buttons), 2))
    if audio:
        card_id, audio_id = audio
        rows.append([{'type': 'callback', 'text': '🔊 Слушать' if locale == 'ru' else '🔊 Listen',
                      'payload': f'maxaudio:{card_id}:{audio_id}'}])
    return {'type': 'inline_keyboard', 'payload': {'buttons': rows}} if rows else None
