-- Источники контента: каналы, группы и сообщества, где состоит бот; их посты можно копировать пользователям.
CREATE TABLE content_sources (
    id bigserial PRIMARY KEY,
    platform text NOT NULL CHECK (platform IN ('telegram', 'max', 'vk')),
    external_id bigint NOT NULL,                       -- внешний ID канала/группы/владельца стены
    title text NOT NULL DEFAULT '',
    chat_id bigint,                                    -- внутренний ID назначения (если бот в нём зарегистрирован)
    mode text NOT NULL DEFAULT 'approve' CHECK (mode IN ('capture', 'approve', 'auto')),
    audience jsonb NOT NULL DEFAULT '{}'::jsonb,
    prefix text NOT NULL DEFAULT '' CHECK (length(prefix) <= 200),
    min_length integer NOT NULL DEFAULT 1 CHECK (min_length >= 1),
    is_enabled boolean NOT NULL DEFAULT false,         -- новые источники выключены, пока администратор не включит
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (platform, external_id)
);

CREATE TABLE source_posts (
    id bigserial PRIMARY KEY,
    source_id bigint NOT NULL REFERENCES content_sources(id) ON DELETE CASCADE,
    external_id text NOT NULL,
    body text NOT NULL,
    asset_id bigint REFERENCES media_assets(id) ON DELETE SET NULL,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    posted_at timestamptz NOT NULL DEFAULT now(),
    status text NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'published', 'ignored')),
    published_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, external_id)
);
CREATE INDEX ix_source_posts_status ON source_posts(status, created_at DESC);
