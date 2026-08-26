"""Scraper for Ticketwala's public Islamabad listings API.

Ticketwala is a Next.js app behind Cloudflare, but its city/date search
(`Events` and `Workshops & Classes` on the homepage) calls a plain,
unauthenticated JSON endpoint under its own domain — found by watching the
network tab while using the site's own search, not by reverse-engineering
the rendered page. That endpoint is what this scrapes; no HTML parsing,
no Cloudflare-evading TLS tricks needed.

The API has no price field on list *or* detail responses (checked both), so
`price_text` is always `None` here — unlike theblackhole.pk, Ticketwala
sells paid tickets, so there's no safe default to fall back to.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from ..models import KARACHI, DigestWindow, Event
from .base import register

log = logging.getLogger(__name__)

API_URL = "https://ticketwala.pk/api/public/events/public"
COUNTRY_ID = "167"  # Pakistan
CITY = "Islamabad"
LISTING_TYPES = ("events", "workshops")
PER_PAGE = 100
MAX_PAGES = 10  # defensive cap; Islamabad has ~15 live listings at a time

_DATE_FMT = "%Y-%m-%d %H:%M:%S"


def _parse_dt(text: str | None) -> datetime | None:
    """API datetimes are already Islamabad wall-clock time, unlabelled."""
    if not text:
        return None
    return datetime.strptime(text, _DATE_FMT).replace(tzinfo=KARACHI)


def _parse_item(item: dict) -> Event | None:
    starts_at = _parse_dt(item.get("startDate"))
    slug = item.get("slug")
    title = item.get("title")
    if item.get("expired") or not (starts_at and slug and title):
        return None
    return Event(
        title=title.strip(),
        venue=item.get("location") or None,
        starts_at=starts_at,
        ends_at=_parse_dt(item.get("endDate")),
        url=f"https://ticketwala.pk/event/{slug}",
        sources=["ticketwala"],
    )


def _fetch_page(listing_type: str, page: int) -> dict:
    resp = httpx.get(
        API_URL,
        params={
            "type": listing_type,
            "countryId": COUNTRY_ID,
            "present": "true",
            "city": CITY,
            "page": page,
            "perPage": PER_PAGE,
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def parse(payload: dict) -> list[Event]:
    events: list[Event] = []
    for item in payload.get("items", []):
        try:
            event = _parse_item(item)
        except Exception:
            log.exception("ticketwala: failed to parse an item")
            continue
        if event is not None:
            events.append(event)
    return events


def _fetch_listing_type(listing_type: str) -> list[Event]:
    events: list[Event] = []
    page = 1
    while page <= MAX_PAGES:
        payload = _fetch_page(listing_type, page)
        events.extend(parse(payload))
        if page >= payload.get("totalPages", 1):
            break
        page += 1
    return events


@register("ticketwala")
class TicketwalaSource:
    slug = "ticketwala"

    def fetch(self, window: DigestWindow) -> list[Event]:
        events: list[Event] = []
        for listing_type in LISTING_TYPES:
            events.extend(_fetch_listing_type(listing_type))
        return events
