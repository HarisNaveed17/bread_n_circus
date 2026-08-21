"""Render events to Telegram MarkdownV2 digest text. Pure — no I/O.

`render(events, window, filters=None) -> list[str]`. The `filters` argument is a
reserved seam for per-subscriber variants; it is unused in v0. Each returned
string is one Telegram message, already under the 4096-char limit and split
only at day boundaries.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .models import KARACHI, DigestWindow, Event

MAX_EVENTS = 20
TELEGRAM_LIMIT = 4096
_MD2_SPECIALS = r"_*[]()~`>#+-=|{}.!\\"


def escape_md2(text: str) -> str:
    """Escape every MarkdownV2 special char. The usual cause of silent 400s."""
    out = []
    for ch in text:
        if ch in _MD2_SPECIALS:
            out.append("\\")
        out.append(ch)
    return "".join(out)


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


def _event_line(event: Event, dates: list[datetime] | None = None) -> str:
    """One bullet. `dates` (>1) means a collapsed recurring series."""
    title = escape_md2(event.series_key if dates else event.title)
    parts = [f"• *{title}*"]
    if event.venue:
        parts.append(escape_md2(event.venue))
    if dates and len(dates) > 1:
        days = ", ".join(escape_md2(f"{d.astimezone(KARACHI):%a %-d}") for d in sorted(dates))
        parts.append(f"{len(dates)}× this week: {days}")
    else:
        parts.append(escape_md2(_fmt_time(event.starts_at)))
    if event.price_text:
        parts.append(escape_md2(event.price_text))
    line = " — ".join(parts)
    return f"{line} [link]({escape_md2(event.url)})"


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
    header = f"*Islamabad — week of {escape_md2(_month_day(window.start))}*"

    in_window = sorted(
        (e for e in events if window.contains(e.starts_at)),
        key=lambda e: e.starts_at,
    )

    cut_note = ""
    if len(in_window) > MAX_EVENTS:
        cut = len(in_window) - MAX_EVENTS
        in_window = in_window[:MAX_EVENTS]
        cut_note = escape_md2(f"…and {cut} more not shown.")

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
        lines = [f"*{escape_md2(day_dt.strftime('%a %-d'))}*"]
        for e in day_events:
            skey = e.series_key or f"__{e.id}"
            group = series_all[skey]
            if len(group) > 1:
                if skey in rendered_series:
                    continue
                rendered_series.add(skey)
                # Anchor the collapsed line on this event's day.
                lines.append(_event_line(e, dates=[g.starts_at for g in group]))
            else:
                lines.append(_event_line(e))
        if len(lines) > 1:
            day_blocks.append("\n".join(lines))

    if not day_blocks:
        return [f"{header}\n\n_No events found this week\\._"]

    return _pack(header, day_blocks, cut_note)


def _pack(header: str, day_blocks: list[str], cut_note: str) -> list[str]:
    """Assemble messages, splitting at day boundaries under the char limit."""
    messages: list[str] = []
    current = header
    for block in day_blocks:
        candidate = f"{current}\n\n{block}"
        if len(candidate) > TELEGRAM_LIMIT and current != header:
            messages.append(current)
            current = block
        else:
            current = candidate
    if cut_note:
        addition = f"\n\n{cut_note}"
        if len(current) + len(addition) > TELEGRAM_LIMIT:
            messages.append(current)
            current = cut_note
        else:
            current += addition
    messages.append(current)
    return messages
