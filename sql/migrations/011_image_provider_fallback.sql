-- Add metadata only; never repeat legacy uncertain paid requests.
ALTER TABLE image_generation_attempts ADD COLUMN provider text NOT NULL DEFAULT 'openai';
ALTER TABLE image_generation_attempts ADD COLUMN model text NOT NULL DEFAULT 'gpt-image-2';
ALTER TABLE image_generation_attempts ADD COLUMN error_code text;
ALTER TABLE image_generation_attempts ADD COLUMN request_id text;
ALTER TABLE image_generation_attempts ADD COLUMN polling_url text;
ALTER TABLE image_generation_attempts ADD COLUMN key_fingerprint text;
CREATE INDEX image_attempt_provider_job ON image_generation_attempts(job_id,provider);

CREATE TABLE image_provider_health (
 provider text PRIMARY KEY CHECK(provider IN ('openai','gemini','bfl','ideogram','stability')),
 key_fingerprint text NOT NULL,
 failures integer NOT NULL DEFAULT 0,
 blocked_until timestamptz,
 error_code text,
 updated_at timestamptz NOT NULL DEFAULT now()
);
