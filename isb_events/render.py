"""Render events to WhatsApp-flavoured digest text. Pure — no I/O.

`render(events, window, filters=None) -> list[str]`. The `filters` argument is a
reserved seam for per-subscriber variants; it is unused in v0. Each returned
string is one message, already under the 4096-char limit and split only at day
boundaries.

WhatsApp is not Markdown. It takes `*bold*` and `_italic_` and nothing else:
there is no link syntax (`[text](url)` shows up literally — bare URLs are
auto-linked instead) and there is no escape character, so the MarkdownV2
backslashes this renderer used to emit for Telegram would print as literal
backslashes. Hence no escaping anywhere below, and URLs on their own line.

Each event is a small block rather than one dense line, because the digest text
stored here is exactly what the bot replies with — the reader sees this verbatim.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .models import KARACHI, DigestWindow, Event

MAX_EVENTS = 20
WHATSAPP_LIMIT = 4096

# Field markers. Emoji rather than "Time:"/"Where:" labels: they read as
# headings at a glance, cost one character, and don't repeat a word down the
# whole message. Swap the constants if plain labels are wanted.
TIME_MARK = "🕒"
SERIES_MARK = "🔁"
VENUE_MARK = "📍"
PRICE_MARK = "🎟"


def _fmt_time(dt: datetime) -> str:
    dt = dt.astimezone(KARACHI)
    hour = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    if dt.minute:
        return f"{hour}:{dt.minute:02d}{ampm}"
    return f"{hour}{ampm}"


def _month_day(dt: datetime) -> str:
    dt = dt.astimezone(KARACHI)
    return f"{dt.day} {dt:%b}"


def _event_block(event: Event, dates: list[datetime] | None = None) -> str:
    """One event, as its own titled block. `dates` (>1) means a collapsed series.

    Time, venue and price each get their own marked line; a field the source
    didn't give us is simply left out rather than rendered blank.
    """
    title = event.series_key if dates else event.title
    lines = [f"• *{title}*"]
    if dates and len(dates) > 1:
        days = ", ".join(f"{d.astimezone(KARACHI):%a %-d}" for d in sorted(dates))
        lines.append(f"{SERIES_MARK} {len(dates)}× this week — {days}")
    else:
        lines.append(f"{TIME_MARK} {_fmt_time(event.starts_at)}")
    if event.venue:
        lines.append(f"{VENUE_MARK} {event.venue}")
    if event.price_text:
        lines.append(f"{PRICE_MARK} {event.price_text}")
    lines.append(event.url)
    return "\n".join(lines)


def _collapse_series(events: list[Event]) -> dict[str, list[Event]]:
    by_series: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        key = e.series_key or f"__{e.id}"
        by_series[key].append(e)
    return by_series


def render(
    events: list[Event],
    window: DigestWindow,
    filters: dict | None = None,  # reserved seam; unused in v0
) -> list[str]:
    header = f"*Islamabad — week of {_month_day(window.start)}*"

    in_window = sorted(
        (e for e in events if window.contains(e.starts_at)),
        key=lambda e: e.starts_at,
    )

    cut_note = ""
    if len(in_window) > MAX_EVENTS:
        cut = len(in_window) - MAX_EVENTS
        in_window = in_window[:MAX_EVENTS]
        cut_note = f"…and {cut} more not shown."

    # Group by calendar day, then collapse recurring series within the week.
    by_day: dict[str, list[Event]] = defaultdict(list)
    for e in in_window:
        day_key = e.starts_at.astimezone(KARACHI).strftime("%Y-%m-%d")
        by_day[day_key].append(e)

    series_all = _collapse_series(in_window)

    day_blocks: list[str] = []
    rendered_series: set[str] = set()
    for day_key in sorted(by_day):
        day_events = by_day[day_key]
        day_dt = day_events[0].starts_at.astimezone(KARACHI)
        # The month is in the day heading because a Mon–Sun window straddles
        # one often enough that a bare "Tue 1" under "week of 31 Aug" misleads.
        blocks = [f"*{day_dt.strftime('%a %-d %b')}*"]
        for e in day_events:
            skey = e.series_key or f"__{e.id}"
            group = series_all[skey]
            if len(group) > 1:
                if skey in rendered_series:
                    continue
                rendered_series.add(skey)
                # Anchor the collapsed block on this event's day.
                blocks.append(_event_block(e, dates=[g.starts_at for g in group]))
            else:
                blocks.append(_event_block(e))
        if len(blocks) > 1:
            day_blocks.append("\n\n".join(blocks))

    if not day_blocks:
        return [f"{header}\n\n_No events found this week._"]

    return _pack(header, day_blocks, cut_note)


def _pack(header: str, day_blocks: list[str], cut_note: str) -> list[str]:
    """Assemble messages, splitting at day boundaries under the char limit."""
    messages: list[str] = []
    current = header
    for block in day_blocks:
        candidate = f"{current}\n\n{block}"
        if len(candidate) > WHATSAPP_LIMIT and current != header:
            messages.append(current)
            current = block
        else:
            current = candidate
    if cut_note:
        addition = f"\n\n{cut_note}"
        if len(current) + len(addition) > WHATSAPP_LIMIT:
            messages.append(current)
            current = cut_note
        else:
            current += addition
    messages.append(current)
    return messages
