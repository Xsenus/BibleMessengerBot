-- Preserve existing image IDs, Telegram file IDs, outbox references and S3 copies.
DO $$
DECLARE old_name text;
BEGIN
 SELECT conname INTO STRICT old_name FROM pg_constraint
 WHERE conrelid='verse_illustrations'::regclass AND contype='u'
 AND pg_get_constraintdef(oid)='UNIQUE (translation_id, book_code, chapter, verse, text_sha256)';
 EXECUTE format('ALTER TABLE verse_illustrations DROP CONSTRAINT %I',old_name);
END $$;
ALTER TABLE verse_illustrations ADD COLUMN generated_at timestamptz;
ALTER TABLE verse_illustrations ADD COLUMN prompt_version integer NOT NULL DEFAULT 1;
UPDATE verse_illustrations SET generated_at=created_at WHERE status='ready';
CREATE UNIQUE INDEX verse_illustrations_one_pending
 ON verse_illustrations(translation_id,book_code,chapter,verse,text_sha256) WHERE status='pending';
CREATE INDEX verse_illustrations_versions
 ON verse_illustrations(translation_id,book_code,chapter,verse,text_sha256,generated_at DESC)
 WHERE status='ready';

CREATE TABLE illustration_views (
 telegram_chat_id bigint NOT NULL REFERENCES telegram_chats(telegram_chat_id) ON DELETE CASCADE,
 image_id bigint NOT NULL REFERENCES verse_illustrations(id) ON DELETE CASCADE,
 last_sent_at timestamptz NOT NULL DEFAULT now(), send_count integer NOT NULL DEFAULT 1,
 PRIMARY KEY(telegram_chat_id,image_id)
);
CREATE TABLE illustration_requests (
 id bigserial PRIMARY KEY,
 telegram_chat_id bigint NOT NULL REFERENCES telegram_chats(telegram_chat_id) ON DELETE CASCADE,
 image_id bigint NOT NULL REFERENCES verse_illustrations(id) ON DELETE CASCADE,
 request_key text NOT NULL,
 caption text NOT NULL,
 chat_revision bigint NOT NULL,
 message_thread_id bigint,
 state text NOT NULL DEFAULT 'waiting' CHECK(state IN ('waiting','queued','expired','cancelled')),
 delivery_id bigint REFERENCES delivery_log(id) ON DELETE SET NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 expires_at timestamptz NOT NULL DEFAULT now()+interval '24 hours',
 UNIQUE(telegram_chat_id,request_key)
);
CREATE UNIQUE INDEX illustration_requests_one_waiter
 ON illustration_requests(telegram_chat_id,image_id) WHERE state='waiting';
CREATE INDEX illustration_requests_pending ON illustration_requests(image_id) WHERE state='waiting';

ALTER TABLE image_generation_jobs ADD COLUMN prompt_version integer NOT NULL DEFAULT 1;
UPDATE verse_illustrations SET s3_backed_up_at=NULL WHERE status='ready';
INSERT INTO illustration_views(telegram_chat_id,image_id,last_sent_at,send_count)
SELECT d.telegram_chat_id,(p.value->>'image_id')::bigint,
 max(COALESCE(d.sent_at,d.updated_at)),count(*)::integer
FROM delivery_log d
CROSS JOIN LATERAL jsonb_array_elements(d.chunks) WITH ORDINALITY AS p(value,position)
JOIN verse_illustrations i ON i.id=(p.value->>'image_id')::bigint
WHERE p.value->>'kind'='photo' AND p.position<=d.next_chunk
GROUP BY d.telegram_chat_id,(p.value->>'image_id')::bigint;
