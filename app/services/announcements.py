"""Объявления администраторов: разовая отправка всем и расписания (еженедельно, ежедневно, с повторами и без).

Объявление хранит текст, необязательную картинку, аудиторию и расписание. Планировщик (`run_due`, вызывается службой
composer) в момент запуска создаёт запись о запуске и ставит сообщения в очередь доставки всем назначениям аудитории.
Запуск идемпотентен (уникальная пара «объявление + момент»), опоздавший запуск пропускается, перезапуск службы
дорассылает незавершённые запуски без дублей.
"""
from __future__ import annotations

import calendar
import json
import logging
import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.services import fanout
from app.services.errors import UserError
from app.services.formatting import escape
from app.services.scheduling import next_occurrence, on_date, parse_hhmm, validate_timezone

LOGGER = logging.getLogger(__name__)

DAYS = {'mon': 1, 'пн': 1, 'tue': 2, 'вт': 2, 'wed': 3, 'ср': 3, 'thu': 4, 'чт': 4,
        'fri': 5, 'пт': 5, 'sat': 6, 'сб': 6, 'sun': 7, 'вс': 7}
WEEKDAYS = {'weekdays': [1, 2, 3, 4, 5], 'будни': [1, 2, 3, 4, 5], 'weekend': [6, 7], 'выходные': [6, 7],
            'daily': [1, 2, 3, 4, 5, 6, 7], 'всегда': [1, 2, 3, 4, 5, 6, 7]}
KIND_ALIASES = {'now': 'now', 'сейчас': 'now', 'once': 'once', 'однократно': 'once', 'daily': 'daily', 'ежедневно': 'daily',
                'weekly': 'weekly', 'еженедельно': 'weekly', 'monthly': 'monthly', 'ежемесячно': 'monthly',
                'every': 'interval', 'каждые': 'interval'}


def render(title: str, body: str) -> str:
    """Текст сообщения: жирный заголовок и экранированный текст (разметка администратора не интерпретируется)."""
    head = f'<b>{escape(title.strip())}</b>\n\n' if title and title.strip() else ''
    return head + escape(body.strip())


def _local(day: str, clock: str, timezone_name: str) -> datetime:
    """Момент «дата + время» в часовом поясе (UTC); неверный формат — UserError."""
    try:
        return on_date(date.fromisoformat(day), parse_hhmm(clock), timezone_name)
    except ValueError:
        raise UserError('invalid') from None


def parse_when(text: str, default_timezone: str = 'UTC', now: datetime | None = None) -> dict[str, Any]:
    """Разобрать описание расписания в параметры объявления.

    Форматы (регистр не важен; часовой пояс IANA необязателен):
      `now` · `once 2026-10-20 18:00` · `daily 09:00` · `weekly sat,sun 10:00` · `weekly weekdays 08:30`
      `monthly 15 10:00` · `every 90m|2h|1d [from 2026-10-20 18:00]`
    и необязательные ограничения: `x5` (не более 5 запусков), `until 2026-12-31` (по дату включительно).
    """
    now = now or datetime.now(UTC)
    tokens = text.lower().replace(',', ' , ').split()
    tz = default_timezone
    max_runs = end_at = None
    out: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if re.fullmatch(r'x[1-9][0-9]{0,3}', token):
            max_runs = int(token[1:])
        elif token in {'until', 'до'} and index + 1 < len(tokens):
            index += 1
            end_at = ('until', tokens[index])
        elif '/' in token:
            tz = validate_timezone(_zone_case(token))
        else:
            out.append(token)
        index += 1
    out = [t for t in out if t != ',']
    if not out or out[0] not in KIND_ALIASES:
        raise UserError('invalid')
    kind = KIND_ALIASES[out[0]]
    args = out[1:]
    schedule: dict[str, Any] = dict(kind='once' if kind == 'now' else kind, send_time=None, timezone=tz,
                                    days_of_week=[1, 2, 3, 4, 5, 6, 7], day_of_month=None, interval_minutes=None,
                                    start_at=None, end_at=None, max_runs=max_runs)
    if kind == 'now':
        schedule['start_at'] = now
        schedule['max_runs'] = 1
    elif kind == 'once':
        if len(args) != 2:
            raise UserError('invalid')
        schedule['start_at'] = _local(args[0], args[1], tz)
        schedule['max_runs'] = 1
    elif kind == 'daily':
        schedule['send_time'] = _clock(args)
    elif kind == 'weekly':
        if len(args) < 2:
            raise UserError('invalid')
        schedule['days_of_week'] = _days(','.join(args[:-1]))          # `sat,sun` разбирается на два слова — склеиваем дни обратно
        schedule['send_time'] = _clock(args[-1:])
    elif kind == 'monthly':
        if len(args) != 2 or not args[0].isdigit() or not 1 <= int(args[0]) <= 31:
            raise UserError('invalid')
        schedule['day_of_month'] = int(args[0])
        schedule['send_time'] = _clock(args[1:])
    else:  # interval
        match = re.fullmatch(r'([1-9][0-9]{0,4})([mhdмчд])', args[0]) if args else None
        if not match:
            raise UserError('invalid')
        unit = {'m': 1, 'м': 1, 'h': 60, 'ч': 60, 'd': 1440, 'д': 1440}[match[2]]
        schedule['interval_minutes'] = int(match[1]) * unit
        if schedule['interval_minutes'] < 5:
            raise UserError('invalid')
        if len(args) == 4 and args[1] in {'from', 'с'}:
            schedule['start_at'] = _local(args[2], args[3], tz)
        elif len(args) != 1:
            raise UserError('invalid')
    if end_at:
        schedule['end_at'] = _local(end_at[1], '23:59', tz)
    return schedule


