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

WEEK = "week"
DAY = "day"


@dataclass(frozen=True)
class Filter:
    """What to serve. `kind == "week"` is the default and means the whole digest."""

    kind: str
    day: date | None = None
    # How the asker put it, for the "nothing on <label>" reply. Echoing their own
    # word beats naming a date they did not use.
    label: str = "this week"


WEEK_FILTER = Filter(WEEK)


def parse(text: str, *, today: date) -> Filter:
    """Anything unrecognised is the week — the reply that was always the default.

    Tokenised rather than substring-matched so a day word has to stand alone:
    "Tomorrowland" is a plausible event title and must not silently narrow the
    digest to one day. Tomorrow is checked first so "not today, tomorrow" lands
    where the sender meant.
    """
    words = set(re.findall(r"[a-z]+", text.lower()))
    if words & TOMORROW_WORDS:
        return Filter(DAY, today + timedelta(days=1), "tomorrow")
    if words & TODAY_WORDS:
        return Filter(DAY, today, "today")
    return WEEK_FILTER
