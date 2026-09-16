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
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from .models import KARACHI, Event

log = logging.getLogger(__name__)

# Haiku 4.5, not Opus 5. Measured on all eight real listings 2026-09-17: after
# rule 12 pinned the time format, Haiku matched Opus field for field on every
# one, including both refusals — at $2.44 per 1,000 listings against $14.91.
# Opus is one string away if extraction quality ever disappoints; the sample is
# only eight listings, and the failure that matters is inventing an event, not
# a formatting slip.
MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 2000

# Adaptive thinking exists only on 4.6-and-later models. Sending it to Haiku 4.5
# or Sonnet 4.5 is a hard 400, not a warning — so switching `MODEL` to a cheaper
# older model is **not** the one-line change it looks like. Anything not listed
# here is called without a `thinking` parameter at all, which is fine: this is a
# short structured extraction, not a reasoning task.
ADAPTIVE_THINKING_MODELS = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
)


# Measured on all eight real listings, 2026-09-17: Opus 5 with and without
# thinking produced identical extractions, at 1,905 vs 867 output tokens — 19%
# more cost for no difference. This is short structured extraction from a few
# hundred characters, not a reasoning task. Left switchable rather than ripped
# out, because a flyer-image listing may yet justify it.
USE_THINKING = False


def _thinking_kwargs(model: str) -> dict:
    if USE_THINKING and model.startswith(ADAPTIVE_THINKING_MODELS):
        return {"thinking": {"type": "adaptive"}}
    return {}


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

    **Every field is either an enum or a constrained string**, and that is a
    privacy control, not a style choice. A real forwarded newsletter carried an
    IBAN, a bank account title, a third party's mobile number and the
    recipient's name. A narrow schema *is* the PII filter: if there is nowhere
    to put an IBAN, one cannot reach the store. `decline_reason` is an enum for
    the same reason — a free-text explanation would happily quote the thing
    being filtered out.

    `registration_phone` is the one deliberate exception, and it is exactly as
    wide as it needs to be. "To register, WhatsApp: 0303 5667670" is the whole
    call to action for that event; dropping it leaves a listing nobody can act
    on. It is a number the organiser published *so that people would use it*,
    which is not the same as a bank account that happened to be in the thread —
    and `_clean_phone` enforces the difference by shape.
    """

    is_event: bool
    decline_reason: (
        Literal["not_an_event", "no_date", "no_time", "unclear", "date_conflict"] | None
    ) = None
    title: str | None = None
    date: str | None = None  # YYYY-MM-DD, Karachi
    start_time: str | None = None  # HH:MM, 24-hour
    end_time: str | None = None
    venue: str | None = None
    price_text: str | None = None
    category: str | None = None
    # The number to contact to book a place, when that is how booking works.
    # Narrow on purpose: a published registration line is the organiser's call
    # to action, not incidental personal data, and an entry without it is not
    # actionable. `_clean_phone` rejects anything that is not phone-shaped, so
    # an account number cannot ride in through this field.
    registration_phone: str | None = None
    # Which link to use, when the text has several. Picked by the model because
    # it can read the labels — "Strava" against "IRU Web" — where taking the
    # first URL found is arbitrary. Verified against the source text before use.
    event_url: str | None = None


# The prompt lives in a file, not here: it is the thing most likely to be
# edited, and a plain-text diff is far easier to review than an escaped Python
# string. Read once at import — it never changes within a run.
PROMPT_PATH = Path(__file__).parent / "prompts" / "extract.md"


def _load_prompt() -> str:
    """The prompt, minus the HTML comment that explains the file to a human."""
    text = PROMPT_PATH.read_text()
    if text.lstrip().startswith("<!--"):
        text = text.split("-->", 1)[1]
    return text.strip() + "\n"


SYSTEM = _load_prompt()


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


# A Pakistani mobile is exactly 11 digits and starts 03 ("0303 5667670"), or
# the same number with the country code in place of the leading zero
# ("+92 303 5667670"). No brackets: that is not how they are written here.
#
# This is deliberately the tightest rule that still accepts a real number. The
# field exists for "WhatsApp this number to register", so it only ever needs to
# match a mobile — and a length this exact is what makes it structurally unable
# to carry an account number or an IBAN, whatever the model was told.
PHONE_DIGITS = 11
_LOCAL_PREFIX = "03"


def _clean_phone(value: str | None) -> str | None:
    """Return the number as `0303 5667670`, or None if it is not one.

    The model is told what belongs in this field. This is the check that does
    not depend on it having listened.
    """
    if not value or "(" in value or ")" in value:
        return None
    digits = re.sub(r"\D", "", value)
    # The country code stands in for the leading zero, so put it back rather
    # than just stripping: +92 320 1234568 and 0320 1234568 are one number.
    for prefix in ("0092", "92"):
        if digits.startswith(prefix) and len(digits) == PHONE_DIGITS - 1 + len(prefix):
            digits = "0" + digits[len(prefix) :]
            break
    if len(digits) != PHONE_DIGITS or not digits.startswith(_LOCAL_PREFIX):
        return None
    return f"{digits[:4]} {digits[4:]}"


def _clean_url(value: str | None, text: str) -> str | None:
    """A URL the model chose, but only if it really is in the source text.

    The model is asked to copy a link exactly. This checks that it did: a URL
    that does not appear verbatim in the message was repaired, shortened or
    invented, and any of those would put a link in front of readers that the
    organiser never wrote. Substring rather than parsing, deliberately — the
    question is not "is this well formed" but "is this theirs".
    """
    if not value:
        return None
    value = value.strip().rstrip(".,)")
    if not value.startswith(("http://", "https://")):
        return None
    return value if value in text else None


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
        contact_phone=_clean_phone(found.registration_phone),
        url=_clean_url(found.event_url, listing.text) or listing.url,
        source_ref=listing.source_ref,
        sources=[listing.source],
    )


def extract(listing: Listing, *, client=None) -> Event | None:
    """One listing in, one `Event` or None out.

    Transient failures cost this listing, not the run: the pipeline processes a
    batch of forwarded posts and one timeout should not stop the rest.

    **A 4xx is re-raised**, because it is a bug in the request rather than a
    problem with this listing — a wrong model name, a bad key, a parameter the
    model does not accept. Swallowing those made eight malformed calls print
    eight "declined" lines, which reads as "none of these were events" and sends
    you off inspecting the listings. It will fail identically for every listing,
    so failing loudly on the first is strictly better.
    """
    try:
        response = (client or _client()).messages.parse(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            **_thinking_kwargs(MODEL),
            messages=[{"role": "user", "content": _content(listing)}],
            output_format=Extraction,
        )
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        if status is not None and 400 <= status < 500 and status != 429:
            log.error("extract: request rejected (%s) — this is a bug, not a listing", status)
            raise
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
