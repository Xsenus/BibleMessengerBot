-- Persist publication intent before fan-out, so a crash cannot lose an approved post.
-- An unapproved captured post has no publication_requested_at and is never retried.
ALTER TABLE source_posts
    ADD COLUMN publication_requested_at timestamptz,
    ADD COLUMN publication_audience jsonb,
    ADD COLUMN publication_retry_at timestamptz,
    ADD COLUMN publication_attempts integer NOT NULL DEFAULT 0,
    ADD COLUMN publication_error text NOT NULL DEFAULT '',
    ADD COLUMN recipients integer NOT NULL DEFAULT 0,
    ADD COLUMN queued integer NOT NULL DEFAULT 0,
    ADD COLUMN skipped integer NOT NULL DEFAULT 0;
CREATE INDEX ix_source_posts_publication_retry ON source_posts(publication_retry_at)
    WHERE status='new' AND publication_requested_at IS NOT NULL;

ALTER TABLE broadcasts
    ADD COLUMN attempts integer NOT NULL DEFAULT 0,
    ADD COLUMN retry_at timestamptz,
    ADD COLUMN last_error text NOT NULL DEFAULT '';
ALTER TABLE broadcasts DROP CONSTRAINT broadcasts_status_check;
ALTER TABLE broadcasts ADD CONSTRAINT broadcasts_status_check
    CHECK (status IN ('draft','sending','done','cancelled','failed'));
CREATE INDEX ix_broadcasts_retry ON broadcasts(retry_at) WHERE status='sending';

ALTER TABLE video_items
    ADD COLUMN notification_attempts integer NOT NULL DEFAULT 0,
    ADD COLUMN notification_error text NOT NULL DEFAULT '';

-- Repair missing source identities without confusing equal IDs on different platforms.
UPDATE content_sources s SET chat_id=c.telegram_chat_id
    FROM telegram_chats c
    WHERE s.platform='telegram' AND c.platform='telegram'
      AND s.external_id=c.telegram_chat_id AND s.chat_id IS NULL;
UPDATE content_sources s SET chat_id=i.internal_id
    FROM platform_identities i
    WHERE i.kind='chat' AND s.platform=i.platform AND s.external_id=i.external_id AND s.chat_id IS NULL;
