"""Turn a forwarded WhatsApp message into a `Listing` for `extract`.

The adapter is thin on purpose — the message body *is* the text, so there is no
fetching and no parsing. Everything that makes a listing hard is handled once,
in `extract`, which is the point of the seam: Instagram and WhatsApp differ in
how the text arrives, not in what has to come out of it.

Three things this path has that the Instagram one does not:

- **No post date.** A caption carries "on August 18, 2026" in its `og:`
  metadata; a forwarded message carries nothing. The receipt time stands in for
  it, which is why `listing_from_message` takes `received_at` — without an
  anchor, "this Saturday" and a bare "12th September" have no year and the
  extractor is right to refuse them.
- **Usually no link.** Two of three real samples say "DM us" or give a phone
  number, so `Event.url` is optional and `source_ref` carries identity instead.
- **Untrusted content.** These are bodies typed by strangers and fed to a
  model. The curator allowlist lives in the bot, not here, but the strict
  extraction schema is the second line: a phone number in the text has no field
  to land in.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import date

from ..extract import Listing
from ..linkpage import first_url

log = logging.getLogger(__name__)

SLUG = "whatsapp"

# Curators prefix forwards with the organiser, which is the only reliable
# attribution a message carries. Optional: a bare forward still works.
_ORGANISER_RE = re.compile(r"^\s*organi[sz]er\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)

__all__ = ["SLUG", "first_url", "listing_from_message", "organiser", "source_ref"]


def organiser(text: str) -> str | None:
    match = _ORGANISER_RE.search(text)
    return match.group(1) if match else None


def source_ref(text: str) -> str:
    """A stable identity for a message with no URL.

    Hashes the body, so the same listing forwarded twice upserts to one row
    while two listings from one organiser stay distinct. Using the organiser's
    page instead — the earlier plan — would have collapsed everything they ever
    send into a single event, because the store upserts by id.
    """
    return hashlib.sha256(text.strip().encode()).hexdigest()[:32]


def listing_from_message(text: str, *, received_at: date) -> Listing | None:
    """Build a `Listing` from one forwarded message body.

    `received_at` anchors relative and year-less dates. It is the day the bot
    was sent the message, not the day the pipeline runs — a message forwarded
    on Friday and processed on Monday still means Friday's "tomorrow".
    """
    text = (text or "").strip()
    if not text:
        return None
    return Listing(
        text=text,
        source=SLUG,
        url=first_url(text),
        source_ref=source_ref(text),
        posted_at=received_at,
    )
