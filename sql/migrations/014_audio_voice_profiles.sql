-- Preserve old MP3s and their acknowledgements while new profiles are generated.
ALTER TABLE reading_audio ADD COLUMN voice_profile text NOT NULL DEFAULT 'v1';
