-- Дополнительные генераторы картинок, которыми администратор управляет из панели (бесплатные и любые совместимые API).
-- Имя провайдера в очереди попыток — `x-<slug>`, поэтому ограничение на пять встроенных имён снимается.
ALTER TABLE image_provider_health DROP CONSTRAINT IF EXISTS image_provider_health_provider_check;

-- Бесплатные генераторы резервируют 0 USD, поэтому строгая положительность резерва больше не нужна.
ALTER TABLE image_generation_attempts DROP CONSTRAINT IF EXISTS image_generation_attempts_reserved_usd_check;
ALTER TABLE image_generation_attempts ADD CONSTRAINT image_generation_attempts_reserved_usd_check CHECK (reserved_usd >= 0);

CREATE TABLE image_custom_providers (
    id bigserial PRIMARY KEY,
    slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,29}$'),
    label text NOT NULL DEFAULT '' CHECK (length(label) <= 100),
    adapter text NOT NULL CHECK (adapter IN ('cloudflare', 'huggingface', 'pollinations', 'horde', 'openai_compatible')),
    base_url text NOT NULL DEFAULT '' CHECK (length(base_url) <= 300),
    model text NOT NULL DEFAULT '' CHECK (length(model) <= 200),
    api_key_enc text NOT NULL DEFAULT '',                    -- ключ в зашифрованном виде (Fernet), наружу не отдаётся
    account_id text NOT NULL DEFAULT '' CHECK (length(account_id) <= 100),
    width integer NOT NULL DEFAULT 1216 CHECK (width BETWEEN 256 AND 2048),
    height integer NOT NULL DEFAULT 832 CHECK (height BETWEEN 256 AND 2048),
    steps integer CHECK (steps BETWEEN 1 AND 100),
    is_enabled boolean NOT NULL DEFAULT true,
    priority integer NOT NULL DEFAULT 500 CHECK (priority BETWEEN 0 AND 100000),   -- меньше — раньше; платные встроенные — от 1000
    daily_limit integer NOT NULL DEFAULT 20 CHECK (daily_limit BETWEEN 1 AND 10000),
    extra jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_test_at timestamptz,
    last_test_ok boolean,
    last_test_error text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
