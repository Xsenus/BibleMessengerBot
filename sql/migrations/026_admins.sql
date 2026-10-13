-- Несколько администраторов на любой платформе (Telegram, MAX, ВКонтакте).
-- Владелец (is_owner) остаётся единственным и управляет списком администраторов.
ALTER TABLE telegram_users ADD COLUMN is_admin boolean NOT NULL DEFAULT false;

-- Одноразовые приглашения: код показывается владельцу один раз, в БД хранится только хеш.
CREATE TABLE admin_invites (
    id bigserial PRIMARY KEY,
    code_hash text NOT NULL UNIQUE,
    created_by bigint NOT NULL,
    expires_at timestamptz NOT NULL,
    used_by bigint,
    used_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
