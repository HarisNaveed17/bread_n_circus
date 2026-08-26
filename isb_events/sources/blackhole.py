"""Scraper for The Black Hole's upcoming-events page.

The site runs WP Event Manager (WPEM); events render server-side into
`.event_listing` cards on a single page — no pagination as of M1, since the
venue rarely has more than a handful of events live at once. A malformed
card must not sink the whole fetch, so cards are parsed one at a time with
failures logged and skipped.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

import httpx
from selectolax.parser import HTMLParser, Node

from ..models import KARACHI, DigestWindow, Event
from .base import register

log = logging.getLogger(__name__)

LISTING_URL = "https://theblackhole.pk/upcoming-events/"
USER_AGENT = "Mozilla/5.0 (compatible; isb-events/0.1; +https://github.com/)"

_CATEGORY_RE = re.compile(r"event_listing_category-([\w-]+)")
_DATE_FMT = "%A, %B %d, %Y"
_TIME_FMT = "%I:%M %p"


def _clean(text: str | None) -> str:
    return " ".join((text or "").split())


def _parse_category(class_attr: str) -> str | None:
    m = _CATEGORY_RE.search(class_attr)
    return m.group(1).replace("-", " ").title() if m else None


def _parse_when(text: str) -> tuple[datetime, datetime | None]:
    """Parse e.g. "Thursday, August 27, 2026 @ 06:30 PM - 08:15 PM" -> (start, end).

    Strict `strptime`, not `dateutil`: the site's format is fixed, so a strict
    parse fails loudly (and per-card, not per-fetch) the day the site changes
    its markup, instead of silently misreading a date.
    """
    date_part, _, time_part = text.partition("@")
    day = datetime.strptime(date_part.strip(), _DATE_FMT).date()
    start_text, _, end_text = time_part.strip().partition("-")
    starts_at = datetime.combine(
        day, datetime.strptime(start_text.strip(), _TIME_FMT).time(), tzinfo=KARACHI
    )
    ends_at = None
    if end_text.strip():
        try:
            ends_at = datetime.combine(
                day, datetime.strptime(end_text.strip(), _TIME_FMT).time(), tzinfo=KARACHI
            )
        except ValueError:
            # The start time is the important half; an unparseable end time
            # shouldn't sink the whole card.
            log.warning("blackhole: could not parse end time %r", end_text.strip())
    return starts_at, ends_at


def _text(node: Node, selector: str) -> str | None:
    found = node.css_first(selector)
    return _clean(found.text()) if found else None


def _parse_card(node: Node) -> Event | None:
    link = node.css_first("a.wpem-event-action-url")
    title = _text(node, "h3.wpem-heading-text")
    when_text = _text(node, ".wpem-event-date-time-text")
    href = link.attributes.get("href") if link is not None else None
    if not (href and title and when_text):
        return None
    starts_at, ends_at = _parse_when(when_text)
    return Event(
        title=title,
        venue=_text(node, ".wpem-event-location-text"),
        starts_at=starts_at,
        ends_at=ends_at,
        category=_parse_category(node.attributes.get("class") or ""),
        # Every Black Hole event observed so far is free; a missing ticket-type
        # node is treated as "Free" rather than left blank.
        price_text=_text(node, ".wpem-event-ticket-type-text") or "Free",
        url=href,
        sources=["blackhole"],
    )


def parse(html: str) -> list[Event]:
    events: list[Event] = []
    for node in HTMLParser(html).css(".event_listing"):
        try:
            event = _parse_card(node)
        except Exception:
            log.exception("blackhole: failed to parse an event card")
            continue
        if event is not None:
            events.append(event)
    return events


def _fetch_html() -> str:
    resp = httpx.get(
        LISTING_URL, headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True
    )
    resp.raise_for_status()
    return resp.text


@register("blackhole")
class BlackHoleSource:
    slug = "blackhole"

    def fetch(self, window: DigestWindow) -> list[Event]:
        return parse(_fetch_html())
