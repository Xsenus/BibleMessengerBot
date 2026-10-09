-- Store the transport on the immutable source card, including on legacy inserts.
ALTER TABLE reading_cards ADD COLUMN platform text NOT NULL DEFAULT 'telegram'
    CHECK (platform IN ('telegram','max'));
ALTER TABLE reading_cards ADD COLUMN audio_offered_id bigint REFERENCES reading_audio(id) ON DELETE SET NULL;
UPDATE reading_cards r SET platform=c.platform FROM telegram_chats c
    WHERE c.telegram_chat_id=r.telegram_chat_id;
CREATE FUNCTION reading_card_platform() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.platform := COALESCE((SELECT platform FROM telegram_chats
        WHERE telegram_chat_id=NEW.telegram_chat_id),'telegram');
    RETURN NEW;
END;
$$;
CREATE TRIGGER reading_card_platform BEFORE INSERT OR UPDATE OF telegram_chat_id,platform
    ON reading_cards FOR EACH ROW EXECUTE FUNCTION reading_card_platform();

ALTER TABLE max_inbox ADD COLUMN chat_key text NOT NULL DEFAULT 'legacy';
UPDATE max_inbox SET chat_key=COALESCE(payload->>'chat_id',payload->'message'->'recipient'->>'chat_id',
    'user:'||(payload->'callback'->'user'->>'user_id'),'legacy:'||id::text);
CREATE UNIQUE INDEX ux_max_processing_chat ON max_inbox(chat_key) WHERE status='processing';
