-- Слежение за видеоканалами (YouTube, RuTube, ВКонтакте): новые видео и пересылка ссылок пользователям.
CREATE TABLE video_feeds (
    id bigserial PRIMARY KEY,
    platform text NOT NULL CHECK (platform IN ('youtube', 'rutube', 'vk')),
    handle text NOT NULL CHECK (length(handle) BETWEEN 1 AND 100),   -- ID канала YouTube, автора RuTube или владельца видео VK
    title text NOT NULL DEFAULT '',
    is_enabled boolean NOT NULL DEFAULT true,
    notify boolean NOT NULL DEFAULT false,                           -- пересылать ссылки пользователям
    audience jsonb NOT NULL DEFAULT '{}'::jsonb,
    template text NOT NULL DEFAULT '' CHECK (length(template) <= 500),
    interval_minutes integer NOT NULL DEFAULT 30 CHECK (interval_minutes BETWEEN 5 AND 1440),
    baselined boolean NOT NULL DEFAULT false,                        -- первый опрос только запоминает уже вышедшие видео
    next_check_at timestamptz NOT NULL DEFAULT now(),
    last_checked_at timestamptz,
    error_count integer NOT NULL DEFAULT 0,
    last_error text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (platform, handle)
);
CREATE INDEX ix_video_feeds_due ON video_feeds(next_check_at) WHERE is_enabled;

CREATE TABLE video_items (
    id bigserial PRIMARY KEY,
    feed_id bigint NOT NULL REFERENCES video_feeds(id) ON DELETE CASCADE,
    external_id text NOT NULL,
    title text NOT NULL DEFAULT '',
    url text NOT NULL,
    published_at timestamptz,
    status text NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'notified', 'skipped')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (feed_id, external_id)
);
