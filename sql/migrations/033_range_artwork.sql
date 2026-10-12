-- Иллюстрации для диапазона стихов, заданного вручную (например, Гал 6:1–10): отдельная область `range`.
-- Картинка закрепляется за точной последовательностью: хеш содержит текст всех стихов диапазона с номерами,
-- поэтому 6:1–10, 6:1–9 и 6:1–5 — разные картинки; `verse` — первый стих, `verse_end` — последний.
ALTER TABLE verse_illustrations DROP CONSTRAINT IF EXISTS verse_illustrations_artwork_scope_check;
ALTER TABLE verse_illustrations ADD CONSTRAINT verse_illustrations_artwork_scope_check CHECK (artwork_scope IN ('verse', 'chapter', 'range'));
ALTER TABLE verse_illustrations ADD COLUMN verse_end integer;
ALTER TABLE verse_illustrations ADD CONSTRAINT verse_illustrations_range_end_check
    CHECK ((artwork_scope = 'range' AND verse_end IS NOT NULL AND verse_end > verse) OR (artwork_scope <> 'range' AND verse_end IS NULL));
