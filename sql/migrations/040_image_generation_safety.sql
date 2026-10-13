-- Explicit administrator jobs and a shared, conservative budget for custom APIs.
ALTER TABLE image_generation_jobs ADD COLUMN admin_requested boolean NOT NULL DEFAULT false;
-- Repair previously requested gallery variants which had no delivery target.
UPDATE image_generation_jobs SET admin_requested=true
WHERE prompt LIKE '%Alternative composition:%';

ALTER TABLE image_custom_providers
    ADD COLUMN billing_mode text NOT NULL DEFAULT 'unknown'
        CHECK (billing_mode IN ('unknown','metered','free_only')),
    ADD COLUMN reservation_usd numeric(12,4) NOT NULL DEFAULT 0
        CHECK (reservation_usd >= 0 AND reservation_usd <= 1000);
ALTER TABLE image_custom_providers ADD CONSTRAINT custom_provider_metered_reserve
    CHECK (billing_mode <> 'metered' OR reservation_usd >= 0.10);
UPDATE image_custom_providers SET billing_mode='free_only'
WHERE api_key_enc='' AND (
    (adapter='horde' AND base_url IN ('','https://aihorde.net/api/v2') AND model='') OR
    (adapter='pollinations' AND base_url IN ('','https://image.pollinations.ai') AND model IN ('','flux'))
);

-- Panel probes are submissions too; they share monthly and daily limits.
CREATE TABLE image_provider_test_attempts (
    id bigserial PRIMARY KEY,
    provider text NOT NULL,
    key_fingerprint text NOT NULL,
    reserved_usd numeric(12,4) NOT NULL CHECK (reserved_usd >= 0),
    state text NOT NULL DEFAULT 'reserved' CHECK (state IN ('reserved','succeeded','failed','uncertain')),
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX image_provider_test_attempts_created ON image_provider_test_attempts(created_at);
