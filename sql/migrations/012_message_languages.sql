-- Per-message selections never change destination preferences or reading progress.
CREATE TABLE reading_cards (
 id bigserial PRIMARY KEY,
 telegram_chat_id bigint NOT NULL REFERENCES telegram_chats(telegram_chat_id) ON DELETE CASCADE,
 telegram_message_id bigint CHECK (telegram_message_id IS NULL OR telegram_message_id>0),
 source_key text NOT NULL,
 source_translation_id bigint NOT NULL REFERENCES translations(id),
 selected_translation_id bigint NOT NULL REFERENCES translations(id),
 refs jsonb NOT NULL CHECK (jsonb_typeof(refs)='array' AND jsonb_array_length(refs)>0),
 original_html text NOT NULL,
 current_html text NOT NULL,
 ui_language text NOT NULL,
 image_id bigint REFERENCES verse_illustrations(id),
 request_id bigint REFERENCES illustration_requests(id),
 language_page integer NOT NULL DEFAULT 0 CHECK (language_page>=0),
 text_page integer NOT NULL DEFAULT 0 CHECK (text_page>=0),
 title_key text,
 revision integer NOT NULL DEFAULT 0,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(telegram_chat_id,source_key),
 UNIQUE(telegram_chat_id,telegram_message_id)
);
CREATE INDEX reading_cards_request ON reading_cards(request_id) WHERE request_id IS NOT NULL;
