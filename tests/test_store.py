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

from isb_events.models import KARACHI, DigestWindow, Event
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


def _entry(**overrides) -> dict:
    fields = {
        "id": "abc123",
        "event_date": "2026-09-11",
        "day_label": "Fri 11 Sep",
        "starts_at": "2026-09-11T20:00:00+05:00",
        "category": None,
        "block": "• *Kaavish Live*\n🕒 8pm",
    }
    return {**fields, **overrides}


def test_save_digest_events_round_trips(store):
    store.save_digest_events(WEEK_OF, [_entry()])
    row = store._conn.execute(
        "SELECT week_of, event_date, day_label, block FROM digest_events"
    ).fetchone()
    assert tuple(row) == ("2026-08-31", "2026-09-11", "Fri 11 Sep", "• *Kaavish Live*\n🕒 8pm")


def test_save_digest_events_replaces_the_week_wholesale(store):
    """A re-render must not leave an event behind that the source has dropped."""
    store.save_digest_events(WEEK_OF, [_entry(), _entry(id="gone", block="• *Cancelled*")])
    store.save_digest_events(WEEK_OF, [_entry(block="• *Kaavish Live (moved)*")])
    rows = store._conn.execute("SELECT id, block FROM digest_events").fetchall()
    assert [tuple(r) for r in rows] == [("abc123", "• *Kaavish Live (moved)*")]


def test_save_digest_events_leaves_other_weeks_alone(store):
    store.save_digest_events(date(2026, 8, 24), [_entry(id="older")])
    store.save_digest_events(WEEK_OF, [_entry()])
    rows = store._conn.execute("SELECT id FROM digest_events ORDER BY week_of").fetchall()
    assert [r[0] for r in rows] == ["older", "abc123"]


def test_save_digest_events_accepts_an_empty_week(store):
    store.save_digest_events(WEEK_OF, [_entry()])
    store.save_digest_events(WEEK_OF, [])
    assert store._conn.execute("SELECT COUNT(*) FROM digest_events").fetchone()[0] == 0


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


# -- reading the week back out ------------------------------------------------


def test_events_in_window_round_trips_every_field(store):
    original = _event(category="music", price_text="Rs 500", series_key="Kaavish")
    store.upsert_event(original)
    window = DigestWindow.week_of(date(2026, 9, 7))
    (found,) = store.events_in_window(window)
    assert found.id == original.id  # id is derived, so it must survive the trip
    assert (found.title, found.venue, found.category) == ("Kaavish Live", original.venue, "music")
    assert found.starts_at == original.starts_at
    assert found.ends_at == original.ends_at
    assert found.sources == ["ticketwala"]
    assert found.series_key == "Kaavish"


def test_events_in_window_excludes_other_weeks(store):
    store.upsert_event(_event())  # 11 Sep
    assert store.events_in_window(DigestWindow.week_of(date(2026, 9, 7))) != []
    assert store.events_in_window(DigestWindow.week_of(date(2026, 8, 31))) == []


def test_events_in_window_is_ordered_by_start(store):
    store.upsert_event(_event(url="a", starts_at=datetime(2026, 9, 11, 20, tzinfo=KARACHI)))
    store.upsert_event(_event(url="b", starts_at=datetime(2026, 9, 11, 9, tzinfo=KARACHI)))
    found = store.events_in_window(DigestWindow.week_of(date(2026, 9, 7)))
    assert [e.starts_at.hour for e in found] == [9, 20]


def test_events_in_window_survives_an_unreadable_row(store):
    """One corrupt row must not sink the digest."""
    store.upsert_event(_event())
    store._conn.execute("UPDATE events SET starts_at = 'not a date' WHERE id = ?", (_event().id,))
    store._conn.commit()
    assert store.events_in_window(DigestWindow.week_of(date(2026, 9, 7))) == []


def test_events_in_window_handles_optional_fields_left_null(store):
    store.upsert_event(_event(venue=None, ends_at=None))
    (found,) = store.events_in_window(DigestWindow.week_of(date(2026, 9, 7)))
    assert found.venue is None and found.ends_at is None


# -- the intake queue ---------------------------------------------------------


def _queue(store, body="a listing", sender="923001234567", intake_id="i1"):
    store._conn.execute(
        "INSERT INTO intake (id, channel, sender, body, received_at) VALUES (?,?,?,?,?)",
        (intake_id, "whatsapp", sender, body, "2026-09-17T12:00:00+05:00"),
    )
    store._conn.commit()


def test_pending_intake_returns_queued_rows_oldest_first(store):
    _queue(store, "second", intake_id="b")
    store._conn.execute(
        "UPDATE intake SET received_at = ? WHERE id = 'b'", ("2026-09-17T13:00:00+05:00",)
    )
    _queue(store, "first", intake_id="a")
    store._conn.commit()
    assert [r["body"] for r in store.pending_intake()] == ["first", "second"]


def test_a_processed_row_leaves_the_queue(store):
    """A declined listing must not be retried forever.

    Left pending it would be re-extracted every run, re-paying for the same
    refusal and holding the pending count above the bot's dispatch threshold —
    which fires a pipeline run on every curator message.
    """
    _queue(store)
    store.mark_intake_processed("i1", decline_reason="declined")
    assert store.pending_intake() == []
    assert store.intake_counts() == {"total": 1, "pending": 0, "extracted": 0, "declined": 1}


def test_an_extracted_row_records_what_it_became(store):
    _queue(store)
    store.mark_intake_processed("i1", event_id="abc123")
    assert store.intake_counts()["extracted"] == 1
    row = store._conn.execute("SELECT event_id, processed_at FROM intake").fetchone()
    assert row[0] == "abc123" and row[1]


def test_pending_intake_respects_its_limit(store):
    for n in range(5):
        _queue(store, f"listing {n}", intake_id=f"i{n}")
    assert len(store.pending_intake(limit=2)) == 2


def test_intake_counts_on_an_empty_table(store):
    assert store.intake_counts() == {"total": 0, "pending": 0, "extracted": 0, "declined": 0}


# -- the events.url NOT NULL, dropped after the fact ---------------------------


def test_an_event_with_no_url_can_be_stored(store):
    """`001_init.sql` declared url NOT NULL; forwarded listings often have none.

    Two of three real WhatsApp samples say "DM us" or give a phone number, so
    without the rebuild the whole intake path fails at the first insert.
    """
    store.upsert_event(_event(url=None, source_ref="wa:abc123"))
    (found,) = store.events_in_window(DigestWindow.week_of(date(2026, 9, 7)))
    assert found.url is None
    assert found.source_ref == "wa:abc123"


def test_the_rebuild_keeps_existing_rows_and_columns(store):
    """The rebuild runs on a table that already has data and added columns."""
    store.upsert_event(_event(category="music", contact_phone="0303 5667670"))
    store._relax_not_null()  # idempotent: should be a no-op the second time
    (found,) = store.events_in_window(DigestWindow.week_of(date(2026, 9, 7)))
    assert found.title == "Kaavish Live"
    assert found.category == "music"
    assert found.contact_phone == "0303 5667670"


def test_the_rebuild_leaves_the_indexes_in_place(store):
    store._relax_not_null()
    names = {
        r[0]
        for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='events'"
        ).fetchall()
    }
    assert "idx_events_starts_at" in names


def test_other_not_nulls_survive_the_rebuild(store):
    """Only `url` is relaxed — `title` must still be required."""
    info = store._conn.execute("PRAGMA table_info(events)").fetchall()
    notnull = {row[1]: row[3] for row in info}
    assert not notnull["url"]
    assert notnull["title"]
    assert notnull["starts_at"]
