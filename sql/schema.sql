BEGIN;

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS app_settings (
    key text PRIMARY KEY,
    value text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS languages (
    id bigserial PRIMARY KEY,
    code text NOT NULL UNIQUE,
    name text NOT NULL,
    english_name text NOT NULL,
    script text,
    text_direction text NOT NULL DEFAULT 'ltr' CHECK (text_direction IN ('ltr', 'rtl')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS translations (
    id bigserial PRIMARY KEY,
    language_id bigint NOT NULL REFERENCES languages(id) ON DELETE RESTRICT,
    source_name text NOT NULL,
    source_translation_id text NOT NULL,
    title text NOT NULL,
    short_title text,
    description text,
    license_type text NOT NULL,
    license_version text,
    license_url text,
    copyright_notice text,
    copyright_holder text,
    copyright_years text,
    translated_by text,
    publication_url text,
    source_file_url text NOT NULL,
    source_sha256 text NOT NULL,
    source_updated_at date,
    imported_at timestamptz NOT NULL DEFAULT now(),
    is_active boolean NOT NULL DEFAULT true,
    redistributable boolean NOT NULL DEFAULT true,
    downloadable boolean NOT NULL DEFAULT true,
    coverage text NOT NULL DEFAULT 'unknown' CHECK (coverage IN ('full', 'ot', 'nt', 'partial', 'unknown')),
    ot_books integer NOT NULL DEFAULT 0,
    nt_books integer NOT NULL DEFAULT 0,
    dc_books integer NOT NULL DEFAULT 0,
    book_count integer NOT NULL DEFAULT 0,
    verse_count integer NOT NULL DEFAULT 0,
    nonempty_verse_count integer NOT NULL DEFAULT 0,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (source_name, source_translation_id)
);

CREATE INDEX IF NOT EXISTS ix_translations_language_active
    ON translations(language_id, is_active);

CREATE TABLE IF NOT EXISTS books (
    code text PRIMARY KEY,
    canonical_order integer NOT NULL UNIQUE,
    testament text NOT NULL CHECK (testament IN ('OT', 'NT', 'DC')),
    default_name text NOT NULL
);

CREATE TABLE IF NOT EXISTS book_names (
    book_code text NOT NULL REFERENCES books(code) ON DELETE CASCADE,
    language_code text NOT NULL,
    name text NOT NULL,
    short_name text,
    PRIMARY KEY (book_code, language_code)
);

CREATE TABLE IF NOT EXISTS verses (
    translation_id bigint NOT NULL REFERENCES translations(id) ON DELETE CASCADE,
    book_code text NOT NULL REFERENCES books(code) ON DELETE RESTRICT,
    chapter integer NOT NULL CHECK (chapter > 0),
    verse integer NOT NULL CHECK (verse > 0),
    text text NOT NULL,
    is_range_continuation boolean NOT NULL DEFAULT false,
    source_line integer NOT NULL CHECK (source_line > 0),
    search_text text GENERATED ALWAYS AS (lower(text)) STORED,
    PRIMARY KEY (translation_id, book_code, chapter, verse)
);

CREATE INDEX IF NOT EXISTS ix_verses_reference
    ON verses(book_code, chapter, verse, translation_id);
CREATE INDEX IF NOT EXISTS ix_verses_translation_book
    ON verses(translation_id, book_code, chapter, verse);
CREATE INDEX IF NOT EXISTS ix_verses_search_trgm
    ON verses USING gin(search_text gin_trgm_ops);

CREATE TABLE IF NOT EXISTS telegram_users (
    telegram_user_id bigint PRIMARY KEY,
    username text,
    first_name text,
    last_name text,
    telegram_language_code text,
    timezone text NOT NULL DEFAULT 'Europe/Amsterdam',
    default_translation_id bigint REFERENCES translations(id) ON DELETE SET NULL,
    is_owner boolean NOT NULL DEFAULT false,
    is_blocked boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS telegram_chats (
    telegram_chat_id bigint PRIMARY KEY,
    chat_type text NOT NULL CHECK (chat_type IN ('private', 'group', 'supergroup', 'channel')),
    title text,
    username text,
    timezone text NOT NULL DEFAULT 'Europe/Amsterdam',
    default_translation_id bigint REFERENCES translations(id) ON DELETE SET NULL,
    registered_by bigint REFERENCES telegram_users(telegram_user_id) ON DELETE SET NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id bigserial PRIMARY KEY,
    telegram_chat_id bigint NOT NULL REFERENCES telegram_chats(telegram_chat_id) ON DELETE CASCADE,
    created_by bigint REFERENCES telegram_users(telegram_user_id) ON DELETE SET NULL,
    translation_id bigint NOT NULL REFERENCES translations(id) ON DELETE RESTRICT,
    mode text NOT NULL CHECK (mode IN ('sequential', 'verse_of_day', 'topic_of_day', 'reading_plan')),
    send_time time NOT NULL DEFAULT '09:00',
    timezone text NOT NULL DEFAULT 'Europe/Amsterdam',
    days_of_week smallint[] NOT NULL DEFAULT ARRAY[1,2,3,4,5,6,7]::smallint[],
    plan_code text,
    chapters_per_delivery integer NOT NULL DEFAULT 1 CHECK (chapters_per_delivery BETWEEN 1 AND 20),
    current_book_code text REFERENCES books(code) ON DELETE SET NULL,
    current_chapter integer,
    current_verse integer,
    next_run_at timestamptz,
    last_run_at timestamptz,
    locked_at timestamptz,
    lock_token uuid,
    is_enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (telegram_chat_id, mode)
);

CREATE INDEX IF NOT EXISTS ix_subscriptions_due
    ON subscriptions(next_run_at)
    WHERE is_enabled = true;

CREATE TABLE IF NOT EXISTS reading_progress (
    telegram_user_id bigint NOT NULL REFERENCES telegram_users(telegram_user_id) ON DELETE CASCADE,
    translation_id bigint NOT NULL REFERENCES translations(id) ON DELETE CASCADE,
    plan_code text NOT NULL DEFAULT 'sequential',
    book_code text REFERENCES books(code) ON DELETE SET NULL,
    chapter integer,
    verse integer,
    completed_references jsonb NOT NULL DEFAULT '[]'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (telegram_user_id, translation_id, plan_code)
);

CREATE TABLE IF NOT EXISTS reading_plans (
    code text PRIMARY KEY,
    name text NOT NULL,
    description text NOT NULL,
    duration_days integer NOT NULL CHECK (duration_days > 0),
    language_code text NOT NULL DEFAULT 'ru',
    is_active boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS reading_plan_items (
    plan_code text NOT NULL REFERENCES reading_plans(code) ON DELETE CASCADE,
    day_number integer NOT NULL CHECK (day_number > 0),
    position integer NOT NULL DEFAULT 1,
    book_code text NOT NULL REFERENCES books(code) ON DELETE RESTRICT,
    chapter_from integer NOT NULL CHECK (chapter_from > 0),
    verse_from integer,
    chapter_to integer NOT NULL CHECK (chapter_to > 0),
    verse_to integer,
    PRIMARY KEY (plan_code, day_number, position)
);

CREATE TABLE IF NOT EXISTS topics (
    code text PRIMARY KEY,
    title_ru text NOT NULL,
    title_en text NOT NULL,
    description_ru text,
    is_active boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS topic_references (
    topic_code text NOT NULL REFERENCES topics(code) ON DELETE CASCADE,
    book_code text NOT NULL REFERENCES books(code) ON DELETE CASCADE,
    chapter integer NOT NULL CHECK (chapter > 0),
    verse_from integer NOT NULL CHECK (verse_from > 0),
    verse_to integer NOT NULL CHECK (verse_to >= verse_from),
    weight integer NOT NULL DEFAULT 100,
    PRIMARY KEY (topic_code, book_code, chapter, verse_from, verse_to)
);

CREATE TABLE IF NOT EXISTS delivery_log (
    id bigserial PRIMARY KEY,
    subscription_id bigint REFERENCES subscriptions(id) ON DELETE SET NULL,
    telegram_chat_id bigint NOT NULL,
    translation_id bigint REFERENCES translations(id) ON DELETE SET NULL,
    mode text NOT NULL,
    scheduled_for timestamptz NOT NULL,
    payload_key text NOT NULL,
    payload_preview text,
    telegram_message_ids bigint[],
    status text NOT NULL CHECK (status IN ('pending', 'sending', 'sent', 'retry', 'failed', 'skipped')),
    attempt_count integer NOT NULL DEFAULT 0,
    error_code text,
    error_message text,
    sent_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (telegram_chat_id, payload_key)
);

CREATE INDEX IF NOT EXISTS ix_delivery_log_status
    ON delivery_log(status, scheduled_for);

CREATE TABLE IF NOT EXISTS import_runs (
    id bigserial PRIMARY KEY,
    profile text NOT NULL,
    source_name text NOT NULL,
    status text NOT NULL CHECK (status IN ('running', 'succeeded', 'partial', 'failed')),
    selected_count integer NOT NULL DEFAULT 0,
    imported_count integer NOT NULL DEFAULT 0,
    skipped_count integer NOT NULL DEFAULT 0,
    failed_count integer NOT NULL DEFAULT 0,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

CREATE TABLE IF NOT EXISTS import_failures (
    id bigserial PRIMARY KEY,
    import_run_id bigint NOT NULL REFERENCES import_runs(id) ON DELETE CASCADE,
    source_translation_id text,
    stage text NOT NULL,
    error_message text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

COMMIT;
