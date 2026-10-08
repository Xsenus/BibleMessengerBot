CREATE TABLE reading_audio (
 id bigserial PRIMARY KEY,
 cache_key text NOT NULL UNIQUE,
 language_code text NOT NULL,
 source_text text NOT NULL CHECK (length(source_text)>0),
 provider_mode text NOT NULL,
 state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','running','ready','retry','failed','uncertain')),
 provider text,
 voice text,
 audio_data bytea CHECK (audio_data IS NULL OR octet_length(audio_data)<=20971520),
 duration integer,
 telegram_file_id text,
 telegram_bot_id bigint,
 attempts integer NOT NULL DEFAULT 0,
 retry_at timestamptz,
 error_code text,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 CHECK (state<>'ready' OR (audio_data IS NOT NULL AND duration>0))
);
CREATE INDEX reading_audio_due ON reading_audio(retry_at,id) WHERE state IN ('queued','retry');
ALTER TABLE reading_cards ADD COLUMN audio_id bigint REFERENCES reading_audio(id);
ALTER TABLE reading_cards ADD COLUMN audio_sent_id bigint REFERENCES reading_audio(id);
CREATE INDEX reading_cards_audio ON reading_cards(audio_id) WHERE audio_id IS NOT NULL;
CREATE TABLE audio_generation_attempts (
 id bigserial PRIMARY KEY,
 audio_id bigint NOT NULL REFERENCES reading_audio(id),
 characters integer NOT NULL CHECK (characters>0),
 state text NOT NULL CHECK (state IN ('running','ready','failed','uncertain')),
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now()
);
