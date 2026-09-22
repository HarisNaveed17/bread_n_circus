-- Applied idempotently at startup. Same dialect on local sqlite and Turso/libSQL.

-- What the classifier decided about a title, so it is never asked twice.
--
-- Keyed on the *title and venue*, not on an event id, and that is the whole
-- point. The cron renders two weeks twice a day, so the same ~90 events pass
-- through classification four times daily; without this the model would be
-- asked about "Kaavish Live" 730 times a year.
--
-- **This is a correctness mechanism before it is a cost one.** A title
-- re-classified twice a day can land in `music` one run and `theatre_and_film`
-- the next, and `normalize._merge` then propagates whichever value won into
-- deduped rows — so the bot's answer to "what music is on" would flicker with
-- no code change behind it. Deciding once and remembering is what makes a
-- category stick.
--
-- `vocab_version` is part of the key, not just a record: changing the
-- vocabulary or the precedence rule invalidates every cached answer in one
-- edit, rather than leaving a corpus half-labelled under two vocabularies.
--
-- `category` may be NULL, and a NULL row is a real answer — it means the
-- classifier looked and could not say. Caching that matters as much as caching
-- a hit, or every run re-pays for the same shrug.
CREATE TABLE IF NOT EXISTS event_categories (
    title_hash    TEXT NOT NULL,     -- sha256 of "title|venue", lowercased
    vocab_version TEXT NOT NULL,     -- categories.VOCAB_VERSION at decision time
    category      TEXT,              -- a vocabulary value, or NULL for "could not say"
    classified_at TEXT NOT NULL,     -- ISO 8601, Karachi
    PRIMARY KEY (title_hash, vocab_version)
);
