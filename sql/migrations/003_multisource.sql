-- Additive 1.3.0 migration. Previously applied schema files are unchanged.
ALTER TABLE translations ADD COLUMN IF NOT EXISTS content_sha256 text;
ALTER TABLE translations ADD COLUMN IF NOT EXISTS numbering_system text NOT NULL DEFAULT 'BibleNLP Original versification';
CREATE INDEX IF NOT EXISTS ix_translation_content ON translations(language_id, content_sha256);

CREATE TABLE IF NOT EXISTS translation_sources (
    source_slug text NOT NULL CHECK (source_slug IN ('biblenlp','getbible','helloao')),
    source_translation_id text NOT NULL,
    translation_id bigint NOT NULL REFERENCES translations(id) ON DELETE CASCADE,
    source_name text NOT NULL,
    source_url text NOT NULL,
    source_sha256 text NOT NULL,
    source_revision text NOT NULL,
    content_sha256 text NOT NULL,
    numbering_system text NOT NULL,
    license_evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    verified_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(source_slug,source_translation_id)
);
CREATE INDEX IF NOT EXISTS ix_translation_sources_target ON translation_sources(translation_id);
CREATE TABLE IF NOT EXISTS translation_book_names (
    translation_id bigint NOT NULL REFERENCES translations(id) ON DELETE CASCADE,
    book_code text NOT NULL REFERENCES books(code) ON DELETE RESTRICT,
    name text NOT NULL,
    PRIMARY KEY(translation_id,book_code)
);
