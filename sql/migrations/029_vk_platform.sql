-- ВКонтакте: третья платформа поверх общего корпуса, очереди доставки и административных функций.
ALTER TABLE telegram_users DROP CONSTRAINT IF EXISTS telegram_users_platform_check;
ALTER TABLE telegram_users ADD CONSTRAINT telegram_users_platform_check CHECK (platform IN ('telegram', 'max', 'vk'));
ALTER TABLE telegram_chats DROP CONSTRAINT IF EXISTS telegram_chats_platform_check;
ALTER TABLE telegram_chats ADD CONSTRAINT telegram_chats_platform_check CHECK (platform IN ('telegram', 'max', 'vk'));
ALTER TABLE reading_cards DROP CONSTRAINT IF EXISTS reading_cards_platform_check;
ALTER TABLE reading_cards ADD CONSTRAINT reading_cards_platform_check CHECK (platform IN ('telegram', 'max', 'vk'));
ALTER TABLE platform_identities DROP CONSTRAINT IF EXISTS platform_identities_platform_check;
ALTER TABLE platform_identities ADD CONSTRAINT platform_identities_platform_check CHECK (platform IN ('max', 'vk'));

ALTER TABLE usage_events DROP CONSTRAINT IF EXISTS usage_events_platform_check;
ALTER TABLE usage_events ADD CONSTRAINT usage_events_platform_check CHECK (platform IN ('telegram', 'max', 'vk'));

-- Входящие события Long Poll записываются до подтверждения, дубликат события имеет одну строку.
CREATE TABLE vk_inbox (
    id bigserial PRIMARY KEY,
    event_key text NOT NULL UNIQUE,
    payload jsonb NOT NULL,
    chat_key text NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'done', 'failed', 'uncertain')),
    attempts integer NOT NULL DEFAULT 0,
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_vk_inbox_pending ON vk_inbox(id) WHERE status = 'pending';
CREATE UNIQUE INDEX ux_vk_processing_chat ON vk_inbox(chat_key) WHERE status = 'processing';
CREATE INDEX ix_vk_inbox_done ON vk_inbox(updated_at) WHERE status = 'done';

-- Вложения ВКонтакте (фото, документы), загруженные от имени сообщества, переиспользуются.
CREATE TABLE vk_media (
    group_id bigint NOT NULL,
    kind text NOT NULL CHECK (kind IN ('image', 'audio', 'asset')),
    asset_id bigint NOT NULL,
    attachment text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (group_id, kind, asset_id)
);
CREATE TABLE vk_rate_limits (
    key text PRIMARY KEY,
    next_allowed timestamptz NOT NULL DEFAULT now()
);
