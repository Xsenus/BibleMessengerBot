-- Статистика циклов служб (длительность, ошибки) — для метрик и оповещений.
CREATE TABLE loop_stats (
    name text PRIMARY KEY,
    runs bigint NOT NULL DEFAULT 0,
    errors bigint NOT NULL DEFAULT 0,
    last_run_at timestamptz,
    last_ok_at timestamptz,
    last_duration_ms integer NOT NULL DEFAULT 0,
    last_error text NOT NULL DEFAULT ''
);

-- Индексы для отчётов и очистки.
CREATE INDEX IF NOT EXISTS ix_announcement_runs_created ON announcement_runs(created_at);
CREATE INDEX IF NOT EXISTS ix_video_items_status ON video_items(status, created_at);
CREATE INDEX IF NOT EXISTS ix_source_posts_created ON source_posts(created_at);
CREATE INDEX IF NOT EXISTS ix_delivery_log_mode_created ON delivery_log(mode, created_at);

-- Длительность попыток генерации картинок (для автоматического выбора лучшего генератора).
ALTER TABLE image_generation_attempts ADD COLUMN duration_ms integer;
CREATE INDEX IF NOT EXISTS ix_image_attempts_created ON image_generation_attempts(created_at);

-- Закрепление лучшей версии картинки и карантин (скрытие) неудачных.
ALTER TABLE verse_illustrations ADD COLUMN pinned boolean NOT NULL DEFAULT false;
ALTER TABLE verse_illustrations ADD COLUMN quarantined_at timestamptz;
