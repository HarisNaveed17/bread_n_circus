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


# -- prices, which the API does not carry ------------------------------------
#
# `entry_fee` and `isFree` are null on every API response and no "Rs" string
# appears anywhere in it, so prices need a second GET against the event page.
# Both fixtures are real captures; `_event_group_ticket` is `connected-6975`
# and `_event_single_price` is `carnival-of-lights-7279`.

GROUP_PAGE = (FIXTURES / "ticketwala_event_group_ticket.html").read_text()
SINGLE_PAGE = (FIXTURES / "ticketwala_event_single_price.html").read_text()
MULTI_SHOW_PAGE = (FIXTURES / "ticketwala_event_multi_show.html").read_text()


def test_tickets_are_read_from_the_next_payload():
    """There is no __NEXT_DATA__; the tiers live in an escaped JS string."""
    tickets = ticketwala.tickets_from_page(GROUP_PAGE, "connected-6975")
    assert [(t["title"], t["price"], t["persons"]) for t in tickets] == [
        ("Standard Ticket", "1350.00", "1"),
        ("Group of 5", "4750.00", "5"),
    ]


def test_group_tickets_are_kept_out_of_the_range():
    """Rs 4,750 for five is Rs 950 a head — folding it in would treble the price.

    The event really costs Rs 1,350 to attend, and a "Rs 1,350–4,750" line
    would send readers away thinking otherwise.
    """
    assert ticketwala.price_text_from_page(GROUP_PAGE, "connected-6975") == "Rs 1,350"


def test_a_single_tier_renders_without_a_range():
    assert ticketwala.price_text_from_page(SINGLE_PAGE, "carnival-of-lights-7279") == "Rs 2,000"


def test_every_ticket_array_is_read_not_just_the_first():
    """Kaavish Live carries six `tickets` arrays, one per seating block.

    Reading only the first returned its lone wheelchair tier at Rs 10,000 and
    hid seventeen others — a wrong price, which is worse than no price.
    """
    tickets = ticketwala.tickets_from_page(MULTI_SHOW_PAGE, "kaavish-live-in-concert-7164")
    assert len(tickets) == 18
    assert len({t["eventShowId"] for t in tickets}) > 1
    assert (
        ticketwala.price_text_from_page(MULTI_SHOW_PAGE, "kaavish-live-in-concert-7164")
        == "Rs 3,000–18,000"
    )


def test_tickets_are_matched_to_the_event_by_id():
    """The slug ends in the event id, and every ticket carries `eventId`."""
    assert ticketwala.tickets_from_page(GROUP_PAGE, "some-other-event-9999") == []


def test_a_page_with_no_tickets_yields_no_price():
    assert ticketwala.price_text_from_page("<html><body>nothing</body></html>") is None
    assert ticketwala.price_text_from_page("") is None


def test_a_broken_page_never_raises():
    """A redesign must cost one price, not the whole digest."""
    assert ticketwala.price_text_from_page('\\"tickets\\":[{"unbalanced"') is None
    assert ticketwala.price_text_from_page('\\"tickets\\":[not json]') is None


def test_free_tickets_render_as_free():
    page = '"tickets":[{"eventId":"1","price":"0.00","persons":"1","isFree":"yes"}]'
    assert ticketwala.price_text_from_page(page, "x-1") == "Free"


def test_prices_are_only_fetched_for_events_inside_the_window(monkeypatch):
    """The listing API returns months of events; the digest shows one week."""
    asked = []

    def fake_fetch_page(listing_type, page):
        return EVENTS_PAYLOAD if listing_type == "events" else WORKSHOPS_PAYLOAD

    def fake_price(slug):
        asked.append(slug)
        return "Rs 999"

    monkeypatch.setattr(ticketwala, "_fetch_page", fake_fetch_page)
    monkeypatch.setattr(ticketwala, "fetch_price_text", fake_price)

    events = ticketwala.TicketwalaSource().fetch(WINDOW)
    in_window = [e for e in events if WINDOW.contains(e.starts_at)]
    assert asked, "expected at least one in-window event to be priced"
    assert len(asked) == len(in_window)
    assert all(e.price_text == "Rs 999" for e in in_window)
    assert all(e.price_text is None for e in events if not WINDOW.contains(e.starts_at))


def test_a_failed_price_fetch_leaves_the_event_intact(monkeypatch):
    monkeypatch.setattr(ticketwala, "_fetch_page", lambda t, p: EVENTS_PAYLOAD)
    monkeypatch.setattr(ticketwala, "fetch_price_text", lambda slug: None)
    events = ticketwala.TicketwalaSource().fetch(WINDOW)
    assert events and all(e.price_text is None for e in events)
