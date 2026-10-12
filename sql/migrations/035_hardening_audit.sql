-- Исправления по результатам аудита: повторные попытки запусков объявлений, постраничные клавиатуры VK, голосовые вложения VK.
ALTER TABLE announcement_runs ADD COLUMN attempts integer NOT NULL DEFAULT 0;
ALTER TABLE announcement_runs DROP CONSTRAINT IF EXISTS announcement_runs_status_check;
ALTER TABLE announcement_runs ADD CONSTRAINT announcement_runs_status_check CHECK (status IN ('sending', 'done', 'skipped_late', 'failed'));
CREATE INDEX ix_announcement_runs_sending ON announcement_runs(id) WHERE status = 'sending';

ALTER TABLE vk_media DROP CONSTRAINT IF EXISTS vk_media_kind_check;
ALTER TABLE vk_media ADD CONSTRAINT vk_media_kind_check CHECK (kind IN ('image', 'audio', 'asset', 'voice'));

-- Клавиатуры VK, не поместившиеся в одно сообщение (6 рядов, 10 кнопок): страницы хранятся вместе с текстом сообщения.
CREATE TABLE vk_keyboard_pages (
    chat_id bigint NOT NULL,
    cmid bigint NOT NULL,
    body text NOT NULL,
    attachment text NOT NULL DEFAULT '',
    pages jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, cmid)
);
