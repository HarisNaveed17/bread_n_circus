"""M1 done-when: the Black Hole scraper turns its listing page into `Event`s.

`tests/fixtures/blackhole.html` is a trimmed capture of the real
upcoming-events page (three `.event_listing` cards). No test touches the
network — `fetch()` is exercised by monkeypatching `_fetch_html`.
"""

from datetime import datetime
from pathlib import Path

from isb_events.models import KARACHI, DigestWindow
from isb_events.sources import blackhole
from isb_events.sources.base import load_enabled_sources

FIXTURE = (Path(__file__).parent / "fixtures" / "blackhole.html").read_text()
WINDOW = DigestWindow.week_of(datetime(2026, 8, 24, tzinfo=KARACHI).date())


def test_parse_extracts_all_cards():
    events = blackhole.parse(FIXTURE)
    assert len(events) == 3
    assert all(e.sources == ["blackhole"] for e in events)


def test_parse_fields_for_first_card():
    event = blackhole.parse(FIXTURE)[0]
    assert event.title == "Theatre, Society and the Human Experience"
    assert event.starts_at == datetime(2026, 8, 27, 18, 30, tzinfo=KARACHI)
    assert event.ends_at == datetime(2026, 8, 27, 20, 15, tzinfo=KARACHI)
    assert event.venue == "5-H, Street 100, G-11/3, Islamabad"
    assert event.category == "Theater"
    assert event.price_text == "Free"
    assert event.url.startswith("https://site.theblackhole.pk/event/")


def test_parse_when_tolerates_an_unparseable_end_time():
    starts_at, ends_at = blackhole._parse_when("Thursday, August 27, 2026 @ 06:30 PM - TBD")
    assert starts_at == datetime(2026, 8, 27, 18, 30, tzinfo=KARACHI)
    assert ends_at is None


def test_parse_defaults_missing_price_to_free():
    html = FIXTURE.replace(
        '<span class="wpem-event-ticket-type-text">Free</span>',
        "",
        1,
    )
    assert blackhole.parse(html)[0].price_text == "Free"


def test_parse_skips_a_card_missing_required_fields():
    broken = '<div class="event_listing"><h3 class="wpem-heading-text">No link or date</h3></div>'
    assert blackhole.parse(FIXTURE + broken) == blackhole.parse(FIXTURE)


def test_fetch_uses_fetch_html(monkeypatch):
    monkeypatch.setattr(blackhole, "_fetch_html", lambda: FIXTURE)
    events = blackhole.BlackHoleSource().fetch(WINDOW)
    assert len(events) == 3


def test_registered_and_loaded_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr(blackhole, "_fetch_html", lambda: FIXTURE)
    sources_yaml = tmp_path / "sources.yaml"
    sources_yaml.write_text("sources:\n  - slug: blackhole\n    enabled: true\n")
    sources = load_enabled_sources(sources_yaml)
    assert [s.slug for s in sources] == ["blackhole"]
