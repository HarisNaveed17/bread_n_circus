"""Fetch the page a listing links to, when the listing itself does not say when.

Organisers who run something every week write the reminder, not the date:
"IRU Monday Intervals are happening today", forwarded on some later day. The
text alone is unusable — `extract` is right to refuse it — but the link at the
bottom of the message usually knows exactly which Monday it was. This module
turns that link into plain text so the extractor can have a second look; see
`extract._second_look`.

Three things shape it:

- **It is a fetch, not a crawl.** One GET of one URL that a curator's message
  already contained, no link following beyond redirects, and one retry per
  listing at most. Nothing here searches for pages.
- **The page is untrusted input.** The URL comes from a forwarded message and
  the bytes come from whoever runs that host, so the size, the time and the
  content type are all capped before any of it reaches the model, and the
  prompt is told to treat the result as data rather than instructions. The
  curator allowlist in the bot is still the real boundary.
- **A JS shell reduces to nothing, and that is the answer.** `page_text`
  returns None rather than a page's worth of CSS when there is no readable
  text, because sending that to the model spends tokens to learn nothing.
  Where a known host hides its content behind a plain JSON endpoint, a
  resolver rewrites the URL to that endpoint instead — the Ticketwala lesson
  (CLAUDE.md § M2), applied to one more site.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from urllib.parse import urlparse

import httpx
from selectolax.parser import HTMLParser

log = logging.getLogger(__name__)

TIMEOUT = 20
MAX_BYTES = 500_000  # a page is text; anything this big is not for us
MAX_CHARS = 4_000  # ~1,000 tokens of page on top of a ~2,000-token call
MIN_USEFUL_CHARS = 40  # below this there is no date in there, whatever it is

# A URL anywhere in a message body — a registration form, a ticket page, the
# organiser's own event page. Lives here rather than in the WhatsApp adapter
# because both the adapter and the second look need the same answer.
URL_RE = re.compile(r"https?://[^\s<>\"')]+")

# Trailing punctuation is sentence, not URL: "see https://x.example/e/1." and
# "(https://x.example/e/1)" both end a character early.
_TRAILING = ".,;:!?)]}>\"'"


def first_url(text: str) -> str | None:
    """The first link in a message body, if it has one."""
    match = URL_RE.search(text or "")
    return match.group(0).rstrip(_TRAILING) if match else None


# -- what may be fetched -----------------------------------------------------

_LOCAL_HOSTS = {"localhost", "localhost.localdomain"}
_LOCAL_SUFFIXES = (".local", ".internal", ".localhost", ".home.arpa")


def is_fetchable(url: str) -> bool:
    """Is this a public web URL we are willing to GET?

    A shape check, deliberately: the host is not resolved, so this does not
    stop a public name that points at a private address. It is the cheap half
    of the defence — the expensive half is that only allowlisted curators can
    put a URL into the queue at all. What it does stop is the obvious own-goal,
    a message that links to `http://169.254.169.254/` or to something on the
    runner itself.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    if host in _LOCAL_HOSTS or host.endswith(_LOCAL_SUFFIXES):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True  # a name, not a literal address
    return address.is_global


# -- hosts that serve a shell ------------------------------------------------

# app.islamabadrunwithus.com renders nothing server-side: the page is a
# loading spinner that fetches the event from a plain, unauthenticated JSON
# endpoint (found in the page's own script, 2026-09-22). The date lives only
# there — `{"when": "Monday 21 September, 6:20 pm"}` — so fetching the HTML
# would return a stylesheet and the listing would be discarded for no reason.
_IRU_EVENT_RE = re.compile(r"^https?://app\.islamabadrunwithus\.com/event/(\d+)", re.IGNORECASE)
_IRU_API = "https://islamabadrunwithus.com/iru-api/api.php?action=eventInfo&eventId={}"


def _iru(url: str) -> str | None:
    match = _IRU_EVENT_RE.match(url)
    return _IRU_API.format(match.group(1)) if match else None


# Add a resolver when a link a curator actually forwarded turns out to be a
# shell — not speculatively. Each one is a host whose HTML was checked and
# found empty.
RESOLVERS = (_iru,)


def data_url(url: str) -> str:
    """The URL that actually carries the event details for this link."""
    for resolver in RESOLVERS:
        resolved = resolver(url)
        if resolved:
            log.info("linkpage: %s serves a shell; reading its data instead", urlparse(url).netloc)
            return resolved
    return url


# -- fetching ----------------------------------------------------------------


def _readable(content_type: str) -> bool:
    kind = content_type.split(";")[0].strip().lower()
    return kind.startswith("text/") or kind.endswith(("json", "xml", "+json", "+xml"))


def _tidy(text: str) -> str:
    """Collapse whitespace and cut to a length worth sending."""
    text = " ".join(text.split())
    return text[:MAX_CHARS]


def page_text(url: str, *, fetch=None) -> str | None:
    """The readable text of one page, or None if there is nothing worth reading.

    Never raises: a dead link, a timeout, a 403 or a PDF all cost this one
    listing its second look, and nothing else. `fetch` is the seam the tests
    use — no test in this repo touches the network.
    """
    target = data_url(url)
    if not is_fetchable(target):
        log.info("linkpage: refusing to fetch %r", url)
        return None
    try:
        body, content_type = (fetch or _get)(target)
    except Exception:
        log.warning("linkpage: could not fetch %s", target, exc_info=True)
        return None
    if body is None:
        return None
    if not _readable(content_type):
        log.info("linkpage: %s is %r, not text", target, content_type)
        return None

    text = _tidy(body if "json" in content_type else _strip_markup(body))
    if len(text) < MIN_USEFUL_CHARS:
        log.info("linkpage: %s has no readable text (a client-side page?)", target)
        return None
    return text


def _strip_markup(body: str) -> str:
    tree = HTMLParser(body)
    tree.strip_tags(["script", "style", "noscript", "svg", "head"])
    return tree.text(separator=" ") if tree.body else ""


def _get(url: str) -> tuple[str | None, str]:
    """GET a page, giving up on anything too big to be one.

    Streamed so an oversized response is abandoned mid-download rather than
    read into memory and then rejected.
    """
    with httpx.stream("GET", url, timeout=TIMEOUT, follow_redirects=True) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if not _readable(content_type):
            return None, content_type
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_BYTES:
                log.info("linkpage: %s is over %d bytes; reading the start only", url, MAX_BYTES)
                break
        return b"".join(chunks).decode(response.encoding or "utf-8", "replace"), content_type