def _zone_case(token: str) -> str:
    """Вернуть точное название пояса IANA (`europe/moscow` → `Europe/Moscow`); неизвестное возвращается с заглавными буквами."""
    from zoneinfo import available_timezones
    wanted = token.lower()
    for name in available_timezones():
        if name.lower() == wanted:
            return name
    return '/'.join('_'.join(part.capitalize() for part in piece.split('_')) for piece in token.split('/'))


def _clock(args: list[str]) -> time:
    if len(args) != 1:
        raise UserError('invalid')
    try:
        return parse_hhmm(args[0])
    except ValueError:
        raise UserError('invalid') from None


def _days(value: str) -> list[int]:
    if value in WEEKDAYS:
        return list(WEEKDAYS[value])
    result = []
    for part in re.split(r'[,\s]+', value.strip(',')):
        if part not in DAYS:
            raise UserError('invalid')
        result.append(DAYS[part])
    return sorted(set(result))


def _monthly(day_of_month: int, clock: time, timezone_name: str, after: datetime) -> datetime:
    """Ближайшее `day_of_month`-е число (если в месяце столько дней нет — последний день) строго после `after`."""
    zone = ZoneInfo(timezone_name)
    local = after.astimezone(zone)
    year, month = local.year, local.month
    for _ in range(26):
        day = min(day_of_month, calendar.monthrange(year, month)[1])
        candidate = on_date(date(year, month, day), clock, timezone_name)
        if candidate > after:
            return candidate
        month += 1
        if month == 13:
            year, month = year + 1, 1
    raise RuntimeError('Unable to calculate monthly occurrence')


def compute_next(row: Any, previous: datetime | None, now: datetime) -> datetime | None:
    """Следующий запланированный момент (UTC) или None, если запусков больше нет.

    `previous` — момент только что выполненного запуска (None при создании). Для расписаний с повторами результат
    строго позже и `previous`, и `now`; ограничения `end_at` и `max_runs` учитываются.
    """
    kind = row['kind']
    anchor = max(previous, now) if previous else now
    if kind == 'once':
        result = row['start_at'] if previous is None else None
    elif kind in {'daily', 'weekly'}:
        result = next_occurrence(row['send_time'], row['timezone'], list(row['days_of_week']), now=anchor)
    elif kind == 'monthly':
        result = _monthly(row['day_of_month'], row['send_time'], row['timezone'], anchor)
    else:
        step = timedelta(minutes=row['interval_minutes'])
        if previous is None:
            result = row['start_at'] or now + step
        else:
            result = previous + step
            while result <= now:
                result += step
    if row['start_at'] and kind in {'daily', 'weekly', 'monthly'} and result and result < row['start_at']:
        result = compute_next(row, row['start_at'] - timedelta(seconds=1), now)
    if result and row['end_at'] and result > row['end_at']:
        return None
    return result


