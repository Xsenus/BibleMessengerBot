-- Рассылка отрывка всем получателям по решению владельца бота.
CREATE TABLE broadcasts (
    id bigserial PRIMARY KEY,
    created_by bigint NOT NULL,
    refs jsonb NOT NULL,                      -- [{book, chapter, first, last}] — адреса отрывков
    source_translation_id bigint REFERENCES translations(id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'sending', 'done', 'cancelled')),
    recipients integer NOT NULL DEFAULT 0,    -- сколько назначений было активно
    queued integer NOT NULL DEFAULT 0,        -- поставлено в очередь
    skipped integer NOT NULL DEFAULT 0,       -- пропущено (нет отрывка в издании чата и т. п.)
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_broadcasts_created ON broadcasts(created_at DESC);
