"""Read the stored digest from Turso over its HTTP API.

No libSQL driver: `libsql-experimental` is a compiled extension and a poor fit
for a serverless function, and this side only ever runs one SELECT. The HTTP
API needs nothing but `httpx`.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

KARACHI = ZoneInfo("Asia/Karachi")

TIMEOUT = 10.0

# The pipeline joins a multi-part digest with this marker before storing it
# (`isb_events/cli.py`). Duplicated rather than imported, because importing it
# would drag the whole package — and its compiled driver — into the function.
# `tests/test_bot.py` pins the two copies together.
MESSAGE_SEPARATOR = "\n\n===MESSAGE===\n\n"

# Serve the week the asker is actually living in.
#
# This used to be "newest row wins", which was right only while the cron ran
# once a week: the next week's row did not exist until Saturday. A cron that
# runs daily writes it days ahead, so newest-wins would answer "what's on" with
# a week that has not started yet, every day from Tuesday on.
#
# So: the week containing today, by exact Monday. Failing that the nearest
# upcoming one (nothing has been rendered for this week yet), and failing that
# the most recent past one — stale, but better than the "no digest" placeholder
# when a digest does exist.
WEEK_DIGEST_SQL = "SELECT rendered_text, week_of FROM digests WHERE week_of = ? LIMIT 1"
UPCOMING_DIGEST_SQL = (
    "SELECT rendered_text, week_of FROM digests WHERE week_of > ? ORDER BY week_of ASC LIMIT 1"
)
PAST_DIGEST_SQL = (
    "SELECT rendered_text, week_of FROM digests WHERE week_of < ? ORDER BY week_of DESC LIMIT 1"
)


def _http_url(database_url: str) -> str:
    """`libsql://host` -> `https://host`; the HTTP API lives on the same host."""
    _, _, rest = database_url.partition("://")
    return f"https://{rest or database_url}"


def _cell(value: dict) -> str | None:
    """Turso returns typed cells: {"type": "text", "value": "..."}."""
    return None if value.get("type") == "null" else value.get("value")


def query(sql: str, args: list[str] | None = None) -> list[list[str | None]]:
    url = os.environ["TURSO_DATABASE_URL"]
    token = os.environ["TURSO_AUTH_TOKEN"]
    stmt: dict = {"sql": sql}
    if args:
        stmt["args"] = [{"type": "text", "value": a} for a in args]
    resp = httpx.post(
        f"{_http_url(url)}/v2/pipeline",
        headers={"Authorization": f"Bearer {token}"},
        json={"requests": [{"type": "execute", "stmt": stmt}, {"type": "close"}]},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    first = body["results"][0]
    if first.get("type") != "ok":
        raise RuntimeError(f"Turso query failed: {first.get('error')}")
    return [[_cell(c) for c in row] for row in first["response"]["result"]["rows"]]


def current_digest() -> tuple[str, str] | None:
    """`(rendered_text, week_of)` for the week containing today, or None."""
    today = datetime.now(KARACHI).date()
    monday = (today - timedelta(days=today.weekday())).isoformat()
    for sql, args in (
        (WEEK_DIGEST_SQL, [monday]),
        (UPCOMING_DIGEST_SQL, [monday]),
        (PAST_DIGEST_SQL, [monday]),
    ):
        rows = query(sql, args)
        if rows and rows[0][0] is not None:
            return rows[0][0], rows[0][1] or ""
    return None


def digest_messages() -> list[str]:
    """The digest split back into the messages the renderer packed it into."""
    found = current_digest()
    if found is None:
        return []
    text, _ = found
    return [part for part in text.split(MESSAGE_SEPARATOR) if part.strip()]


# -- day views ---------------------------------------------------------------
#
# `digest_events` is the week's digest broken into one row per event, written by
# the pipeline (`isb_events/render.py` renders `block`; `isb_events/store.py`
# stores it). Filtering is a WHERE clause here precisely so that no formatting
# rule has to be duplicated on this side — the bot concatenates blocks and adds
# a heading, nothing more.
#
# Deliberately keyed on `event_date` alone, not on a week: on a Saturday the
# newest digest row is already *next* week, so joining through `digests` would
# make "what's on today" come back empty from Saturday lunchtime onwards. The
# day is unambiguous by itself — digest weeks do not overlap.
DAY_EVENTS_SQL = """
SELECT day_label, block FROM digest_events
WHERE event_date = ?
ORDER BY starts_at
"""


def day_events(day: date) -> list[tuple[str, str]]:
    """`(day_label, block)` for everything on one day, in start order."""
    rows = query(DAY_EVENTS_SQL, [day.isoformat()])
    return [(row[0] or "", row[1] or "") for row in rows]


# -- subscribers -------------------------------------------------------------
#
# The bot is the only writer: it is the only side that sees inbound messages.
# `isb_events/store.py` reads them back for the nudge.

RECORD_CONTACT_SQL = """
INSERT INTO subscribers (wa_id, first_seen, last_seen, message_count)
VALUES (?, ?, ?, 1)
ON CONFLICT(wa_id) DO UPDATE SET
    last_seen     = excluded.last_seen,
    message_count = subscribers.message_count + 1
"""

# Consent is only ever granted explicitly, so opting in clears any earlier STOP
# — that is a person asking again, not an accident.
OPT_IN_SQL = "UPDATE subscribers SET opted_in_at = ?, opted_out_at = NULL WHERE wa_id = ?"
OPT_OUT_SQL = "UPDATE subscribers SET opted_out_at = ? WHERE wa_id = ?"


def _now() -> str:
    return datetime.now(KARACHI).isoformat()


def record_contact(wa_id: str) -> None:
    """Log that someone messaged us. Not consent to message them first."""
    now = _now()
    query(RECORD_CONTACT_SQL, [wa_id, now, now])


def opt_in(wa_id: str) -> None:
    query(OPT_IN_SQL, [_now(), wa_id])


def opt_out(wa_id: str) -> None:
    query(OPT_OUT_SQL, [_now(), wa_id])