async def create(connection: Any, created_by: int, title: str, body: str, schedule: dict, *,
                 audience: dict | None = None, asset_id: int | None = None) -> Any:
    """Создать объявление с расписанием и вернуть строку (с вычисленным `next_run_at`)."""
    body = body.strip()
    if not 1 <= len(body) <= 3500 or len(title) > 200:
        raise UserError('invalid')
    now = datetime.now(UTC)
    probe = dict(schedule, runs_done=0)
    next_run = compute_next(probe, None, now)
    if next_run is None:
        raise UserError('invalid')
    return await connection.fetchrow(
        '''INSERT INTO announcements(title,body,asset_id,audience,created_by,kind,send_time,timezone,days_of_week,day_of_month,
           interval_minutes,start_at,end_at,max_runs,next_run_at) VALUES($1,$2,$3,$4::jsonb,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
           RETURNING *''', title.strip(), body, asset_id, json.dumps(fanout.normalize_audience(audience)), created_by,
        schedule['kind'], schedule['send_time'], schedule['timezone'], schedule['days_of_week'], schedule['day_of_month'],
        schedule['interval_minutes'], schedule['start_at'], schedule['end_at'], schedule['max_runs'], next_run)


async def set_status(connection: Any, announcement_id: int, status: str) -> bool:
    """Приостановить (`paused`), возобновить (`active`) или отменить (`cancelled`) объявление.

    При возобновлении следующий запуск пересчитывается от текущего момента (пропущенное не догоняется).
    """
    row = await connection.fetchrow('SELECT * FROM announcements WHERE id=$1', announcement_id)
    if not row or row['status'] in {'finished', 'cancelled'} and status != 'cancelled':
        return False
    next_run = row['next_run_at']
    if status == 'active':
        now = datetime.now(UTC)
        # Для расписаний по времени суток ближайший запуск считается от текущего момента, чтобы пауза не съедала предстоящий запуск.
        previous = row['next_run_at'] if row['kind'] == 'interval' else None
        next_run = compute_next(dict(row, runs_done=row['runs_done']), previous, now) if row['next_run_at'] else None
        if row['kind'] == 'once' and row['runs_done'] == 0:
            next_run = row['start_at'] if row['start_at'] and row['start_at'] > datetime.now(UTC) else datetime.now(UTC)
    result = await connection.execute(
        'UPDATE announcements SET status=$2,next_run_at=$3,updated_at=now() WHERE id=$1', announcement_id, status, next_run)
    return result == 'UPDATE 1'


async def deliver_run(connection: Any, announcement: Any, run_id: int) -> dict[str, int]:
    """Поставить сообщение запуска в очередь доставки всей аудитории и записать итог."""
    audience = announcement['audience']
    audience = json.loads(audience) if isinstance(audience, str) else audience
    counts = await fanout.enqueue(connection, key=f'announcement:{run_id}', text=render(announcement['title'], announcement['body']),
                                  mode='announcement', audience=audience, asset_id=announcement['asset_id'],
                                  progress={'announcement_id': announcement['id']})
    if counts['recipients'] and not counts['queued'] and counts.get('failed'):
        # Ничего не поставлено в очередь из-за сбоев: запуск остаётся `sending` и будет повторён (ключи доставки идемпотентны).
        raise RuntimeError('announcement run queued nothing')
    await connection.execute("UPDATE announcement_runs SET status='done',recipients=$2,queued=$3,skipped=$4 WHERE id=$1",
                             run_id, counts['recipients'], counts['queued'], counts['skipped'])
    return counts


async def run_one(connection: Any, announcement_id: int, now: datetime | None = None) -> int | None:
    """Выполнить наступивший запуск объявления: записать запуск, сдвинуть расписание и поставить рассылку в очередь.

    Возвращает номер запуска или None, если запускать нечего (не пора, на паузе, уже взято другим процессом).
    """
    now = now or datetime.now(UTC)
    async with connection.transaction():
        row = await connection.fetchrow('SELECT * FROM announcements WHERE id=$1 FOR UPDATE SKIP LOCKED', announcement_id)
        if not row or row['status'] != 'active' or not row['next_run_at'] or row['next_run_at'] > now:
            return None
        scheduled = row['next_run_at']
        late = (now - scheduled) > timedelta(minutes=row['misfire_grace_minutes'])
        run_id = await connection.fetchval(
            '''INSERT INTO announcement_runs(announcement_id,scheduled_for,status) VALUES($1,$2,$3)
               ON CONFLICT(announcement_id,scheduled_for) DO NOTHING RETURNING id''',
            announcement_id, scheduled, 'skipped_late' if late else 'sending')
        done = row['runs_done'] + (0 if late else 1)
        try:
            following = compute_next(dict(row, runs_done=row['runs_done']), scheduled, now)
        except ValueError:                                  # локальное время не существует (переход на летнее время) — следующий день
            following = compute_next(dict(row, runs_done=row['runs_done']), scheduled + timedelta(days=1), now)
        if row['max_runs'] and done >= row['max_runs']:
            following = None
        await connection.execute(
            "UPDATE announcements SET runs_done=$2,next_run_at=$3,status=CASE WHEN $3::timestamptz IS NULL THEN 'finished' ELSE status END,"
            "updated_at=now() WHERE id=$1", announcement_id, done, following)
    if run_id is None or late:
        return None
    await deliver_run(connection, row, run_id)
    return run_id


