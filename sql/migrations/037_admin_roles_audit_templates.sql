-- Роли администраторов, журнал действий, шаблоны объявлений.
ALTER TABLE telegram_users ADD COLUMN admin_role text NOT NULL DEFAULT 'admin'
    CHECK (admin_role IN ('viewer', 'editor', 'admin'));
ALTER TABLE admin_invites ADD COLUMN role text NOT NULL DEFAULT 'admin'
    CHECK (role IN ('viewer', 'editor', 'admin'));

CREATE TABLE admin_audit (
    id bigserial PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    actor text NOT NULL,                    -- id пользователя или 'panel'
    source text NOT NULL,                   -- 'panel' | 'bot'
    action text NOT NULL,
    target text NOT NULL DEFAULT '',
    details jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX ix_admin_audit_created ON admin_audit(created_at DESC);

CREATE TABLE announcement_templates (
    id bigserial PRIMARY KEY,
    name text NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 100),
    title text NOT NULL DEFAULT '' CHECK (length(title) <= 200),
    body text NOT NULL CHECK (length(body) BETWEEN 1 AND 3500),
    created_at timestamptz NOT NULL DEFAULT now()
);
