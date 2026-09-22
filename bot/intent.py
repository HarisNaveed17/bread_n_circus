"""What did the sender actually ask for? Message text -> a digest filter.

Keyword matching, not a model. Three phrases is not a natural-language problem,
and Meta retries a webhook that takes too long to answer — an LLM call in this
path would buy nothing and cost the reply. Phase 3 (`CLAUDE.md` § Build order)
is where real Q&A goes, and it will sit *behind* this, not replace it: whatever
this recognises answers instantly and for free.

Categories join the same way — another word list mapping to another column of
`digest_events` — which is why `Filter` carries fields rather than a bare enum.

**A message carries two independent axes**, a timeframe and a category, and
either may be absent: "sports events happening today" is both, "music" is a
category with no timeframe, "tomorrow" is a timeframe with no category. So the
parse scans for each separately and combines, rather than returning on the first
thing it recognises.
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
# "ksh" because the greeting tells people to text it: Kya Scene Hai.
WEEK_WORDS = frozenset({"week", "events", "upcoming", "listings", "lineup", "ksh"})
WEEK_PHRASES = ("whats on", "what is on", "whats happening", "anything on", "what s on")

WEEK = "week"
DAY = "day"
# Recognised nothing. The bot used to answer *any* message with the whole
# digest, which meant a wrong number or a "thanks!" got fifteen events back.
UNKNOWN = "unknown"

# The vocabulary, duplicated. `bot/` cannot import `isb_events` — `vercel.json`
# keeps the package out of the function bundle — so these names exist twice, and
# `test_the_bots_categories_match_the_pipelines` is what stops them drifting.
CATEGORIES = frozenset(
    {"music", "comedy", "theatre_and_film", "talks", "workshops", "sports", "social", "mixed"}
)

# What a reader might type for each. The stored value is snake_case and nobody
# types that, so every category needs at least its own plain-English word, and
# the plural is what people actually reach for ("any gigs?", "comedy tonight").
#
# `mixed` gets the most synonyms because it is the one whose name does not
# describe its contents — "Mixed Bag" is the button, but "surprise" and "random"
# are what someone types when they do not know what they want.
CATEGORY_WORDS = {
    "music": "music",
    "gig": "music",
    "gigs": "music",
    "concert": "music",
    "concerts": "music",
    "comedy": "comedy",
    "standup": "comedy",
    "theatre": "theatre_and_film",
    "theater": "theatre_and_film",
    "film": "theatre_and_film",
    "films": "theatre_and_film",
    "cinema": "theatre_and_film",
    "movie": "theatre_and_film",
    "movies": "theatre_and_film",
    "talk": "talks",
    "talks": "talks",
    "lecture": "talks",
    "lectures": "talks",
    "workshop": "workshops",
    "workshops": "workshops",
    "class": "workshops",
    "classes": "workshops",
    "sport": "sports",
    "sports": "sports",
    "running": "sports",
    "social": "social",
    "socials": "social",
    "meetup": "social",
    "meetups": "social",
    "mixed": "mixed",
    "surprise": "mixed",
    "random": "mixed",
    "misc": "mixed",
    "other": "mixed",
}
# Deliberately *not* here: "anything". "anything on today?" is one of the
# project's own week phrases (`WEEK_PHRASES`) and means the whole digest, so
# reading it as the mixed bag would silently narrow a question people already
# ask. `test_today_is_recognised` caught exactly that.

# Two-word forms, matched as a phrase because they tokenise into words that mean
# something else on their own — "stand up" is not "up", "mixed bag" is the
# button's own title and has to parse back to the category it names.
CATEGORY_PHRASES = (
    ("mixed bag", "mixed"),
    ("stand up", "comedy"),
    ("surprise me", "mixed"),
    ("a bit of everything", "mixed"),
)

# How each category is named back to a reader in "Nothing listed for X".
CATEGORY_LABELS = {
    "music": "music",
    "comedy": "comedy",
    "theatre_and_film": "theatre & film",
    "talks": "talks",
    "workshops": "workshops",
    "sports": "sports",
    "social": "social events",
    "mixed": "the mixed bag",
}


@dataclass(frozen=True)
class Filter:
    """What to serve. `kind == "week"` is the default and means the whole digest."""

    kind: str
    day: date | None = None
    # How the asker put it, for the "nothing on <label>" reply. Echoing their own
    # word beats naming a date they did not use.
    label: str = "this week"
    # A vocabulary value, or None for "everything". Independent of `kind`: a
    # category can narrow a day or a week equally.
    category: str | None = None


WEEK_FILTER = Filter(WEEK)
UNKNOWN_FILTER = Filter(UNKNOWN, label="that")


def _normalise(text: str) -> str:
    """Lowercase, apostrophes dropped, everything else to spaces.

    So "What's on?" and "whats on" are one thing, and a phrase can be matched
    as a substring without punctuation getting in the way.
    """
    lowered = text.lower().replace("'", "").replace("\u2019", "")
    return " ".join(re.findall(r"[a-z0-9]+", lowered))


def _timeframe(
    normalised: str, words: set[str], today: date
) -> tuple[str, date | None, str] | None:
    """The `(kind, day, label)` the message asks for, or None for no timeframe.

    Tomorrow before today, so "not today, tomorrow" lands where the sender meant.
    The week is checked *last* and is the weakest signal of the three, because
    "events" is a week word and "sports events happening today" contains one —
    a reader who named a day means that day.
    """
    if words & TOMORROW_WORDS:
        return DAY, today + timedelta(days=1), "tomorrow"
    if words & TODAY_WORDS:
        return DAY, today, "today"
    if words & WEEK_WORDS or any(phrase in normalised for phrase in WEEK_PHRASES):
        return WEEK, None, "this week"
    return None


def _category(normalised: str, words: set[str]) -> str | None:
    """The category the message names, or None.

    Phrases first: "stand up" tokenises into two words that mean nothing on
    their own, and "mixed bag" is a button title that has to parse back to the
    category it names.
    """
    for phrase, category in CATEGORY_PHRASES:
        if phrase in normalised:
            return category
    for word in normalised.split():
        if word in CATEGORY_WORDS:
            return CATEGORY_WORDS[word]
    return None


def parse(text: str, *, today: date) -> Filter:
    """What was asked for, or `UNKNOWN`.

    **Unrecognised is no longer the week.** Answering every message with the
    whole digest meant a wrong number, a "thanks!", or a forwarded chain letter
    all got fifteen events back — which reads as a bot that is not listening.

    Day words are matched as whole tokens so they have to stand alone:
    "Tomorrowland" is a plausible event title and must not silently narrow the
    digest to one day.

    The two axes are read independently and then combined, because a message
    can carry either or both. A category on its own means the whole week of it —
    someone texting "music" wants the gigs, not a refusal for having named no
    day.
    """
    normalised = _normalise(text)
    words = set(normalised.split())
    when = _timeframe(normalised, words, today)
    category = _category(normalised, words)

    if when is None and category is None:
        return UNKNOWN_FILTER
    if when is None:
        # A category with no timeframe: the whole week of it.
        return Filter(WEEK, None, CATEGORY_LABELS[category], category)

    kind, day, label = when
    if category is None:
        return Filter(kind, day, label)
    # "Nothing listed for music today" — the category first, then the day, which
    # is the order they were asked in.
    return Filter(kind, day, f"{CATEGORY_LABELS[category]} {label}", category)
