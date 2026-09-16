"""Draining the intake queue: forwarded text in, events out.

The model is stubbed. What is being checked is the queue discipline — that
every row leaves the queue exactly once, whatever the model said about it —
because the failure mode is a declined listing being re-extracted forever,
which both re-pays for the same refusal and holds the pending count above the
bot's dispatch threshold.
"""

import sqlite3
from datetime import datetime

import pytest

from isb_events.extract import Extraction
from isb_events.intake import drain
from isb_events.models import KARACHI
from isb_events.store import Store

BODY = "Chess N' Jams, Saturday 12th September, 5-9pm, Leafy Brew"


@pytest.fixture
def store():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with Store(conn) as s:
        yield s


def _queue(store, body=BODY, intake_id="i1", channel="whatsapp"):
    store._conn.execute(
        "INSERT INTO intake (id, channel, sender, body, received_at) VALUES (?,?,?,?,?)",
        (intake_id, channel, "923001234567", body, "2026-09-17T12:00:00+05:00"),
    )
    store._conn.commit()


class _FakeClient:
    """Returns a fixed extraction, or raises, for every call."""

    def __init__(self, result):
        outer = self

        class _M:
            def parse(self, **kw):
                if isinstance(outer.result, Exception):
                    raise outer.result
                return type("R", (), {"parsed_output": outer.result})()

        self.result = result
        self.messages = _M()


GOOD = Extraction(
    is_event=True,
    title="Chess N' Jams",
    date="2026-09-12",
    start_time="17:00",
    venue="Leafy Brew",
    price_text="Rs 1,500",
)


def test_a_listing_becomes_an_event_and_leaves_the_queue(store):
    _queue(store)
    created = drain(store, client=_FakeClient(GOOD))
    assert len(created) == 1
    assert created[0].title == "Chess N' Jams"
    assert created[0].starts_at == datetime(2026, 9, 12, 17, 0, tzinfo=KARACHI)
    assert store.pending_intake() == []
    assert store.intake_counts()["extracted"] == 1


def test_the_event_is_persisted_not_just_returned(store):
    _queue(store)
    drain(store, client=_FakeClient(GOOD))
    titles = [r[0] for r in store._conn.execute("SELECT title FROM events").fetchall()]
    assert titles == ["Chess N' Jams"]


def test_a_declined_listing_still_leaves_the_queue(store):
    _queue(store)
    assert (
        drain(store, client=_FakeClient(Extraction(is_event=False, decline_reason="no_date"))) == []
    )
    assert store.pending_intake() == []
    assert store.intake_counts()["declined"] == 1


def test_a_transient_failure_also_leaves_the_queue(store):
    """`extract` re-raises 4xx, so a None here means the answer will not change."""
    _queue(store)
    assert drain(store, client=_FakeClient(RuntimeError("overloaded"))) == []
    assert store.pending_intake() == []


def test_the_received_date_anchors_relative_dates(store):
    """ "Saturday 12th September" needs to know when it was sent."""
    _queue(store)
    client = _FakeClient(GOOD)
    seen = {}
    original = client.messages.parse

    def spy(**kw):
        seen["text"] = next(b["text"] for b in kw["messages"][0]["content"] if b["type"] == "text")
        return original(**kw)

    client.messages.parse = spy
    drain(store, client=client)
    assert "Post date: 2026-09-17" in seen["text"]


def test_an_unknown_channel_is_dropped_not_retried(store):
    _queue(store, channel="carrier-pigeon")
    assert drain(store, client=_FakeClient(GOOD)) == []
    assert store.pending_intake() == []
    row = store._conn.execute("SELECT decline_reason FROM intake").fetchone()
    assert row[0] == "no_adapter"


def test_draining_an_empty_queue_calls_nothing(store):
    """A run with no forwarded listings must not touch the model at all."""

    class _Exploding:
        messages = type("M", (), {"parse": lambda self, **kw: pytest.fail("called the model")})()

    assert drain(store, client=_Exploding()) == []


def test_a_batch_is_capped(store):
    for n in range(30):
        _queue(store, f"{BODY} {n}", intake_id=f"i{n}")
    drain(store, client=_FakeClient(GOOD), limit=25)
    assert store.intake_counts()["pending"] == 5


def test_the_same_listing_twice_is_one_event(store):
    """Two curators forwarding one post must not make two events.

    The bot dedupes on the body hash at write time; this checks the end of that
    guarantee — one row in, one event out, with a stable id.
    """
    _queue(store)
    drain(store, client=_FakeClient(GOOD))
    _queue(store, intake_id="i2")  # same body, a second row
    drain(store, client=_FakeClient(GOOD))
    assert store._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
