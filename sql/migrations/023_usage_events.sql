-- Anonymous aggregate usage counters: no user, chat or message identifiers are stored.
CREATE TABLE usage_events (
    day date NOT NULL,
    platform text NOT NULL CHECK (platform IN ('telegram', 'max')),
    event text NOT NULL CHECK (event ~ '^[a-z0-9_:.-]{1,40}$'),
    count bigint NOT NULL DEFAULT 0 CHECK (count >= 0),
    PRIMARY KEY (day, platform, event)
);
