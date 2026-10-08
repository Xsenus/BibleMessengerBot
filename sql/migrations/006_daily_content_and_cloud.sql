ALTER TABLE subscriptions DROP CONSTRAINT subscriptions_mode_check;
ALTER TABLE subscriptions ADD CONSTRAINT subscriptions_mode_check CHECK
 (mode IN ('sequential','verse_of_day','topic_of_day','reading_plan','morning_verse','evening_verse'));

CREATE TABLE daily_verse_selections (
 id bigserial PRIMARY KEY,
 telegram_chat_id bigint NOT NULL REFERENCES telegram_chats(telegram_chat_id) ON DELETE CASCADE,
 translation_id bigint NOT NULL REFERENCES translations(id) ON DELETE CASCADE,
 local_date date NOT NULL,
 slot text NOT NULL CHECK(slot IN ('morning_verse','evening_verse')),
 book_code text NOT NULL, chapter integer NOT NULL, verse integer NOT NULL,
 verse_text text NOT NULL, text_sha256 text NOT NULL,
 theme text NOT NULL, prompt_variant text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(telegram_chat_id,translation_id,local_date,slot),
 FOREIGN KEY(translation_id,book_code,chapter,verse) REFERENCES verses(translation_id,book_code,chapter,verse) ON DELETE CASCADE
);
CREATE INDEX daily_verse_recent ON daily_verse_selections(telegram_chat_id,local_date DESC);

ALTER TABLE verse_illustrations ADD COLUMN s3_key text;
ALTER TABLE verse_illustrations ADD COLUMN s3_sha256 text;
ALTER TABLE verse_illustrations ADD COLUMN s3_backed_up_at timestamptz;

CREATE TABLE image_generation_jobs (
 id bigserial PRIMARY KEY,
 image_id bigint NOT NULL UNIQUE REFERENCES verse_illustrations(id) ON DELETE CASCADE,
 state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','ready','retry','failed','uncertain')),
 prompt text NOT NULL, prompt_variant text NOT NULL, model text NOT NULL,
 attempts integer NOT NULL DEFAULT 0, retry_at timestamptz,
 request_id text, error_code text, usage jsonb,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE image_generation_attempts (
 id bigserial PRIMARY KEY, job_id bigint NOT NULL REFERENCES image_generation_jobs(id) ON DELETE CASCADE,
 reserved_usd numeric(10,4) NOT NULL CHECK(reserved_usd>0),
 state text NOT NULL DEFAULT 'reserved' CHECK(state IN ('reserved','succeeded','failed','uncertain')),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX generation_budget_date ON image_generation_attempts(created_at);
CREATE TABLE image_generation_targets (
 image_id bigint NOT NULL REFERENCES verse_illustrations(id) ON DELETE CASCADE,
 subscription_id bigint NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
 local_date date NOT NULL, scheduled_for timestamptz NOT NULL,
 PRIMARY KEY(image_id,subscription_id,local_date)
);
CREATE TABLE cloud_backup_receipts (
 id bigserial PRIMARY KEY, kind text NOT NULL CHECK(kind IN ('database','image')),
 object_key text NOT NULL UNIQUE, sha256 text NOT NULL, byte_count bigint NOT NULL,
 verified_at timestamptz NOT NULL DEFAULT now()
);
