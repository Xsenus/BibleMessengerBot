-- Общие медиафайлы (картинки объявлений и скопированных постов), объявления и расписания их отправки.
CREATE TABLE media_assets (
    id bigserial PRIMARY KEY,
    kind text NOT NULL DEFAULT 'image' CHECK (kind IN ('image')),
    mime_type text NOT NULL CHECK (mime_type IN ('image/png', 'image/jpeg', 'image/webp')),
    data bytea NOT NULL,
    sha256 text NOT NULL UNIQUE,
    origin text NOT NULL DEFAULT 'upload',
    telegram_file_id text,
    telegram_bot_id bigint,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Кэш загрузок в MAX теперь хранит и вложения-объявления.
ALTER TABLE max_media DROP CONSTRAINT IF EXISTS max_media_kind_check;
ALTER TABLE max_media ADD CONSTRAINT max_media_kind_check CHECK (kind IN ('image', 'audio', 'asset'));

CREATE TABLE announcements (
    id bigserial PRIMARY KEY,
    title text NOT NULL DEFAULT '' CHECK (length(title) <= 200),
    body text NOT NULL CHECK (length(body) BETWEEN 1 AND 3500),
    asset_id bigint REFERENCES media_assets(id) ON DELETE SET NULL,
    audience jsonb NOT NULL DEFAULT '{}'::jsonb,        -- {"platforms": [...], "chat_types": [...]}
    created_by bigint,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'finished', 'cancelled')),
    -- Расписание
    kind text NOT NULL CHECK (kind IN ('once', 'daily', 'weekly', 'monthly', 'interval')),
    send_time time,
    timezone text NOT NULL DEFAULT 'UTC',
    days_of_week integer[] NOT NULL DEFAULT '{1,2,3,4,5,6,7}',
    day_of_month integer CHECK (day_of_month BETWEEN 1 AND 31),
    interval_minutes integer CHECK (interval_minutes >= 5),
    start_at timestamptz,                               -- первый возможный запуск (для once и interval)
    end_at timestamptz,                                 -- после этого момента запусков нет
    max_runs integer CHECK (max_runs >= 1),             -- NULL — без ограничения числа повторов
    runs_done integer NOT NULL DEFAULT 0,
    next_run_at timestamptz,
    misfire_grace_minutes integer NOT NULL DEFAULT 60,  -- опоздавший дольше этого запуск пропускается
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_announcements_due ON announcements(next_run_at) WHERE status = 'active';

CREATE TABLE announcement_runs (
    id bigserial PRIMARY KEY,
    announcement_id bigint NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
    scheduled_for timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'sending' CHECK (status IN ('sending', 'done', 'skipped_late')),
    recipients integer NOT NULL DEFAULT 0,
    queued integer NOT NULL DEFAULT 0,
    skipped integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (announcement_id, scheduled_for)
);
