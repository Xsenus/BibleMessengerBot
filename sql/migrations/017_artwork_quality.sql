-- Keep charged failures and repair original messages without deleting history.
CREATE TABLE image_quality_reviews (
 id bigserial PRIMARY KEY,
 image_id bigint NOT NULL REFERENCES verse_illustrations(id),
 attempt_id bigint UNIQUE REFERENCES image_generation_attempts(id),
 verdict text NOT NULL CHECK(verdict IN ('approved','rejected','unavailable')),
 reason text NOT NULL,
 metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
 rejected_data bytea CHECK(rejected_data IS NULL OR octet_length(rejected_data)<=5242880),
 checked_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX image_quality_reviews_image ON image_quality_reviews(image_id,id DESC);
CREATE TABLE artwork_replacements (
 old_image_id bigint PRIMARY KEY REFERENCES verse_illustrations(id),
 new_image_id bigint REFERENCES verse_illustrations(id),
 reason text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 CHECK(old_image_id<>new_image_id)
);
