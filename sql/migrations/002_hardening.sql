-- Additive upgrade from the actual 1.1.0 schema. Run with the migration lock.
ALTER TABLE telegram_chats ADD COLUMN IF NOT EXISTS ui_language text NOT NULL DEFAULT 'ru';
ALTER TABLE telegram_chats ADD COLUMN IF NOT EXISTS bible_language_code text;
ALTER TABLE telegram_chats ADD COLUMN IF NOT EXISTS revision integer NOT NULL DEFAULT 1;
ALTER TABLE telegram_chats ADD COLUMN IF NOT EXISTS message_thread_id integer CHECK(message_thread_id > 0);
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS revision integer NOT NULL DEFAULT 1;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS plan_day integer NOT NULL DEFAULT 0 CHECK(plan_day >= 0);
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS completed boolean NOT NULL DEFAULT false;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS topic_code text REFERENCES topics(code);
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS retry_at timestamptz;
ALTER TABLE translations ADD COLUMN IF NOT EXISTS audit_status text NOT NULL DEFAULT 'unverified';
ALTER TABLE translations ADD COLUMN IF NOT EXISTS source_revision text;
ALTER TABLE translations ADD COLUMN IF NOT EXISTS reference_sha256 text;
ALTER TABLE translations ADD COLUMN IF NOT EXISTS validation_report jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE translations ADD COLUMN IF NOT EXISTS canonical_66_complete boolean NOT NULL DEFAULT false;
ALTER TABLE translations ADD COLUMN IF NOT EXISTS nt_complete boolean NOT NULL DEFAULT false;
ALTER TABLE verses ADD COLUMN IF NOT EXISTS verse_end integer;
ALTER TABLE verses ADD COLUMN IF NOT EXISTS ordinal integer;
CREATE UNIQUE INDEX IF NOT EXISTS ux_verse_ordinal ON verses(translation_id, ordinal) WHERE ordinal IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_visible_verses ON verses(translation_id, source_line) WHERE text <> '' AND NOT is_range_continuation;
CREATE TABLE IF NOT EXISTS translation_chapters (
 translation_id bigint NOT NULL REFERENCES translations(id) ON DELETE CASCADE,
 book_code text NOT NULL REFERENCES books(code), chapter integer NOT NULL CHECK(chapter > 0),
 position integer NOT NULL CHECK(position > 0), verse_count integer NOT NULL CHECK(verse_count > 0),
 PRIMARY KEY(translation_id, book_code, chapter), UNIQUE(translation_id, position)
);
CREATE TABLE IF NOT EXISTS chat_reading_progress (
 telegram_chat_id bigint NOT NULL REFERENCES telegram_chats(telegram_chat_id) ON DELETE CASCADE,
 translation_id bigint NOT NULL REFERENCES translations(id) ON DELETE CASCADE,
 book_code text REFERENCES books(code), chapter integer,
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(telegram_chat_id, translation_id)
);
-- Preserve personal progress in the shared per-destination mechanism.
INSERT INTO chat_reading_progress(telegram_chat_id, translation_id, book_code, chapter)
 SELECT p.telegram_user_id,p.translation_id,p.book_code,p.chapter FROM reading_progress p
 JOIN telegram_chats c ON c.telegram_chat_id=p.telegram_user_id AND c.chat_type='private'
 WHERE p.plan_code='sequential' ON CONFLICT DO NOTHING;
ALTER TABLE delivery_log DROP CONSTRAINT IF EXISTS delivery_log_status_check;
ALTER TABLE delivery_log ADD CONSTRAINT delivery_log_status_check
 CHECK(status IN ('pending','sending','sent','retry','failed','skipped','uncertain','cancelled'));
ALTER TABLE delivery_log ADD COLUMN IF NOT EXISTS chunks jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE delivery_log ADD COLUMN IF NOT EXISTS next_chunk integer NOT NULL DEFAULT 0 CHECK(next_chunk >= 0);
ALTER TABLE delivery_log ADD COLUMN IF NOT EXISTS sending_chunk integer;
ALTER TABLE delivery_log ADD COLUMN IF NOT EXISTS progress jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE delivery_log ADD COLUMN IF NOT EXISTS subscription_revision integer;
ALTER TABLE delivery_log ADD COLUMN IF NOT EXISTS chat_revision integer NOT NULL DEFAULT 1;
ALTER TABLE delivery_log ADD COLUMN IF NOT EXISTS ui_language text NOT NULL DEFAULT 'ru';
ALTER TABLE delivery_log ADD COLUMN IF NOT EXISTS retry_at timestamptz;
ALTER TABLE delivery_log ADD COLUMN IF NOT EXISTS message_thread_id integer;
-- The old sender did not checkpoint individual messages. Never automatically replay those jobs.
UPDATE delivery_log SET status='uncertain',error_code='legacy_delivery_requires_review',updated_at=now()
 WHERE status IN ('sending','retry');
UPDATE subscriptions SET is_enabled=false,locked_at=NULL,lock_token=NULL
 WHERE id IN (SELECT subscription_id FROM delivery_log WHERE status='uncertain');
CREATE UNIQUE INDEX IF NOT EXISTS ux_manual_outstanding ON delivery_log(telegram_chat_id)
 WHERE subscription_id IS NULL AND mode='manual' AND status IN ('pending','sending','retry','uncertain');
CREATE INDEX IF NOT EXISTS ix_delivery_pending ON delivery_log(retry_at,id)
 WHERE status IN ('pending','retry');
CREATE TABLE IF NOT EXISTS operator_events (
 id bigserial PRIMARY KEY, actor_id bigint, chat_id bigint, action text NOT NULL,
 details jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS telegram_rate_limits (
 key text PRIMARY KEY, next_allowed timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS service_heartbeats (
 service text PRIMARY KEY, last_seen timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_single_owner ON telegram_users((is_owner)) WHERE is_owner=true;

ALTER TABLE delivery_log ADD COLUMN IF NOT EXISTS consecutive_failures integer NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS ix_delivery_outstanding_subscription ON delivery_log(subscription_id) WHERE status IN ('pending','retry','sending','uncertain');
-- Old pending records have no immutable content, so they cannot be resumed safely.
UPDATE delivery_log SET status='cancelled',error_code='legacy_empty_payload' WHERE status='pending' AND chunks='[]'::jsonb;
