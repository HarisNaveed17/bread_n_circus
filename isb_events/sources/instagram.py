"""Read a public Instagram post into a `Listing` for `extract`.

Not a scraper and not a crawl: it fetches exactly the posts a curator has
forwarded. Instagram is never searched, browsed, or logged into — the account
automation that was considered and rejected (CLAUDE.md § Instagram automation)
risked the Meta developer account that hosts the WhatsApp delivery channel, and
nothing here touches an account at all.

**The whole caption is in the `og:` tags**, so the common case needs no vision:
venue, date, time and price all arrive as text. Verified 2026-09-01 against
five real posts, and again from a GitHub runner, where an Azure egress IP got
byte-identical content in 0.7s.

Two things that look backwards and are not:

- **Plain `httpx` works; a browser User-Agent does not.** The default UA gets
  the `og:` tags, a Chrome UA gets the JS shell with none. Instagram serves
  crawler metadata to things that look like link-preview crawlers, which is
  what those tags are for. `curl_cffi` also works and is not needed.
- **`og:image` is a centre-cropped square**, 640x640 out of a larger original,
  and the crop is inside the URL signature — altering `stp=` returns 403. A
  flyer's own title gets sliced off at the edges, so a link cannot feed vision
  reliably. When the detail is in the image, the curator forwards a screenshot
  instead; that path supplies real bytes.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import date, datetime

import httpx

from ..extract import Listing

log = logging.getLogger(__name__)

SLUG = "instagram"
TIMEOUT = 25

# Accepts the share-button form, which carries `?utm_source=ig_web_copy_link`.
POST_URL_RE = re.compile(r"https?://(?:www\.)?instagram\.com/(?:p|reel)/([\w-]+)/?(?:\?\S*)?")

_META_RE = "<meta[^>]+property=[\"']{}[\"'][^>]+content=[\"']([^\"']*)"

# og:description opens with engagement counts and the post date:
#   `33 likes, 3 comments - thesocialchessclub_ on August 18, 2026: "..."`
# The date is what resolves "this Sunday" into a real day, so it is worth
# pulling out separately rather than leaving the model to find it.
_POSTED_RE = re.compile(r"-\s*([\w.]+)\s+on\s+([A-Z][a-z]+ \d{1,2}, \d{4})\s*:")


def _meta(page: str, prop: str) -> str | None:
    match = re.search(_META_RE.format(prop), page)
    return html.unescape(match.group(1)) if match else None


def canonical_url(url: str) -> str | None:
    """Strip tracking parameters down to the bare post URL, or None if not one."""
    match = POST_URL_RE.match(url.strip())
    return f"https://www.instagram.com/p/{match.group(1)}/" if match else None


def parse_posted_at(description: str | None) -> date | None:
    match = _POSTED_RE.search(description or "")
    if not match:
        return None
    try:
        return datetime.strptime(match.group(2), "%B %d, %Y").date()
    except ValueError:
        return None


def listing_from_page(page: str, url: str) -> Listing | None:
    """Build a `Listing` from a fetched post, or None if it carries no caption.

    Prefers `og:url` for the event's link because it is the canonical form and
    includes the organiser's handle — and, more importantly, it is unique per
    post, which is what keeps `Event.id` from collapsing every listing by one
    organiser into a single row.
    """
    description = _meta(page, "og:description")
    title = _meta(page, "og:title")
    caption = description or title
    if not caption:
        log.warning("instagram: no og: caption on %s", url)
        return None
    return Listing(
        text=caption,
        source=SLUG,
        url=_meta(page, "og:url") or url,
        posted_at=parse_posted_at(description),
    )


def fetch_listing(url: str) -> Listing | None:
    """Fetch one forwarded post. Never raises — a bad link costs that link only."""
    canonical = canonical_url(url)
    if canonical is None:
        log.info("instagram: not a post URL, ignoring: %r", url)
        return None
    try:
        # No custom User-Agent: httpx's default is what gets served the og: tags.
        response = httpx.get(canonical, timeout=TIMEOUT, follow_redirects=True)
        response.raise_for_status()
    except Exception:
        log.exception("instagram: could not fetch %s", canonical)
        return None
    return listing_from_page(response.text, canonical)
