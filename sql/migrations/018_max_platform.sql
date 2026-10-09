-- MAX shares the corpus, rendering, media budgets and outbox, but never Telegram identities.
-- Existing primary keys and message checkpoints remain unchanged.
CREATE SEQUENCE IF NOT EXISTS platform_identity_seq AS bigint
    START WITH -4000000000000000 INCREMENT BY -1 MINVALUE -9000000000000000000;

ALTER TABLE telegram_users ADD COLUMN IF NOT EXISTS platform text NOT NULL DEFAULT 'telegram'
    CHECK (platform IN ('telegram','max'));
ALTER TABLE telegram_chats ADD COLUMN IF NOT EXISTS platform text NOT NULL DEFAULT 'telegram'
    CHECK (platform IN ('telegram','max'));

CREATE TABLE IF NOT EXISTS platform_identities (
    platform text NOT NULL CHECK (platform='max'),
    kind text NOT NULL CHECK (kind IN ('user','chat')),
    external_id bigint NOT NULL,
    internal_id bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (platform,kind,external_id),
    UNIQUE (platform,kind,internal_id)
);

-- String MAX message IDs are represented by durable positive local checkpoints.
CREATE TABLE IF NOT EXISTS max_messages (
    id bigserial PRIMARY KEY,
    bot_id bigint NOT NULL,
    chat_id bigint NOT NULL REFERENCES telegram_chats(telegram_chat_id) ON DELETE CASCADE,
    external_id text NOT NULL CHECK (length(external_id) BETWEEN 1 AND 256),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (bot_id,chat_id,external_id)
);

-- Upload tokens are specific to this platform and bot; Telegram file IDs stay intact.
CREATE TABLE IF NOT EXISTS max_media (
    bot_id bigint NOT NULL,
    kind text NOT NULL CHECK (kind IN ('image','audio')),
    asset_id bigint NOT NULL,
    payload jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (bot_id,kind,asset_id)
);
CREATE TABLE IF NOT EXISTS max_rate_limits (
    key text PRIMARY KEY,
    next_allowed timestamptz NOT NULL DEFAULT now()
);

-- Webhooks acknowledge only after commit. Duplicate deliveries have one inbox row.
CREATE TABLE IF NOT EXISTS max_inbox (
    id bigserial PRIMARY KEY,
    event_key text NOT NULL UNIQUE,
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','processing','done','failed','uncertain')),
    attempts integer NOT NULL DEFAULT 0,
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_max_inbox_pending ON max_inbox(id) WHERE status='pending';
