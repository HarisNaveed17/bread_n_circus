-- Applied idempotently at startup. Same dialect on local sqlite and Turso/libSQL.

-- The per-event view of a rendered digest, so the bot can answer "what's on
-- today" without re-rendering anything.
--
-- `digests.rendered_text` is the whole week as one blob: fine for the default
-- reply, useless for filtering. This table is the same render broken back into
-- its parts, with the fields worth filtering on kept as columns — so a day view
-- is a WHERE clause, and categories will be one more column in the same clause.
--
-- `block` is rendered by `isb_events/render.py` and stored verbatim. The bot
-- reaches Turso over HTTP with no access to the package (`vercel.json` excludes
-- it from the function bundle), so *every* event's formatting stays on the
-- pipeline side and the bot only ever concatenates.
--
-- Note these blocks are per occurrence: a recurring series is collapsed into
-- one "3x this week" line in the weekly text, but someone asking about a single
-- day wants that day's sitting and its time.
--
-- Rows are replaced wholesale per `week_of` on every render, so a re-render
-- never leaves a dropped event behind.
CREATE TABLE IF NOT EXISTS digest_events (
    week_of    TEXT NOT NULL,     -- ISO date of the Monday; matches digests.week_of
    id         TEXT NOT NULL,     -- Event.id
    event_date TEXT NOT NULL,     -- YYYY-MM-DD in Asia/Karachi
    day_label  TEXT NOT NULL,     -- "Tue 1 Sep", as the renderer heads the day
    starts_at  TEXT NOT NULL,     -- ISO 8601, tz-aware; orders within the day
    category   TEXT,
    block      TEXT NOT NULL,     -- the rendered event block
    PRIMARY KEY (week_of, id)
);

CREATE INDEX IF NOT EXISTS idx_digest_events_date ON digest_events (event_date, starts_at);
