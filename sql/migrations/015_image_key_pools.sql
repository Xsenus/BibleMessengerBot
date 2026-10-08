-- Preserve the existing breaker and every paid attempt when adding credentials.
ALTER TABLE image_provider_health DROP CONSTRAINT image_provider_health_pkey;
ALTER TABLE image_provider_health ADD PRIMARY KEY(provider,key_fingerprint);
CREATE INDEX image_attempt_credential_created
 ON image_generation_attempts(provider,key_fingerprint,created_at);
