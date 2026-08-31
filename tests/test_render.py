r"""The renderer emits WhatsApp text, which is *not* Markdown.

The rules being pinned here: no backslash escaping (WhatsApp has no escape
character, so a `\-` prints as a backslash), no `[text](url)` link syntax
(WhatsApp shows it literally and auto-links bare URLs), `*bold*` only, and one
marked line per field so the reader can scan time and address separately.
"""

from datetime import datetime

from isb_events.models import KARACHI, DigestWindow, Event
from isb_events.normalize import normalize
from isb_events.render import render

WINDOW = DigestWindow.week_of(datetime(2026, 8, 24, tzinfo=KARACHI).date())


def _ev(title, day, hour=19, **kw):
    return normalize(
        Event(
            title=title,
            starts_at=datetime(2026, 8, day, hour, 0, tzinfo=KARACHI),
            url=kw.pop("url", f"https://x.pk/{title}"),
            sources=["blackhole"],
            **kw,
        )
    )


def test_empty_week_renders_without_crashing():
    msgs = render([], WINDOW)
    assert len(msgs) == 1
    assert "No events found" in msgs[0]


def test_grouped_by_day_and_headed():
    msgs = render([_ev("Talk", 24), _ev("Workshop", 25)], WINDOW)
    text = "\n".join(msgs)
    assert "week of 24 Aug" in text
    assert "*Mon 24 Aug*" in text
    assert "*Tue 25 Aug*" in text


def test_nothing_is_escaped():
    """A title full of MarkdownV2 specials must survive byte-for-byte."""
    title = "Mir: The Parallel Universes - a talk (part 1.5)!"
    text = "\n".join(render([_ev(title, 24, venue="G-11/3, Islamabad")], WINDOW))
    assert title in text
    # `normalize` drops the ", Islamabad" suffix; the sector must still arrive
    # with its hyphen and slash unescaped, which is what this test is about.
    assert "G-11/3" in text
    assert "\\" not in text


def test_url_is_bare_not_a_markdown_link():
    url = "https://ticketwala.pk/event/bollywood-night-7234"
    text = "\n".join(render([_ev("Bollywood Night", 24, url=url)], WINDOW))
    assert f"\n{url}" in text
    assert "[link]" not in text
    assert "](" not in text


def test_time_venue_and_price_each_get_their_own_marked_line():
    text = "\n".join(
        render(
            [_ev("Talk", 24, hour=18, venue="PNCA, Islamabad", price_text="Free")],
            WINDOW,
        )
    )
    assert "• *Talk*" in text
    assert "🕒 6pm" in text
    assert "📍 PNCA" in text
    assert "🎟 Free" in text


def test_missing_fields_are_omitted_not_rendered_blank():
    """Ticketwala never supplies a price and some cards have no venue."""
    text = "\n".join(render([_ev("Talk", 24)], WINDOW))
    assert "📍" not in text
    assert "🎟" not in text
    assert "🕒 7pm" in text


def test_recurring_series_collapsed_to_one_block():
    events = [
        _ev("Calligraphy (Session: 15)", 24, url="https://x.pk/c15"),
        _ev("Calligraphy (Session: 16)", 26, url="https://x.pk/c16"),
    ]
    text = "\n".join(render(events, WINDOW))
    assert text.count("Calligraphy") == 1
    assert "🔁 2× this week — Mon 24, Wed 26" in text


def test_out_of_window_events_dropped():
    msgs = render([_ev("NextWeek", 31)], WINDOW)  # 31 Aug is outside the window
    assert "No events found" in msgs[0]
