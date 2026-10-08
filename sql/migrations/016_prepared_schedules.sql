-- Additive preparation state; historical sends and paid attempts are preserved.
ALTER TABLE reading_cards ADD COLUMN prayer_at timestamptz;
ALTER TABLE reading_cards ADD COLUMN delivery_id bigint REFERENCES delivery_log(id) ON DELETE SET NULL;
CREATE INDEX reading_cards_delivery ON reading_cards(delivery_id) WHERE delivery_id IS NOT NULL;
CREATE TABLE scheduled_readings (
 delivery_id bigint PRIMARY KEY REFERENCES delivery_log(id) ON DELETE CASCADE,
 image_id bigint NOT NULL REFERENCES verse_illustrations(id),
 state text NOT NULL DEFAULT 'preparing' CHECK(state IN ('preparing','ready','expired','cancelled')),
 deadline_at timestamptz NOT NULL,
 error_code text,
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE reading_card_tracks (
 card_id bigint NOT NULL REFERENCES reading_cards(id) ON DELETE CASCADE,
 translation_id bigint NOT NULL REFERENCES translations(id),
 page integer NOT NULL CHECK(page>=0),
 audio_id bigint NOT NULL REFERENCES reading_audio(id),
 PRIMARY KEY(card_id,translation_id,page)
);
CREATE INDEX reading_card_tracks_audio ON reading_card_tracks(audio_id);
CREATE TABLE prayer_occurrences (
 id bigserial PRIMARY KEY,
 subscription_id bigint NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
 subscription_revision integer NOT NULL,
 chat_revision integer NOT NULL,
 local_date date NOT NULL,
 scheduled_for timestamptz NOT NULL,
 image_id bigint REFERENCES verse_illustrations(id),
 delivery_id bigint REFERENCES delivery_log(id),
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','ready','expired','cancelled')),
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(subscription_id,subscription_revision,local_date)
);
CREATE TABLE prayer_briefs (
 id bigserial PRIMARY KEY,
 local_date date NOT NULL,
 timezone text NOT NULL,
 slot text NOT NULL CHECK(slot IN ('morning_verse','evening_verse')),
 locale text NOT NULL,
 prayer_text text NOT NULL,
 generator text NOT NULL,
 news_snapshot jsonb NOT NULL DEFAULT '[]'::jsonb,
 generated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(local_date,timezone,slot,locale)
);

-- Existing devotional send_time is the prayer time. Re-anchor only future
-- occurrences without an in-flight/uncertain checkpoint; never replay history.
UPDATE subscriptions s SET next_run_at=(date_trunc('hour',s.next_run_at AT TIME ZONE s.timezone) AT TIME ZONE s.timezone)
WHERE s.mode IN ('morning_verse','evening_verse') AND s.is_enabled AND s.next_run_at>now()
AND (date_trunc('hour',s.next_run_at AT TIME ZONE s.timezone) AT TIME ZONE s.timezone)>now()
AND NOT EXISTS(SELECT 1 FROM delivery_log d WHERE d.subscription_id=s.id AND d.status IN ('pending','retry','sending','uncertain'));
