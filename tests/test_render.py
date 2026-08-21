from datetime import datetime

from isb_events.models import KARACHI, DigestWindow, Event
from isb_events.normalize import normalize
from isb_events.render import escape_md2, render

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


def test_escape_md2_covers_specials():
    assert escape_md2("a.b-c!") == "a\\.b\\-c\\!"


def test_empty_week_renders_without_crashing():
    msgs = render([], WINDOW)
    assert len(msgs) == 1
    assert "No events found" in msgs[0]


def test_grouped_by_day_and_headed():
    msgs = render([_ev("Talk", 24), _ev("Workshop", 25)], WINDOW)
    text = "\n".join(msgs)
    assert "week of 24 Aug" in text
    assert "*Mon 24*" in text
    assert "*Tue 25*" in text


def test_recurring_series_collapsed_to_one_line():
    events = [
        _ev("Calligraphy (Session: 15)", 24, url="https://x.pk/c15"),
        _ev("Calligraphy (Session: 16)", 26, url="https://x.pk/c16"),
    ]
    msgs = render(events, WINDOW)
    text = "\n".join(msgs)
    assert text.count("Calligraphy") == 1
    assert "2× this week" in text


def test_out_of_window_events_dropped():
    msgs = render([_ev("NextWeek", 31)], WINDOW)  # 31 Aug is outside the window
    assert "No events found" in msgs[0]
