"""Normalisation and dedup.

`normalize` runs per event at ingest: it sets `series_key` by stripping trailing
session markers. `dedupe` merges near-duplicate records across sources.

M0 ships `normalize` (render depends on `series_key`) and a pass-through
`dedupe`. The fuzzy matching, merge policy, and threshold tuning land in M3.
"""

from __future__ import annotations

import re

from .models import Event

# "Learn Calligraphy (Session: 15)" / "Art from the Heart — Session: 53"
_SESSION_RE = re.compile(
    r"\s*(?:[—\-–:]\s*)?\(?\s*session\s*[:#]?\s*\d+\s*\)?\s*$",
    re.IGNORECASE,
)


def strip_series_marker(title: str) -> str:
    """Return the title with a trailing `(Session: N)` / `— Session: N` removed."""
    return _SESSION_RE.sub("", title).strip()


def normalize(event: Event) -> Event:
    """Set `series_key` from the title. Keeps the original `title` untouched."""
    series_key = strip_series_marker(event.title)
    if series_key == event.title and event.series_key is None:
        # No marker; series_key is just the title so render can group by it.
        series_key = event.title
    return event.model_copy(update={"series_key": series_key})


def dedupe(events: list[Event]) -> list[Event]:
    """Merge near-duplicate events. Pass-through until M3."""
    return events
