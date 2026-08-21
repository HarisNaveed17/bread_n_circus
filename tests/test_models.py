from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from isb_events.models import KARACHI, DigestWindow, Event

UTC = ZoneInfo("UTC")


def _event(**kw) -> Event:
    base = dict(
        title="Test",
        starts_at=datetime(2026, 8, 25, 19, 0, tzinfo=KARACHI),
        url="https://example.com/e/1",
        sources=["blackhole"],
    )
    base.update(kw)
    return Event(**base)


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        _event(starts_at=datetime(2026, 8, 25, 19, 0))


def test_times_coerced_to_karachi():
    e = _event(starts_at=datetime(2026, 8, 25, 14, 0, tzinfo=UTC))
    assert e.starts_at.tzinfo == KARACHI
    assert e.starts_at.hour == 19  # UTC+5


def test_id_is_deterministic_and_source_scoped():
    a = _event()
    b = _event()
    assert a.id == b.id
    assert _event(sources=["ticketwala"]).id != a.id
    assert _event(url="https://example.com/e/2").id != a.id


def test_description_truncated_to_300():
    e = _event(description="x" * 500)
    assert len(e.description) == 300


def test_week_of_is_half_open_karachi_week():
    w = DigestWindow.week_of(date(2026, 8, 24))  # a Monday
    assert w.start == datetime(2026, 8, 24, 0, 0, tzinfo=KARACHI)
    assert w.end == datetime(2026, 8, 31, 0, 0, tzinfo=KARACHI)
    assert w.contains(datetime(2026, 8, 30, 23, 59, tzinfo=KARACHI))
    assert not w.contains(datetime(2026, 8, 31, 0, 0, tzinfo=KARACHI))


def test_coming_week_is_a_future_monday():
    # From a Wednesday, the coming week starts the following Monday.
    now = datetime(2026, 8, 19, 12, 0, tzinfo=KARACHI)
    w = DigestWindow.coming_week(now=now)
    assert w.start == datetime(2026, 8, 24, 0, 0, tzinfo=KARACHI)
