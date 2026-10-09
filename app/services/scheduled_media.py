"""Prepare complete scheduled reading bundles before any Telegram submission."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from app.services import artwork, bible, illustrations, message_languages, speech
from app.services.i18n import ui_for_language
from app.services.scheduling import on_date

MODES = ('verse_of_day','topic_of_day','morning_verse','evening_verse')

# Shared by the speaker's selector and its final eligibility check. Preparing a
# scheduled card is sufficient authority to synthesize, but abandoned cards are not.
AUDIO_ELIGIBILITY = """(EXISTS(SELECT 1 FROM reading_cards c WHERE c.audio_id=a.id AND c.telegram_message_id IS NOT NULL)
 OR EXISTS(SELECT 1 FROM reading_card_tracks t JOIN reading_cards c ON c.id=t.card_id
 JOIN delivery_log d ON d.id=c.delivery_id
 JOIN scheduled_readings r ON r.delivery_id=d.id JOIN subscriptions s ON s.id=d.subscription_id
 JOIN telegram_chats ch ON ch.telegram_chat_id=d.telegram_chat_id
 WHERE t.audio_id=a.id AND r.state IN ('preparing','ready') AND d.status IN ('pending','retry')
 AND s.is_enabled AND ch.is_active AND s.revision=d.subscription_revision AND ch.revision=d.chat_revision))"""


async def prepare(connection, delivery_id, row, edition, sub, chat, day):
    """Freeze all offered languages/pages, keeping the initial selection intact."""
    from app.services import prayers
    delivery=await connection.fetchrow('SELECT * FROM delivery_log WHERE id=$1',delivery_id)
    chunks=message_languages.decoded(delivery['chunks'])
    cards=[part['card_id'] for part in chunks if isinstance(part,dict) and part.get('card_id')]
    if not cards:
        raise ValueError('Scheduled reading has no source-backed card')
    image_id=await artwork.enqueue(connection,row,edition,subscription=sub,day=day,slot=sub['mode'],variant='symbolic')
    prayer_at=await prayers.nearby_time(connection,sub,day)
    deadline=prayer_at if sub['mode'] in {'morning_verse','evening_verse'} and prayer_at>delivery['scheduled_for'] else delivery['scheduled_for']+timedelta(minutes=30)
    await connection.execute('''INSERT INTO scheduled_readings(delivery_id,image_id,deadline_at)
        VALUES($1,$2,$3) ON CONFLICT(delivery_id) DO NOTHING''',delivery_id,image_id,deadline)
    audio_settings=speech.for_chat(speech.SpeechSettings.from_env(),chat)
    if not audio_settings.enabled:
        raise ValueError('Scheduled readings require enabled audio')
    for card_id in cards:
        await connection.execute('UPDATE reading_cards SET prayer_at=$2,delivery_id=$3 WHERE id=$1',card_id,prayer_at,delivery_id)
        card=await connection.fetchrow('SELECT * FROM reading_cards WHERE id=$1',card_id)
        for translation in await message_languages.available(connection,card):
            html=await message_languages.render(connection,card,translation)
            locale=card['ui_language'] if translation['id']==card['source_translation_id'] else ui_for_language(translation['language_code']) or card['ui_language']
            for page,part in enumerate(message_languages.pages(html,chat.get('platform','telegram'))):
                text=speech.spoken_text(part,translation,locale)
                if not text:
                    raise ValueError('Empty prepared audio track')
                audio=await speech.ensure_audio(connection,text,translation['language_code'],audio_settings)
                await connection.execute('''INSERT INTO reading_card_tracks(card_id,translation_id,page,audio_id)
                    VALUES($1,$2,$3,$4) ON CONFLICT(card_id,translation_id,page) DO NOTHING''',card_id,translation['id'],page,audio)
    if sub['mode'] in {'morning_verse','evening_verse'}:
        await connection.execute('''INSERT INTO prayer_occurrences(subscription_id,subscription_revision,chat_revision,
            local_date,scheduled_for,image_id) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING''',
            sub['id'],sub['revision'],chat['revision'],day,on_date(day,sub['send_time'],sub['timezone']),image_id)


async def ready(connection, delivery):
    """Fail closed: never send a partial bundle or a misleading late reminder."""
    prepared=await connection.fetchrow('SELECT * FROM scheduled_readings WHERE delivery_id=$1',delivery['id'])
    if not prepared:
        return 'ready'
    if prepared['state'] in {'expired','cancelled'}:
        return prepared['state']
    if datetime.now(UTC)>=prepared['deadline_at']:
        from app.worker.delivery import commit_progress
        async with connection.transaction():
            await connection.execute("UPDATE scheduled_readings SET state='expired',error_code='media_deadline',updated_at=now() WHERE delivery_id=$1",delivery['id'])
            await connection.execute("UPDATE delivery_log SET status='skipped',error_code='media_deadline',updated_at=now() WHERE id=$1",delivery['id'])
            await connection.execute("INSERT INTO operator_events(action,details) VALUES('scheduled_media_expired',jsonb_build_object('delivery_id',$1::bigint))",delivery['id'])
            await commit_progress(connection,delivery)
        return 'expired'
    image=await connection.fetchrow("SELECT * FROM verse_illustrations WHERE id=$1 AND status='ready'",prepared['image_id'])
    chunks=json.loads(delivery['chunks']) if isinstance(delivery['chunks'],str) else delivery['chunks']
    cards=[part['card_id'] for part in chunks if isinstance(part,dict) and part.get('card_id')]
    if not image or not cards:
        return 'preparing_media'
    source=await illustrations.source_row(connection,image)
    edition=await bible.find_translation(connection,image['translation_id'])
    if not source or not edition or illustrations.identity(source,edition)[4]!=image['text_sha256']:
        await connection.execute("UPDATE delivery_log SET status='cancelled',error_code='source_changed' WHERE id=$1",delivery['id'])
        await connection.execute("UPDATE scheduled_readings SET state='cancelled',error_code='source_changed' WHERE delivery_id=$1",delivery['id'])
        return 'source_changed'
    for identifier in cards:
        card=await connection.fetchrow('SELECT * FROM reading_cards WHERE id=$1',identifier)
        if card['request_id']:
            request=await connection.fetchrow('SELECT * FROM illustration_requests WHERE id=$1',card['request_id'])
            if request and not await illustrations.valid_request_source(connection,request,image=image):
                await connection.execute("UPDATE delivery_log SET status='cancelled',error_code='source_changed' WHERE id=$1",delivery['id'])
                await connection.execute("UPDATE scheduled_readings SET state='cancelled',error_code='source_changed' WHERE delivery_id=$1",delivery['id'])
                return 'source_changed'
        counts=await connection.fetchrow('''SELECT count(*) AS total,count(*) FILTER(WHERE a.state='ready') AS ready
            FROM reading_card_tracks t JOIN reading_audio a ON a.id=t.audio_id WHERE t.card_id=$1''',identifier)
        if not counts['total'] or counts['total']!=counts['ready']:
            return 'preparing_media'
    async with connection.transaction():
        await connection.execute('UPDATE reading_cards SET image_id=$2 WHERE id=ANY($1::bigint[])',cards,image['id'])
        await connection.execute("UPDATE scheduled_readings SET state='ready',error_code=NULL,updated_at=now() WHERE delivery_id=$1",delivery['id'])
    return 'ready'
