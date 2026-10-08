-- Preserve all paid verse versions; chapter identities use a domain-separated hash.
ALTER TABLE verse_illustrations ADD COLUMN artwork_scope text NOT NULL DEFAULT 'verse'
 CHECK (artwork_scope IN ('verse','chapter'));
