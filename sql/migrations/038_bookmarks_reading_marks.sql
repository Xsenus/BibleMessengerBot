-- Закладки с заметками и отметки чтения (для серии дней подряд); всё по чату.
CREATE TABLE bookmarks (
    id bigserial PRIMARY KEY,
    telegram_chat_id bigint NOT NULL REFERENCES telegram_chats(telegram_chat_id) ON DELETE CASCADE,
    book_code text NOT NULL REFERENCES books(code),
    chapter integer NOT NULL CHECK (chapter > 0),
    verse_from integer NOT NULL DEFAULT 0 CHECK (verse_from >= 0),   -- 0 — вся глава
    verse_to integer NOT NULL DEFAULT 0 CHECK (verse_to >= verse_from),
    note text NOT NULL DEFAULT '' CHECK (length(note) <= 300),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (telegram_chat_id, book_code, chapter, verse_from, verse_to)
);
CREATE TABLE reading_marks (
    telegram_chat_id bigint NOT NULL REFERENCES telegram_chats(telegram_chat_id) ON DELETE CASCADE,
    day date NOT NULL,
    PRIMARY KEY (telegram_chat_id, day)
);
