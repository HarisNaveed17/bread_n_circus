"""Turn a human-written listing into an `Event`, or decline to.

One extractor for every intake path — a forwarded Instagram link, a forwarded
WhatsApp message, a newsletter section. What differs between them is how the
text is *obtained*, not what has to be pulled out of it, so the seam is
`Listing`: each source builds one, and everything downstream is identical.

This runs in the **pipeline**, never in the bot. `bot/` reaches Turso over HTTP
with `httpx` alone so Vercel does not ship a compiled driver into a serverless
function (see CLAUDE.md § Architecture); adding an LLM client there would break
that discipline for no gain. The bot stores raw text, the pipeline parses it.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel

from .models import KARACHI, Event

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"
MAX_TOKENS = 2000


@dataclass(frozen=True)
class Listing:
    """Raw material for one candidate event.

    `url` is optional: two of three real WhatsApp samples have no link at all,
    saying "DM us" or giving a phone number. `source_ref` carries identity
    instead — see `Event.id`. A source with neither is still usable, but its id
    then depends on the title and start time.
    """

    text: str
    source: str  # the slug that lands in Event.sources
    url: str | None = None
    source_ref: str | None = None  # what Event.id hashes, when url is not it
    posted_at: date | None = None  # anchors "this Saturday" and "12th September"
    image: bytes | None = None  # a flyer, when the text alone is not enough
    image_media_type: str = "image/jpeg"


class Extraction(BaseModel):
    """The only shape the model may return.

    **Deliberately has no free-text field**, and that is a privacy control, not
    a style choice. A real forwarded newsletter carried an IBAN, a bank account
    title, a third party's mobile number and the recipient's name. A strict
    schema *is* the PII filter: if there is nowhere to put an IBAN, one cannot
    reach the store. `decline_reason` is an enum for the same reason — a
    free-text explanation would happily quote the thing being filtered out.
    """

    is_event: bool
    decline_reason: Literal["not_an_event", "no_date", "no_time", "unclear"] | None = None
    title: str | None = None
    date: str | None = None  # YYYY-MM-DD, Karachi
    start_time: str | None = None  # HH:MM, 24-hour
    end_time: str | None = None
    venue: str | None = None
    price_text: str | None = None
    category: str | None = None


SYSTEM = """\
You extract event listings for a weekly digest of things happening in \
Islamabad, Pakistan. You are given the text of a post or message written by an \
organiser, and sometimes a flyer image. Return only the structured fields.

Rules:

1. Only real, scheduled, in-person events someone could attend. A recap of a \
past event, a product advert, a job post, or a general announcement is not an \
event: set is_event false.
2. Resolve relative dates ("this Saturday", "tomorrow", "next week") against \
the post date you are given. A date with no year takes the year that makes it \
fall on or after the post date.
3. If the text gives both a weekday and a date, they are a cross-check. When \
they disagree, trust the explicit date and lower nothing else.
4. start_time is the EARLIEST time an attendee is expected, not the headline. \
When a post says doors open 18:00 and the film starts 18:30, start_time is \
18:00 — sending someone to a door that shut is worse than a vague time.
5. Never guess. If there is no date, set is_event false with decline_reason \
"no_date"; if there is no start time, "no_time". A listing whose details are \
only in the image, with a caption like "link in bio", is "unclear". These \
refusals are expected and useful — a wrong event is worse than a missing one.
6. Never invent a venue, price, title or category. Leave a field null when the \
text does not state it. Do not copy phone numbers, bank details, account \
numbers or personal names into any field.
7. price_text is what a reader should see: "Free", "Rs 2,000", "Rs 1,500-3,000". \
Prefer the per-person price. If a price is not stated, leave it null.
8. title is the event's name. If the text has no name, use a short neutral \
description of what happens ("Game Night", "Candle Making Workshop"). Do not \
copy the whole caption.
9. One message can list the same event in several cities. Extract only the \
Islamabad occurrence, with its own date and venue. If the text lists no \
Islamabad date, set is_event false with decline_reason "not_an_event".
10. When a price depends on how you buy rather than on what you get, give \
both briefly: "Rs 1,500 online, Rs 2,000 on the door". When it depends on \
group size, give the per-person price.
"""


def _label(listing: Listing) -> str:
    """Something to name a listing by in the logs. Never the body itself.

    These are strangers' messages: one carried a phone number, and real
    newsletters have carried bank details. Log what it came from, not what
    it said.
    """
    return listing.url or f"{listing.source}:{listing.source_ref or '?'}"


def _client():
    """Imported lazily so the package still imports without the SDK installed."""
    import anthropic  # noqa: PLC0415

    return anthropic.Anthropic()


def _content(listing: Listing) -> list[dict]:
    blocks: list[dict] = []
    if listing.image:
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": listing.image_media_type,
                    "data": base64.standard_b64encode(listing.image).decode(),
                },
            }
        )
    posted = listing.posted_at.isoformat() if listing.posted_at else "unknown"
    blocks.append({"type": "text", "text": f"Post date: {posted}\n\n{listing.text}"})
    return blocks


def _starts_at(day: str, clock: str) -> datetime | None:
    try:
        parsed_day = date.fromisoformat(day)
        hour, _, minute = clock.partition(":")
        return datetime.combine(parsed_day, time(int(hour), int(minute)), tzinfo=KARACHI)
    except (ValueError, TypeError):
        return None


def to_event(found: Extraction, listing: Listing) -> Event | None:
    """Build an `Event`, or None if the extraction is not usable.

    Re-checks what the prompt asks for rather than trusting it: `is_event` true
    with a missing date still means no event, because `Event.starts_at` is
    required and a digest grouped by day cannot hold an undated listing.
    """
    if not found.is_event:
        log.info(
            "extract: declined (%s) for %s",
            found.decline_reason or "no reason",
            _label(listing),
        )
        return None
    if not (found.title and found.date and found.start_time):
        log.info("extract: incomplete extraction for %s; dropping", _label(listing))
        return None

    starts_at = _starts_at(found.date, found.start_time)
    if starts_at is None:
        log.warning("extract: unparseable date/time %r %r", found.date, found.start_time)
        return None
    ends_at = _starts_at(found.date, found.end_time) if found.end_time else None
    if ends_at is not None and ends_at < starts_at:
        ends_at = None  # crossed midnight, or simply wrong; a bad end time is not worth keeping

    return Event(
        title=found.title.strip(),
        venue=(found.venue or "").strip() or None,
        starts_at=starts_at,
        ends_at=ends_at,
        category=(found.category or "").strip() or None,
        price_text=(found.price_text or "").strip() or None,
        url=listing.url,
        source_ref=listing.source_ref,
        sources=[listing.source],
    )


def extract(listing: Listing, *, client=None) -> Event | None:
    """One listing in, one `Event` or None out. Never raises.

    A failed call must cost this listing, not the run: the pipeline processes a
    batch of forwarded posts and one malformed message should not stop the rest.
    """
    try:
        response = (client or _client()).messages.parse(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": _content(listing)}],
            output_format=Extraction,
        )
    except Exception:
        log.exception("extract: the model call failed for %s", _label(listing))
        return None

    found = getattr(response, "parsed_output", None)
    if found is None:
        log.warning("extract: no parsed output for %s", _label(listing))
        return None
    return to_event(found, listing)


def extract_all(listings: list[Listing], *, client=None) -> list[Event]:
    """Extract a batch, keeping whatever succeeds."""
    events = [extract(listing, client=client) for listing in listings]
    return [e for e in events if e is not None]
