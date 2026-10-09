-- Telegram keyboard choice survives messages and restarts.
ALTER TABLE telegram_chats ADD COLUMN keyboard_hidden boolean NOT NULL DEFAULT false;
ALTER TABLE telegram_chats ADD COLUMN audio_voice text NOT NULL DEFAULT 'david' CHECK(audio_voice IN ('david','mary'));
-- Keep news provenance intact while upgrading prayer composition.
ALTER TABLE prayer_briefs ADD COLUMN composition_version integer NOT NULL DEFAULT 1;
ALTER TABLE daily_verse_selections ADD COLUMN reading_version integer NOT NULL DEFAULT 1;
