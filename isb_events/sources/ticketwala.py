"""Scraper for Ticketwala's public Islamabad listings API.

Ticketwala is a Next.js app behind Cloudflare, but its city/date search
(`Events` and `Workshops & Classes` on the homepage) calls a plain,
unauthenticated JSON endpoint under its own domain — found by watching the
network tab while using the site's own search, not by reverse-engineering
the rendered page. That endpoint is what this scrapes; no HTML parsing,
no Cloudflare-evading TLS tricks needed.

The API has no price field on list *or* detail responses (re-checked
2026-09-05: `entry_fee` and `isFree` are null, and no "Rs" string appears
anywhere in the 96-key detail response — the populated `platformFee` and
`paymentProcessingFee` are booking fees, not the ticket). Prices come from a
second GET against the event *page*; see `price_text_from_page`.
"""

from __future__ import annotations

import json
import logging
import re
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

EVENT_URL = "https://ticketwala.pk/event/{slug}"
PAGE_TIMEOUT = 20

# The event page is a Next.js app-router render: the ticket tiers arrive inside
# `self.__next_f.push([...])` as JSON embedded in a JS string literal, so every
# quote in it is backslash-escaped. There is no `__NEXT_DATA__` blob.
_TICKETS_ESCAPED = re.compile(r'\\"tickets\\":\s*\[')
_TICKETS_PLAIN = re.compile(r'"tickets":\s*\[')

# A slug ends in the event's own id ("connected-6975"). Tickets carry `eventId`,
# so the two can be cross-checked — cheap insurance against a future page that
# also embeds a "related events" block.
_SLUG_ID = re.compile(r"-(\d+)$")


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


def _balanced_array(text: str, open_at: int) -> str | None:
    """The `[...]` starting at `open_at`, honouring nesting. None if unbalanced.

    Bracket characters are never backslash-escaped in the payload, so this works
    on the raw text without decoding it first.
    """
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return text[open_at : i + 1]
    return None


def tickets_from_page(html: str, slug: str | None = None) -> list[dict]:
    """Every ticket tier on an event page, or [] if it has none.

    **All** `tickets` arrays, not the first. A page carries one array per
    `eventShowId` — a seating block or a separate showing — and Kaavish Live
    has six. Reading only the first returned its single wheelchair tier at
    Rs 10,000 and hid seventeen others spanning Rs 3,000-18,000, which is a
    wrong price rather than a missing one.

    Never raises: a redesigned or error-shelled page must cost this event its
    price, not the whole digest.
    """
    event_id = _SLUG_ID.search(slug or "")
    for pattern, escaped in ((_TICKETS_ESCAPED, True), (_TICKETS_PLAIN, False)):
        tickets: list[dict] = []
        for match in pattern.finditer(html):
            blob = _balanced_array(html, match.end() - 1)
            if blob is None:
                continue
            if escaped:
                # Let the JSON decoder undo the escaping — `\uXXXX` and
                # non-ASCII both survive, which `unicode_escape` mangles.
                try:
                    blob = json.loads(f'"{blob}"')
                except json.JSONDecodeError:
                    continue
            try:
                parsed = json.loads(blob)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, list):
                continue
            tickets.extend(t for t in parsed if isinstance(t, dict))
        if event_id:
            tickets = [t for t in tickets if str(t.get("eventId")) == event_id.group(1)]
        if tickets:
            return tickets
    return []


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def price_text_from_page(html: str, slug: str | None = None) -> str | None:
    """A display price for one event, or None if the page shows no tickets.

    **Group tickets are excluded from the range.** "Group of 5" at Rs 4,750 is
    Rs 950 a head, and folding it into a min-max would advertise a Rs 1,350
    event as costing up to Rs 4,750. Only `persons == 1` tiers set the headline;
    if an event sells *nothing* but group tickets, those are used rather than
    reporting no price at all.
    """
    tickets = tickets_from_page(html, slug)
    if not tickets:
        return None
    if all(str(t.get("isFree", "")).lower() == "yes" for t in tickets):
        return "Free"

    singles = [t for t in tickets if int(_as_float(t.get("persons"), 1.0)) == 1]
    prices = sorted({_as_float(t.get("price"), 0.0) for t in (singles or tickets)} - {0.0})
    if not prices:
        return "Free"
    low, high = prices[0], prices[-1]
    if low == high:
        return f"Rs {int(low):,}"
    return f"Rs {int(low):,}–{int(high):,}"


def fetch_price_text(slug: str) -> str | None:
    """One extra GET per event. Any failure means no price, never a crash."""
    try:
        resp = httpx.get(EVENT_URL.format(slug=slug), timeout=PAGE_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
    except Exception:
        log.warning("ticketwala: could not fetch the event page for %s", slug, exc_info=True)
        return None
    return price_text_from_page(resp.text, slug)


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


def _slug_of(event: Event) -> str:
    return event.url.rsplit("/", 1)[-1]


def add_prices(events: list[Event], window: DigestWindow) -> list[Event]:
    """Fill `price_text` from each event page. One GET per *in-window* event.

    Scoped to the window on purpose. The listing API returns everything upcoming
    — months of it — and the digest only ever shows one week, so pricing the
    rest would multiply the request count for output nobody reads. At the
    current twice-daily cadence this is roughly 20 requests a day.
    """
    priced: list[Event] = []
    for event in events:
        if not window.contains(event.starts_at) or event.price_text:
            priced.append(event)
            continue
        price = fetch_price_text(_slug_of(event))
        priced.append(event.model_copy(update={"price_text": price}) if price else event)
    return priced


@register("ticketwala")
class TicketwalaSource:
    slug = "ticketwala"

    def fetch(self, window: DigestWindow) -> list[Event]:
        events: list[Event] = []
        for listing_type in LISTING_TYPES:
            events.extend(_fetch_listing_type(listing_type))
        return add_prices(events, window)
