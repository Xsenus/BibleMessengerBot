-- Картинка рассылки: та же, что сгенерирована при поиске администратора (одна на всех получателей).
ALTER TABLE broadcasts ADD COLUMN image_id bigint REFERENCES verse_illustrations(id) ON DELETE SET NULL;
