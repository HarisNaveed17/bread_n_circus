"""Normalisation and dedup.

`normalize` runs per event at ingest: it tidies the title and venue as scraped,
and sets `series_key` by stripping trailing session markers. `dedupe` merges
near-duplicate records across sources.

M0 shipped `normalize` (render depends on `series_key`) and a pass-through
`dedupe`. The fuzzy matching, merge policy, and threshold tuning land in M3.

The title/venue cleanups exist because the digest text is what a reader sees
verbatim over WhatsApp, and sources supply neither field in a presentable form:
Ticketwala organisers type titles in caps for emphasis, and both sources give
full postal addresses where a venue name and sector would do. Every rule here
was written against a real string observed in a live digest — the docstrings
name them. Cleanup is deliberately conservative: a rule that cannot be certain
leaves the value alone, because a scruffy listing is better than a wrong one.
"""

from __future__ import annotations

import re
from datetime import timedelta

from rapidfuzz import fuzz, utils

from .models import KARACHI, Event

# "Learn Calligraphy (Session: 15)" / "Art from the Heart — Session: 53"
_SESSION_RE = re.compile(
    r"\s*(?:[—\-–:]\s*)?\(?\s*session\s*[:#]?\s*\d+\s*\)?\s*$",
    re.IGNORECASE,
)

_WHITESPACE_RE = re.compile(r"\s+")

# A word that is entirely uppercase letters, two or more of them: "WORKSHOP",
# "ONLY". Digits and punctuation attach to the word but do not make it one.
_SHOUT_WORD = r"[A-Z][A-Z]+(?:['’][A-Z]+)?"
# Single letters ("A", "I") join a shout but never start or end one, so that a
# lone acronym beside an ordinary word is not mistaken for shouting.
_SHOUT_RUN_RE = re.compile(rf"\b{_SHOUT_WORD}(?:[\s\-–—/&]+(?:{_SHOUT_WORD}|[A-Z0-9](?![a-z])))+\b")

# Words that stay lowercase inside a de-shouted run unless they lead it.
_MINOR_WORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "but",
    "by",
    "for",
    "from",
    "in",
    "of",
    "on",
    "or",
    "the",
    "to",
    "vs",
    "with",
}

# Islamabad sector codes: "G-11/3", "F 8/3", "I-10", "E-11/2". Captured so two
# spellings of one sector can be recognised as the same place.
_SECTOR_RE = re.compile(r"\b([A-IG](?:-|\s)?\d{1,2}(?:\s?/\s?\d)?)\b")

# Trailing city/country noise. Every event in this digest is in Islamabad and
# the digest says so in its header, so repeating it on each venue line is pure
# cost. Matches with or without a comma: "…, Islamabad", "… Islamabad".
_TRAILING_PLACE_RE = re.compile(
    r"(?:^|[,\s]+)(?:islamabad|rawalpindi|pakistan)\s*$",
    re.IGNORECASE,
)

# "TBA - to be Disclosed 24-48 Hours Before Event" and friends. A venue that
# announces it is not a venue carries no information past the first token.
_TBA_RE = re.compile(r"^\s*(tba|tbd|tbc|to be announced|to be confirmed)\b.*$", re.IGNORECASE)


def strip_series_marker(title: str) -> str:
    """Return the title with a trailing `(Session: N)` / `— Session: N` removed."""
    return _SESSION_RE.sub("", title).strip()


def _title_case_run(run: str) -> str:
    """Title-case a run of shouted words, keeping minor words lowercase."""
    words = re.split(r"(\W+)", run)
    out: list[str] = []
    seen_word = False
    for part in words:
        if not part or not part[0].isalnum():
            out.append(part)
            continue
        lowered = part.lower()
        if seen_word and lowered in _MINOR_WORDS:
            out.append(lowered)
        else:
            out.append(lowered.capitalize())
        seen_word = True
    return "".join(out)


def clean_title(title: str) -> str:
    """Tidy a scraped title for display.

    De-shouts runs of two or more all-caps words: "TOTE BAG PAINTING WORKSHOP"
    becomes "Tote Bag Painting Workshop", and "Game Evening 4.0 - ONLY ONCE A
    YEAR" loses its shouted tail. A *lone* all-caps word is left alone, because
    at that length it is far more often an acronym than emphasis — "CORE - The
    Sunset Festival" and a venue called "PNCA" both survive unchanged.
    """
    cleaned = _SHOUT_RUN_RE.sub(lambda m: _title_case_run(m.group(0)), title)
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


def _canonical_sector(text: str) -> str:
    """`F 8/3` / `f-8/3` -> `F-8/3`, so two spellings compare equal."""
    compact = re.sub(r"\s+", "", text).upper()
    return re.sub(r"^([A-I])-?", r"\1-", compact)


def _dedupe_sectors(part: str) -> str:
    """Collapse repeated sector codes within one comma-separated fragment.

    Ticketwala venues are typed by organisers and often restate the sector at
    several granularities: "F-8/3 F 8/3 F-8". Keeping the first mention drops
    the repetition without guessing which spelling was intended, and a
    less-specific repeat ("F-8" after "F-8/3") is treated as the same place.
    """
    seen: list[str] = []

    def keep(match: re.Match[str]) -> str:
        canon = _canonical_sector(match.group(1))
        for earlier in seen:
            if earlier == canon or earlier.startswith(canon) or canon.startswith(earlier):
                return ""
        seen.append(canon)
        return canon

    collapsed = _SECTOR_RE.sub(keep, part)
    return _WHITESPACE_RE.sub(" ", collapsed).strip()


