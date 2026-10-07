-- Per-source-verse artwork, queued independently of reading and scheduling.
CREATE TABLE verse_illustrations (
 id bigserial PRIMARY KEY,
 translation_id bigint NOT NULL REFERENCES translations(id) ON DELETE CASCADE,
 book_code text NOT NULL REFERENCES books(code),
 chapter integer NOT NULL CHECK(chapter>0),
 verse integer NOT NULL CHECK(verse>0),
 text_sha256 text NOT NULL CHECK(text_sha256 ~ '^[a-f0-9]{64}$'),
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','ready','failed')),
 image_data bytea,
 mime_type text CHECK(mime_type IN ('image/png','image/jpeg','image/webp')),
 prompt text NOT NULL DEFAULT '',
 telegram_file_id text,
 telegram_bot_id bigint,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(translation_id,book_code,chapter,verse,text_sha256),
 CHECK(image_data IS NULL OR octet_length(image_data) BETWEEN 1 AND 5242880),
 CHECK(status<>'ready' OR (image_data IS NOT NULL AND mime_type IS NOT NULL))
);
CREATE INDEX ix_verse_illustrations_queue ON verse_illustrations(id) WHERE status='pending';