async def run_due(connection: Any, now: datetime | None = None, limit: int = 20) -> int:
    """Выполнить все наступившие запуски (вызывается службой composer каждые несколько секунд) и дорассылать
    запуски, прерванные перезапуском. Возвращает число выполненных запусков."""
    now = now or datetime.now(UTC)
    stuck = await connection.fetch(
        """SELECT r.id AS run_id,r.attempts,a.* FROM announcement_runs r JOIN announcements a ON a.id=r.announcement_id
           WHERE r.status='sending' AND r.created_at<now()-interval '1 minute' ORDER BY r.id LIMIT 5""")
    for row in stuck:
        # Один неисправный запуск не должен блокировать остальные: ошибка считается попыткой, после пяти запуск помечается `failed`.
        attempts = await connection.fetchval('UPDATE announcement_runs SET attempts=attempts+1 WHERE id=$1 RETURNING attempts', row['run_id'])
        try:
            await deliver_run(connection, row, row['run_id'])
        except Exception as error:
            LOGGER.error('Announcement run %s failed (%s), attempt %s', row['run_id'], type(error).__name__, attempts)
            if attempts >= 5:
                await connection.execute("UPDATE announcement_runs SET status='failed' WHERE id=$1", row['run_id'])
    due = await connection.fetch(
        "SELECT id FROM announcements WHERE status='active' AND next_run_at<=$1 ORDER BY next_run_at LIMIT $2", now, limit)
    count = 0
    for item in due:
        try:
            if await run_one(connection, item['id'], now):
                count += 1
        except Exception as error:                      # сбой одного объявления не мешает остальным; запуск повторится как «зависший»
            LOGGER.error('Announcement %s run failed (%s)', item['id'], type(error).__name__)
    return count


def describe(row: Any) -> str:
    """Краткое описание расписания для списка объявлений."""
    names = {1: 'пн', 2: 'вт', 3: 'ср', 4: 'чт', 5: 'пт', 6: 'сб', 7: 'вс'}
    clock = row['send_time'].strftime('%H:%M') if row['send_time'] else ''
    kind = row['kind']
    if kind == 'once':
        return 'однократно'
    if kind == 'daily':
        return f'ежедневно {clock}'
    if kind == 'weekly':
        return f"еженедельно {','.join(names[d] for d in row['days_of_week'])} {clock}"
    if kind == 'monthly':
        return f"ежемесячно {row['day_of_month']}-го {clock}"
    return f"каждые {row['interval_minutes']} мин"


# ----- Команды и меню администратора в боте -------------------------------------------------------------

def _split_command(raw: str) -> list[str]:
    return [part.strip() for part in raw.split('|', 2)]            # знак «|» допустим в тексте объявления