def clean_venue(venue: str | None) -> str | None:
    """Tidy a scraped venue for display, or return None if nothing is left.

    Three rules, each from a real listing: a "TBA - to be Disclosed 24-48 Hours
    Before Event" collapses to "TBA"; a trailing ", Islamabad" / " Pakistan" is
    dropped because the digest header already says the city; and a sector
    restated at several spellings ("F-8/3 F 8/3 F-8") keeps only its first
    mention.
    """
    if venue is None:
        return None

    venue = _WHITESPACE_RE.sub(" ", venue).strip()
    if not venue:
        return None

    if _TBA_RE.match(venue):
        return "TBA"

    # Strip trailing city/country repeatedly: "…, Islamabad, Pakistan".
    previous = None
    while previous != venue:
        previous = venue
        venue = _TRAILING_PLACE_RE.sub("", venue).strip()

    parts = [_dedupe_sectors(p.strip()) for p in venue.split(",")]
    venue = ", ".join(p for p in parts if p)
    venue = re.sub(r"\s*,\s*(?=,|$)", "", venue).strip(" ,-–—")
    return venue or None


def normalize(event: Event) -> Event:
    """Tidy title and venue, then set `series_key` from the cleaned title.

    `series_key` is derived from the *cleaned* title so that two sessions of one
    series still collapse when only one of them was typed in caps.
    """
    title = clean_title(event.title)
    venue = clean_venue(event.venue)
    series_key = strip_series_marker(title)
    return event.model_copy(
        update={"title": title, "venue": venue, "series_key": series_key or title}
    )


# How alike two titles must read before they are called the same listing.
# rapidfuzz's token_set_ratio, so word order and extra words matter less than
# the words themselves. 88 was chosen against the live store: it merges a
# re-slugged relisting while leaving every genuinely distinct pair alone.
TITLE_RATIO = 88

# Two listings for one event rarely agree on the minute. An hour is wide enough
# to absorb "doors 7:30" vs "starts 8", and far too narrow to swallow a second
# performance the same evening.
START_TOLERANCE = timedelta(hours=1)


def _ratio(a: str, b: str) -> float:
    """Token-set similarity, punctuation- and case-insensitive.

    `default_process` is not optional here: without it "Kaavish Live (Jinnah
    Convention Centre, Islamabad)" and "Kaavish Live - Jinnah Convention
    Centre" score 74 rather than 100, because "(Jinnah" and "Jinnah" are
    different tokens — so the threshold would silently never fire.
    """
    return fuzz.token_set_ratio(a, b, processor=utils.default_process)


def _same_listing(a: Event, b: Event) -> bool:
    """Are these two records the same real-world event?

    Deliberately strict, because the cost is asymmetric: a missed duplicate is
    a scruffy digest, a wrong merge silently deletes an event nobody can get
    back. Three conditions, all required.

    **Same calendar day.** This is the load-bearing one. Ticketwala listed
    Kaavish Live on 11 and 12 September 2026 under two slugs, two titles and
    one venue — both live, both published, two real concerts. Anything that
    merges across days deletes one of them, so nothing here looks past a day.

    **Start times within an hour**, so one evening's two sittings stay two.

    **Near-identical titles**, once the series marker is off. Venue is checked
    only when both sides have one, since sources disagree about how much
    address to include and a missing venue must not block a merge.
    """
    if a.starts_at.astimezone(KARACHI).date() != b.starts_at.astimezone(KARACHI).date():
        return False
    if abs(a.starts_at - b.starts_at) > START_TOLERANCE:
        return False
    if a.venue and b.venue and _ratio(a.venue, b.venue) < TITLE_RATIO:
        return False
    return _ratio(strip_series_marker(a.title), strip_series_marker(b.title)) >= TITLE_RATIO


def _merge(keep: Event, drop: Event) -> Event:
    """Fold `drop` into `keep`, preferring whichever record actually has a value.

    `keep` wins ties, so the survivor's `id` and `url` are stable across runs.
    `starts_at` takes the earlier of the two: CLAUDE.md's rule is that it must
    be the earliest time an attendee is expected, not the advertised headline.
    """
    update: dict = {
        "sources": list(dict.fromkeys([*keep.sources, *drop.sources])),
        "starts_at": min(keep.starts_at, drop.starts_at),
    }
    for field in ("venue", "price_text", "ends_at", "category", "description"):
        if getattr(keep, field) is None and getattr(drop, field) is not None:
            update[field] = getattr(drop, field)
    return keep.model_copy(update=update)


def dedupe(events: list[Event]) -> list[Event]:
    """Merge listings that describe the same event (M3).

    Quadratic, and that is fine: a week is tens of events, not thousands.
    Order is preserved, and the first record of a group is the one kept — with
    sources listed in the order they were fetched, so `Event.id` stays stable
    as long as the same source keeps finding it.
    """
    survivors: list[Event] = []
    for event in events:
        for i, kept in enumerate(survivors):
            if _same_listing(kept, event):
                survivors[i] = _merge(kept, event)
                break
        else:
            survivors.append(event)
    return survivors
