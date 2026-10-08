-- Each request belongs to the exact acknowledged bot message, not just its chat.
ALTER TABLE illustration_requests ADD COLUMN telegram_message_id bigint
 CHECK (telegram_message_id IS NULL OR telegram_message_id > 0);
ALTER TABLE illustration_requests ADD COLUMN source_snapshot jsonb NOT NULL DEFAULT '[]'::jsonb;
DROP INDEX illustration_requests_one_waiter;
-- Legacy requests did not record the original message ID. Never guess it or send
-- a replacement photo after upgrading. Preserve all records and paid artwork.
UPDATE delivery_log d SET status='cancelled',error_code='legacy_photo_followup',updated_at=now()
 FROM illustration_requests r WHERE r.delivery_id=d.id
 AND d.status IN ('pending','retry') AND r.telegram_message_id IS NULL;
UPDATE illustration_requests SET state='cancelled'
 WHERE telegram_message_id IS NULL AND state IN ('waiting','queued');
CREATE INDEX illustration_requests_editable ON illustration_requests(image_id)
 WHERE state='waiting' AND telegram_message_id IS NOT NULL;
-- Freeze the whole devotional reading, so prefetch and delivery cannot disagree
-- and repeat exclusion includes neighboring verses, not just the anchor.
ALTER TABLE daily_verse_selections ADD COLUMN reading_snapshot jsonb;
