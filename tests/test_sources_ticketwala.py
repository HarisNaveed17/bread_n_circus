"""M2 done-when: the Ticketwala scraper turns its public listings API into
`Event`s.

`tests/fixtures/ticketwala_events.json` and `ticketwala_workshops.json` are
trimmed captures of the real `/api/public/events/public` responses for
`city=Islamabad`. No test touches the network — `fetch()` is exercised by
monkeypatching `_fetch_page`.
"""

import json
from datetime import datetime
from pathlib import Path

from isb_events.models import KARACHI, DigestWindow
from isb_events.sources import ticketwala
from isb_events.sources.base import load_enabled_sources

FIXTURES = Path(__file__).parent / "fixtures"
EVENTS_PAYLOAD = json.loads((FIXTURES / "ticketwala_events.json").read_text())
WORKSHOPS_PAYLOAD = json.loads((FIXTURES / "ticketwala_workshops.json").read_text())
WINDOW = DigestWindow.week_of(datetime(2026, 8, 24, tzinfo=KARACHI).date())


def test_parse_extracts_all_items():
    events = ticketwala.parse(EVENTS_PAYLOAD)
    assert len(events) == 3
    assert all(e.sources == ["ticketwala"] for e in events)


def test_parse_fields_for_first_item():
    event = ticketwala.parse(EVENTS_PAYLOAD)[0]
    assert event.title == "Kaavish Live (Jinnah Convention Centre, Islamabad)"
    assert event.starts_at == datetime(2026, 9, 11, 20, 0, tzinfo=KARACHI)
    assert event.ends_at == datetime(2026, 9, 11, 23, 0, tzinfo=KARACHI)
    assert event.venue == "Jinnah Convention Centre, Club Rd, Islamabad, Pakistan"
    assert event.price_text is None
    assert event.url == "https://ticketwala.pk/event/kaavish-live-in-concert-7164"


def test_parse_skips_expired_and_incomplete_items():
    payload = {
        "items": [
            {**EVENTS_PAYLOAD["items"][0], "expired": True},
            {"id": "1", "title": "No dates", "slug": "no-dates"},
            {},
        ]
    }
    assert ticketwala.parse(payload) == []


def test_fetch_combines_events_and_workshops_across_pages(monkeypatch):
    def fake_fetch_page(listing_type, page):
        assert page == 1
        return EVENTS_PAYLOAD if listing_type == "events" else WORKSHOPS_PAYLOAD

    monkeypatch.setattr(ticketwala, "_fetch_page", fake_fetch_page)
    events = ticketwala.TicketwalaSource().fetch(WINDOW)
    assert len(events) == 5


def test_fetch_paginates_until_total_pages(monkeypatch):
    page_one = {"items": [EVENTS_PAYLOAD["items"][0]], "totalPages": 2}
    page_two = {"items": [EVENTS_PAYLOAD["items"][1]], "totalPages": 2}
    empty = {"items": [], "totalPages": 1}
    calls = []

    def fake_fetch_page(listing_type, page):
        calls.append((listing_type, page))
        if listing_type == "workshops":
            return empty
        return page_one if page == 1 else page_two

    monkeypatch.setattr(ticketwala, "_fetch_page", fake_fetch_page)
    events = ticketwala._fetch_listing_type("events")
    assert len(events) == 2
    assert calls == [("events", 1), ("events", 2)]


def test_registered_and_loaded_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ticketwala,
        "_fetch_page",
        lambda listing_type, page: (
            EVENTS_PAYLOAD if listing_type == "events" else WORKSHOPS_PAYLOAD
        ),
    )
    sources_yaml = tmp_path / "sources.yaml"
    sources_yaml.write_text("sources:\n  - slug: ticketwala\n    enabled: true\n")
    sources = load_enabled_sources(sources_yaml)
    assert [s.slug for s in sources] == ["ticketwala"]
