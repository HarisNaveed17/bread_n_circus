"""What did the sender actually ask for? Message text -> a digest filter.

Keyword matching, not a model. Three phrases is not a natural-language problem,
and Meta retries a webhook that takes too long to answer — an LLM call in this
path would buy nothing and cost the reply. Phase 3 (`CLAUDE.md` § Build order)
is where real Q&A goes, and it will sit *behind* this, not replace it: whatever
this recognises answers instantly and for free.

Categories join the same way — another word list mapping to another column of
`digest_events` — which is why `Filter` carries fields rather than a bare enum.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

# "tonight" is here rather than in a time-of-day filter of its own: the honest
# answer to it is today's listings, and dropping it would silently hand back the
# whole week instead.
TODAY_WORDS = frozenset({"today", "tonight"})
TOMORROW_WORDS = frozenset({"tomorrow", "tmrw", "tmr"})

# Asking for the week. A single word is enough when it is unambiguous; the
# phrases cover the ways people actually ask, since "what's on" tokenises into
# nothing useful on its own.
WEEK_WORDS = frozenset({"week", "events", "upcoming", "listings", "lineup"})
WEEK_PHRASES = ("whats on", "what is on", "whats happening", "anything on", "what s on")

WEEK = "week"
DAY = "day"
# Recognised nothing. The bot used to answer *any* message with the whole
# digest, which meant a wrong number or a "thanks!" got fifteen events back.
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Filter:
    """What to serve. `kind == "week"` is the default and means the whole digest."""

    kind: str
    day: date | None = None
    # How the asker put it, for the "nothing on <label>" reply. Echoing their own
    # word beats naming a date they did not use.
    label: str = "this week"


WEEK_FILTER = Filter(WEEK)
UNKNOWN_FILTER = Filter(UNKNOWN, label="that")


def _normalise(text: str) -> str:
    """Lowercase, apostrophes dropped, everything else to spaces.

    So "What's on?" and "whats on" are one thing, and a phrase can be matched
    as a substring without punctuation getting in the way.
    """
    lowered = text.lower().replace("'", "").replace("\u2019", "")
    return " ".join(re.findall(r"[a-z0-9]+", lowered))


def parse(text: str, *, today: date) -> Filter:
    """What was asked for, or `UNKNOWN`.

    **Unrecognised is no longer the week.** Answering every message with the
    whole digest meant a wrong number, a "thanks!", or a forwarded chain letter
    all got fifteen events back — which reads as a bot that is not listening.

    Day words are matched as whole tokens so they have to stand alone:
    "Tomorrowland" is a plausible event title and must not silently narrow the
    digest to one day. Tomorrow is checked before today so "not today,
    tomorrow" lands where the sender meant.
    """
    normalised = _normalise(text)
    words = set(normalised.split())
    if words & TOMORROW_WORDS:
        return Filter(DAY, today + timedelta(days=1), "tomorrow")
    if words & TODAY_WORDS:
        return Filter(DAY, today, "today")
    if words & WEEK_WORDS or any(phrase in normalised for phrase in WEEK_PHRASES):
        return WEEK_FILTER
    return UNKNOWN_FILTER
