-- Applied idempotently at startup. Same dialect on local sqlite and Turso/libSQL.

-- Listings forwarded by a curator, before anything has been made of them.
--
-- The bot writes, the pipeline reads — the same split as `subscribers`, and for
-- the same reason: extraction needs an LLM client and the Vercel function is
-- kept to `httpx` alone. The bot's whole job here is to get the text into this
-- table intact.
--
-- `id` is a hash of the body, so the same listing forwarded twice — by two
-- curators, or twice by one — is one row rather than one event per forward.
--
-- `processed_at` is the queue: NULL means the pipeline has not looked at it.
-- A row that was looked at and declined is still processed; `decline_reason`
-- says why, and `event_id` stays NULL. That distinction is what makes "5
-- pending" a meaningful trigger rather than a growing pile of rejects.
--
-- On `body`: this is raw text typed by a person, and forwarded messages have
-- carried bank details and third-party phone numbers. It is stored because the
-- pipeline cannot parse what it cannot read, but it is the one column in this
-- schema worth a retention policy — see CLAUDE.md § Intake.
CREATE TABLE IF NOT EXISTS intake (
    id            TEXT PRIMARY KEY,  -- sha256 of the trimmed body
    channel       TEXT NOT NULL,     -- 'whatsapp'; 'email' if that ever lands
    sender        TEXT NOT NULL,     -- wa_id of the curator who sent it
    body          TEXT NOT NULL,
    received_at   TEXT NOT NULL,     -- ISO 8601, Karachi; anchors relative dates
    processed_at  TEXT,              -- NULL = still queued
    event_id      TEXT,              -- the Event it became, if any
    decline_reason TEXT              -- why not, when it became nothing
);

-- The pipeline's one query: what is still queued, oldest first.
CREATE INDEX IF NOT EXISTS idx_intake_pending ON intake (processed_at, received_at);
