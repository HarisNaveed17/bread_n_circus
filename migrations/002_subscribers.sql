-- Applied idempotently at startup. Same dialect on local sqlite and Turso/libSQL.

-- Everyone who has ever messaged the bot, and separately, who has consented to
-- being messaged *first*.
--
-- Those are not the same thing and the distinction is the point of this table.
-- Replying inside the 24-hour window a user opened is free and needs no
-- consent; sending the weekly nudge is business-initiated, and Meta requires
-- documented opt-in for it. So contact is recorded on every inbound message,
-- while opted_in_at is only ever set by an explicit request.
CREATE TABLE IF NOT EXISTS subscribers (
    wa_id         TEXT PRIMARY KEY,  -- the sender's WhatsApp id (their number)
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    opted_in_at   TEXT,              -- null unless they asked to be nudged
    opted_out_at  TEXT               -- set by STOP; wins over opted_in_at
);

CREATE INDEX IF NOT EXISTS idx_subscribers_opted_in ON subscribers (opted_in_at);
