CREATE TABLE reading_schedule_preferences (
    telegram_chat_id bigint PRIMARY KEY REFERENCES telegram_chats(telegram_chat_id) ON DELETE CASCADE,
    initialized boolean NOT NULL DEFAULT false,
    paused_modes text[] NOT NULL DEFAULT '{}',
    unsubscribed_modes text[] NOT NULL DEFAULT '{}',
    offers_muted boolean NOT NULL DEFAULT false,
    offer_at timestamptz,
    offer_delivery_id bigint REFERENCES delivery_log(id) ON DELETE SET NULL,
    CHECK (paused_modes <@ ARRAY['morning_verse','verse_of_day','evening_verse']),
    CHECK (unsubscribed_modes <@ ARRAY['morning_verse','verse_of_day','evening_verse'])
);

CREATE INDEX reading_schedule_due_offer
    ON reading_schedule_preferences (offer_at) WHERE offer_at IS NOT NULL;
