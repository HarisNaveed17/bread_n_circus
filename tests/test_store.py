"""The store has to behave identically on sqlite3 and on libSQL/Turso.

The two backends disagree in ways that only surface at runtime: libSQL binds
qmark-only (a named-parameter dict raises `TypeError`) and has no
`row_factory`, so its rows are plain tuples rather than `sqlite3.Row`. Both
gaps were live bugs that the sqlite3-only tests could never have caught, so
every test here runs against both connections.

No network: libSQL is exercised over an in-memory database, the same way
sqlite3 is.
"""

import sqlite3
from datetime import date, datetime

import libsql_experimental
import pytest

from isb_events.models import KARACHI, Event
from isb_events.store import Store

WEEK_OF = date(2026, 8, 31)


def _sqlite3_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _libsql_conn():
    return libsql_experimental.connect(":memory:")


@pytest.fixture(params=["sqlite3", "libsql"])
def store(request):
    conn = _sqlite3_conn() if request.param == "sqlite3" else _libsql_conn()
    with Store(conn) as store:
        yield store


def _event(**overrides) -> Event:
    fields = {
        "title": "Kaavish Live",
        "venue": "Jinnah Convention Centre",
        "starts_at": datetime(2026, 9, 11, 20, 0, tzinfo=KARACHI),
        "ends_at": datetime(2026, 9, 11, 23, 0, tzinfo=KARACHI),
        "url": "https://ticketwala.pk/event/kaavish-live-in-concert-7164",
        "sources": ["ticketwala"],
    }
    return Event(**{**fields, **overrides})


def test_upsert_event_inserts(store):
    store.upsert_event(_event())
    row = store._conn.execute("SELECT title, venue FROM events").fetchone()
    assert tuple(row) == ("Kaavish Live", "Jinnah Convention Centre")


def test_upsert_event_is_idempotent_and_updates_in_place(store):
    event = _event()
    store.upsert_event(event)
    store.upsert_event(_event(title="Kaavish Live (rescheduled)"))
    rows = store._conn.execute("SELECT id, title FROM events").fetchall()
    assert len(rows) == 1
    assert tuple(rows[0]) == (event.id, "Kaavish Live (rescheduled)")


def test_upsert_event_handles_optional_fields_left_unset(store):
    store.upsert_event(_event(ends_at=None, venue=None))
    row = store._conn.execute("SELECT venue, ends_at, price_text FROM events").fetchone()
    assert tuple(row) == (None, None, None)


def test_save_and_get_digest_round_trip(store):
    event = _event()
    store.save_digest(WEEK_OF, "*This week in Islamabad*", [event.id])
    row = store.get_digest(WEEK_OF)
    assert row["rendered_text"] == "*This week in Islamabad*"
    assert row["week_of"] == WEEK_OF.isoformat()
    assert row["sent_at"] is None


def test_get_digest_returns_none_for_an_unrendered_week(store):
    assert store.get_digest(WEEK_OF) is None


def test_save_digest_overwrites_and_clears_sent_at(store):
    store.save_digest(WEEK_OF, "first pass", [])
    store.mark_digest_sent(WEEK_OF)
    assert store.get_digest(WEEK_OF)["sent_at"] is not None

    store.save_digest(WEEK_OF, "re-rendered", [])
    row = store.get_digest(WEEK_OF)
    assert row["rendered_text"] == "re-rendered"
    assert row["sent_at"] is None


def test_mark_digest_sent(store):
    store.save_digest(WEEK_OF, "text", [])
    store.mark_digest_sent(WEEK_OF)
    assert store.get_digest(WEEK_OF)["sent_at"] is not None


# -- subscribers -------------------------------------------------------------
#
# Read-only here; the bot writes them over Turso's HTTP API. Rows are inserted
# with raw SQL so these tests pin the reads, not the writer.


def _add_subscriber(store, wa_id, opted_in=None, opted_out=None):
    store._conn.execute(
        """
        INSERT INTO subscribers
            (wa_id, first_seen, last_seen, message_count, opted_in_at, opted_out_at)
        VALUES (?, '2026-08-01', '2026-08-01', 1, ?, ?)
        """,
        (wa_id, opted_in, opted_out),
    )
    store._conn.commit()


def test_opted_in_subscribers_excludes_mere_contacts(store):
    """Messaging the bot is not consent to be messaged first."""
    _add_subscriber(store, "923001111111")  # contacted only
    _add_subscriber(store, "923002222222", opted_in="2026-08-02")
    assert store.opted_in_subscribers() == ["923002222222"]


def test_opt_out_wins_over_opt_in(store):
    _add_subscriber(store, "923003333333", opted_in="2026-08-02", opted_out="2026-08-03")
    assert store.opted_in_subscribers() == []


def test_subscriber_counts_separates_contacts_from_consent(store):
    _add_subscriber(store, "923001111111")
    _add_subscriber(store, "923002222222", opted_in="2026-08-02")
    _add_subscriber(store, "923003333333", opted_in="2026-08-02", opted_out="2026-08-03")
    assert store.subscriber_counts() == {"contacts": 3, "opted_in": 1}


def test_subscriber_counts_on_an_empty_table(store):
    assert store.subscriber_counts() == {"contacts": 0, "opted_in": 0}