async def command(connection: Any, actor_id: int, chat: Any, raw: str) -> tuple[str, Any]:
    """Команда `/announce`. Форматы:
      `/announce <когда> | Заголовок | Текст` — создать (когда: now, once …, daily …, weekly …, monthly …, every …)
      `/announce list` · `/announce pause|resume|delete|run ID`
    Текст берётся из исходного сообщения, поэтому переводы строк сохраняются.
    """
    from aiogram.types import InlineKeyboardMarkup

    from app.services.accounts import require_role
    await require_role(connection, actor_id, 'editor')
    if chat['chat_type'] != 'private':
        raise UserError('private_only')
    ru = chat['ui_language'] == 'ru'
    words = raw.split()
    if not words or words[0] == 'list':
        return await listing(connection, chat)
    if words[0] in {'pause', 'resume', 'delete', 'run'} and len(words) == 2 and words[1].isdigit():
        ident = int(words[1])
        if words[0] == 'run':
            ok = await run_now(connection, ident)
        else:
            ok = await set_status(connection, ident, {'pause': 'paused', 'resume': 'active', 'delete': 'cancelled'}[words[0]])
        return ('Готово.' if ru else 'Done.') if ok else ('Не найдено.' if ru else 'Not found.'), InlineKeyboardMarkup(inline_keyboard=[])
    parts = _split_command(raw)
    if len(parts) != 3 or not parts[2]:
        raise UserError('invalid')
    schedule = parse_when(parts[0], chat['timezone'])
    row = await create(connection, actor_id, parts[1], parts[2], schedule)
    when = row['next_run_at'].astimezone(ZoneInfo(chat['timezone'])).strftime('%d.%m.%Y %H:%M')
    text = (f"✅ Объявление #{row['id']} создано ({describe(row)}). Ближайший запуск: {when} {chat['timezone']}." if ru else
            f"✅ Announcement #{row['id']} created. Next run: {when} {chat['timezone']}.")
    return text, InlineKeyboardMarkup(inline_keyboard=[])


async def run_now(connection: Any, announcement_id: int) -> bool:
    """Отправить объявление сейчас, не меняя расписание (отдельный запуск с уникальным моментом)."""
    row = await connection.fetchrow('SELECT * FROM announcements WHERE id=$1 AND status<>$2', announcement_id, 'cancelled')
    if not row:
        return False
    moment = datetime.now(UTC)
    run_id = await connection.fetchval(
        "INSERT INTO announcement_runs(announcement_id,scheduled_for,status) VALUES($1,$2,'sending') "
        "ON CONFLICT DO NOTHING RETURNING id", announcement_id, moment)
    if run_id:
        await deliver_run(connection, row, run_id)
    return bool(run_id)


async def listing(connection: Any, chat: Any) -> tuple[str, Any]:
    """Список объявлений (последние 15) с кнопками управления для активных."""
    from aiogram.types import InlineKeyboardMarkup

    from app.bot.common import button
    ru = chat['ui_language'] == 'ru'
    rows = await connection.fetch("SELECT * FROM announcements WHERE status<>'cancelled' ORDER BY id DESC LIMIT 15")
    cid = chat['telegram_chat_id']
    zone = ZoneInfo(chat['timezone'])
    lines = ['📣 <b>' + ('Объявления' if ru else 'Announcements') + '</b>']
    keyboard = []
    for row in rows:
        nxt = row['next_run_at'].astimezone(zone).strftime('%d.%m %H:%M') if row['next_run_at'] else '—'
        lines.append(f"#{row['id']} {escape(row['title'] or row['body'][:30])} · {describe(row)} · {row['status']} · {nxt} · ×{row['runs_done']}")
        toggle = ('⏸', f"ap{row['id']}") if row['status'] == 'active' else ('▶', f"ar{row['id']}")
        keyboard.append([button(f"{toggle[0]} #{row['id']}", 'adm', cid, toggle[1]),
                         button(f"📨 #{row['id']}", 'adm', cid, f"an{row['id']}"),
                         button(f"🗑 #{row['id']}", 'adm', cid, f"ad{row['id']}")])
    if not rows:
        lines.append('Пока нет.' if ru else 'None yet.')
    lines.append('\n' + ('Создать: <code>/announce weekly sat 10:00 | Заголовок | Текст</code>' if ru
                          else 'Create: <code>/announce weekly sat 10:00 | Title | Text</code>'))
    keyboard.append([button('◀ Меню' if ru else '◀ Menu', 'adm', cid, 'home')])
    return '\n'.join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard)


async def section(connection: Any, actor_id: int, chat: Any, value: str = '') -> tuple[str, Any]:
    """Раздел «Объявления» меню администратора и действия над объявлением (`ap` пауза, `ar` продолжить, `an` отправить сейчас, `ad` удалить)."""
    if value and value[:2] in {'ap', 'ar', 'an', 'ad'} and value[2:].isdigit():
        ident = int(value[2:])
        if value[:2] == 'an':
            await run_now(connection, ident)
        else:
            await set_status(connection, ident, {'ap': 'paused', 'ar': 'active', 'ad': 'cancelled'}[value[:2]])
    return await listing(connection, chat)
