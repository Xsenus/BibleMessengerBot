# ruff: noqa: RUF001
"""MAX-specific help; do not advertise Telegram-only Stars or forum controls."""


def help_text(locale='ru'):
    if locale=='ru':
        return ('📖 <b>Библия каждый день</b>\n\n'
                '<b>Читать сейчас</b>\n/next — следующая глава\n/today — стих дня\n/random — случайное чтение\n'
                '/search любовь — поиск слов\n/read Иоанна 3:16 — стих\n/read Иоанна 3 — глава\n\n'
                '<b>Языки и озвучка</b>\n/translations — все издания\n/language ru — язык Библии\n/ui en — язык меню\n'
                '/license — источник и лицензия\nКнопки под чтением меняют перевод и страницу. «Слушать» отправляет аудио.\n\n'
                '<b>По расписанию</b>\n/daily — ежедневный стих\n/devotions — утро и вечер\n'
                '/subscribe reading_plan 09:00 Europe/Moscow bible-365 — план\n'
                '/subscribe sequential 09:00 Europe/Moscow — главы по порядку\n'
                '/timezone — выбрать город\n/time 09:00 Europe/Moscow — время рассылки\n'
                '/status — настройки и прогресс\n/pause — пауза\n/resume — продолжить\n/unsubscribe — отключить\n'
                '/reset confirm — сбросить прогресс и приостановить подписки\n\n'
                '<b>Группы и каналы</b>\nДобавьте бота администратором с правом писать сообщения. '
                'Настройки каждого чата независимы. Администратор может использовать /settings и /subscribe в группе; '
                'для канала отправьте /settings chat:123, заменив 123 его числовым ID, в личном диалоге. '
                '/chats показывает доступные вам чаты и команды настройки.\n\n'
                '<b>Добровольная поддержка</b>\n/donate — карта или СБП\n/donations — история\n'
                '/paysupport — вопросы и возврат\n/terms — условия\nБиблия бесплатна; поддержка добровольная.\n\n/start — главное меню')
    return ('📖 <b>Bible Every Day</b>\n\n'
            '/next — next chapter\n/today — daily verse\n/random — random reading\n/search love — word search\n'
            '/read John 3:16 — verse\n/read John 3 — chapter\n/translations — editions\n/language en — Bible language\n'
            '/ui en — menu language\n/license — source/license\n'
            'Buttons change one reading’s language/page. Listen sends its audio.\n\n'
            '/daily — daily verse\n/devotions — morning and evening\n'
            '/subscribe reading_plan 09:00 Europe/London bible-365\n/subscribe sequential 09:00 Europe/London\n'
            '/timezone — city\n/time 09:00 Europe/London — schedule\n/status — preferences/progress\n'
            '/pause · /resume · /unsubscribe\n/reset confirm — reset progress and pause\n\n'
            'Add the bot as an administrator with posting permission for groups/channels. '
            'Each chat has independent preferences. Use /settings in the group, or /settings followed by '
            'chat:123 (replace 123 with its numeric ID) in a private dialog. /chats lists your available destinations.\n\n'
            '/donate — card or SBP\n/donations — history\n/paysupport — payment issues/refunds\n/terms — terms\n'
            'The Bible is free; support is optional.\n/start — main menu')
