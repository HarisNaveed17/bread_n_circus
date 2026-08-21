"""Orchestration: fetch -> normalize -> dedupe -> persist.

A failing source must never mean no digest. Each source is fetched inside its
own try/except; failures are collected and surfaced as a digest footer, and the
pipeline carries on with whatever succeeded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .models import DigestWindow, Event
from .sources.base import Source, load_enabled_sources
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class FetchResult:
    events: list[Event] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)  # source slugs that errored

    def footer_lines(self) -> list[str]:
        return [f"⚠️ source {slug} failed" for slug in self.failures]


def fetch(window: DigestWindow, sources: list[Source] | None = None) -> FetchResult:
    """Fetch from every enabled source, isolating failures per source."""
    sources = sources if sources is not None else load_enabled_sources()
    result = FetchResult()
    for source in sources:
        try:
            events = source.fetch(window)
        except Exception:  # deliberate: one broken source can't sink the whole run
            log.exception("source %s failed", getattr(source, "slug", source))
            result.failures.append(getattr(source, "slug", "unknown"))
            continue
        result.events.extend(events)
    return result


def run_fetch(
    window: DigestWindow, store: Store, sources: list[Source] | None = None
) -> FetchResult:
    """Fetch, normalize, dedupe, and persist. Returns the (deduped) result.

    Normalization and dedup are wired in M3; until then this is fetch + persist.
    """
    from .normalize import dedupe, normalize  # noqa: PLC0415 — avoids import cycle

    result = fetch(window, sources)
    result.events = dedupe([normalize(e) for e in result.events])
    store.upsert_events(result.events)
    return result
