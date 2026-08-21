-- Applied idempotently at startup. Same dialect on local sqlite and Turso/libSQL.

CREATE TABLE IF NOT EXISTS events (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    venue        TEXT,
    starts_at    TEXT NOT NULL,   -- ISO 8601, tz-aware (Asia/Karachi)
    ends_at      TEXT,
    category     TEXT,
    price_text   TEXT,
    url          TEXT NOT NULL,
    sources      TEXT NOT NULL,   -- json array of source slugs
    series_key   TEXT,
    description  TEXT,
    raw_json     TEXT,            -- untouched scraped payload, for post-mortems
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_starts_at ON events (starts_at);

CREATE TABLE IF NOT EXISTS digests (
    week_of       TEXT PRIMARY KEY,  -- ISO date of the Monday
    rendered_text TEXT NOT NULL,     -- full digest; may contain multiple messages
    event_ids     TEXT NOT NULL,     -- json array
    created_at    TEXT NOT NULL,
    sent_at       TEXT               -- null until delivered
);
