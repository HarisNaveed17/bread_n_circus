"""Title/venue cleanup and duplicate merging.

Every string asserted here was scraped from a real listing — the cases come
from the digest for the week of 2026-08-31, not from imagination. When a rule
changes, change it against a string a source actually produced.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from isb_events import normalize as normalize_module
from isb_events.models import KARACHI, Event
from isb_events.normalize import (
    clean_title,
    clean_venue,
    dedupe,
    normalize,
    strip_series_marker,
)


def _event(**kwargs) -> Event:
    base = {
        "title": "A Thing",
        "starts_at": datetime(2026, 9, 1, 18, 30, tzinfo=KARACHI),
        "url": "https://example.test/e/1",
        "sources": ["blackhole"],
    }
    return Event(**{**base, **kwargs})


# -- titles ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Shouted titles get de-shouted.
        ("TOTE BAG PAINTING WORKSHOP", "Tote Bag Painting Workshop"),
        ("Game Evening 4.0 - ONLY ONCE A YEAR", "Game Evening 4.0 - Only Once a Year"),
        # A lone all-caps word is an acronym far more often than emphasis.
        ("CORE - The Sunset Festival", "CORE - The Sunset Festival"),
        ("Inclusion Rocks - Islamabad", "Inclusion Rocks - Islamabad"),
        # Ordinary titles pass through untouched.
        ("Stop Commenting on My Body", "Stop Commenting on My Body"),
        ("Bol ke Lab Azad Hain Tere", "Bol ke Lab Azad Hain Tere"),
        ("The Comeback Tour by Uzair Jaswal", "The Comeback Tour by Uzair Jaswal"),
        # Whitespace is collapsed.
        ("Mehdi   Maloof\tMusic", "Mehdi Maloof Music"),
    ],
)
def test_clean_title(raw: str, expected: str) -> None:
    assert clean_title(raw) == expected


def test_lone_acronym_survives_beside_words() -> None:
    """ "PNCA" next to ordinary words must not be read as the start of a shout."""
    assert clean_title("Concert at PNCA Lawn") == "Concert at PNCA Lawn"


# -- venues ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The digest header already says Islamabad.
        ("5-H, Street 100, G-11/3, Islamabad", "5-H, Street 100, G-11/3"),
        ("G-11, Islamabad", "G-11"),
        ("2 Broke Engineers Islamabad", "2 Broke Engineers"),
        (
            "Jinnah Convention Centre, Club Rd, Islamabad, Pakistan",
            "Jinnah Convention Centre, Club Rd",
        ),
        # One sector, typed three ways by the organiser.
        (
            "Sip Coffee Co, Abbasi Market, F-8/3 F 8/3 F-8, Islamabad",
            "Sip Coffee Co, Abbasi Market, F-8/3",
        ),
        # A venue that announces it is not a venue.
        ("TBA - to be Disclosed 24-48 Hours Before Event", "TBA"),
        # Left alone.
        (
            "Pakistan National Council of the Arts (PNCA) Lawn",
            "Pakistan National Council of the Arts (PNCA) Lawn",
        ),
        ("PNCA", "PNCA"),
        ("Cafe Sol, Bahria Phase 4 Town", "Cafe Sol, Bahria Phase 4 Town"),
    ],
)
def test_clean_venue(raw: str, expected: str) -> None:
    assert clean_venue(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "Islamabad", ", Pakistan"])
def test_clean_venue_empties_become_none(raw: str | None) -> None:
    """A venue that cleans down to nothing is absent, not blank.

    `render` omits a missing venue line entirely, so returning "" here would
    print an empty marker instead of dropping the line.
    """
    assert clean_venue(raw) is None


# -- normalize ---------------------------------------------------------------


def test_normalize_applies_both_cleanups() -> None:
    event = normalize(
        _event(title="TOTE BAG PAINTING WORKSHOP", venue="Sip Coffee Co, F-8/3, Islamabad")
    )
    assert event.title == "Tote Bag Painting Workshop"
    assert event.venue == "Sip Coffee Co, F-8/3"


def test_series_key_strips_session_marker() -> None:
    assert strip_series_marker("Learn Calligraphy (Session: 15)") == "Learn Calligraphy"
    assert normalize(_event(title="Art from the Heart — Session: 53")).series_key == (
        "Art from the Heart"
    )


def test_series_key_collapses_across_shouted_sessions() -> None:
    """A caps-typed session must still group with its normally-typed siblings.

    This is why `series_key` is derived from the cleaned title rather than the
    raw one — otherwise one shouted week would split a series into two lines.
    """
    shouted = normalize(_event(title="LEARN CALLIGRAPHY (Session: 16)"))
    normal = normalize(_event(title="Learn Calligraphy (Session: 15)"))
    assert shouted.series_key == normal.series_key == "Learn Calligraphy"


def test_normalize_leaves_a_clean_event_alone() -> None:
    event = _event(title="Bol ke Lab Azad Hain Tere", venue="PNCA")
    assert normalize(event).title == event.title
    assert normalize(event).venue == event.venue


# -- dedupe (M3) -------------------------------------------------------------
#
# The asymmetry that shapes every rule here: a missed duplicate is a scruffy
# digest, a wrong merge silently deletes an event. These pin the "must not
# merge" side hardest.


def _at(day, hour=20, minute=0, title="Kaavish Live", url=None, **kw):
    return Event(
        title=title,
        starts_at=datetime(2026, 9, day, hour, minute, tzinfo=KARACHI),
        url=url or f"https://ticketwala.pk/event/{day}-{hour}-{minute}",
        sources=kw.pop("sources", ["ticketwala"]),
        **kw,
    )


def test_dedupe_merges_a_relisting_under_a_new_slug():
    """The duplicate frequent scraping actually produces: same show, new URL."""
    merged = dedupe(
        [
            _at(11, title="Kaavish Live (Jinnah Convention Centre, Islamabad)", url="a"),
            _at(
                11,
                title="Kaavish Live - Jinnah Convention Centre",
                url="b",
                price_text="Rs 3,000",
            ),
        ]
    )
    assert len(merged) == 1
    # The survivor keeps its own id/url, and gains what only the other had.
    assert merged[0].url == "a"
    assert merged[0].price_text == "Rs 3,000"


def test_dedupe_keeps_consecutive_nights_apart():
    """Ticketwala really listed Kaavish on 11 and 12 Sep 2026. Both are real.

    Verified live: two slugs, two dates, both `status: publish`. Merging them
    would delete a concert, which is why nothing in dedupe looks past a day.
    """
    nights = [
        _at(11, title="Kaavish Live (Jinnah Convention Centre, Islamabad)", url="a"),
        _at(12, title="Kaavish Live - Islamabad - 12th September", url="b"),
    ]
    assert len(dedupe(nights)) == 2


def test_dedupe_keeps_two_sittings_on_one_day_apart():
    """A morning and an evening session of one series are two events."""
    same_day = [_at(11, hour=8, title="Yoga Session: 1"), _at(11, hour=18, title="Yoga Session: 2")]
    assert len(dedupe(same_day)) == 2


def test_dedupe_keeps_different_events_at_one_venue():
    both = [
        _at(11, title="Bollywood Night", venue="PNCA", url="a"),
        _at(11, title="5th International Jazz Festival", venue="PNCA", url="b"),
    ]
    assert len(dedupe(both)) == 2


def test_dedupe_will_not_merge_across_a_venue_disagreement():
    apart = [
        _at(11, title="Open Mic Night", venue="The Black Hole, G-11/3", url="a"),
        _at(11, title="Open Mic Night", venue="Cafe Sol, Bahria Phase 4", url="b"),
    ]
    assert len(dedupe(apart)) == 2


def test_dedupe_merges_across_sources_and_unions_them():
    """The M7 case: one event listed by an organiser and a ticket seller."""
    merged = dedupe(
        [
            _at(11, hour=19, title="Open Mic Night", sources=["blackhole"], url="a"),
            _at(11, hour=19, minute=30, title="Open Mic Night!", sources=["ticketwala"], url="b"),
        ]
    )
    assert len(merged) == 1
    assert merged[0].sources == ["blackhole", "ticketwala"]
    # starts_at is the earliest time an attendee is expected, not the headline.
    assert merged[0].starts_at.hour == 19 and merged[0].starts_at.minute == 0


def test_dedupe_needs_the_fuzz_processor_to_fire_at_all():
    """Punctuation must not defeat the threshold — the bug this rule had first.

    Without `default_process` these two score 74 rather than 100, because
    "(Jinnah" and "Jinnah" are different tokens, and nothing ever merges.
    """
    assert (
        normalize_module._ratio(
            "Kaavish Live (Jinnah Convention Centre, Islamabad)",
            "Kaavish Live - Jinnah Convention Centre",
        )
        == 100
    )


def test_dedupe_is_a_no_op_on_distinct_events():
    week = [_at(d, title=f"Gig {d}") for d in range(11, 16)]
    assert len(dedupe(week)) == 5
