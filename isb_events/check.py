"""Is the live system still doing its job? Invariants the store must satisfy.

Every failure this project has actually had was silent. theblackhole.pk
answered a rate limit with an empty HTTP 200 and vanished from six digests
while the run went green. A workflow persisted to a runner-local file and
reported success. A migration never reached Turso and the bot hit `no such
table` at runtime. Nothing raised, nothing was red, and each was found by a
person noticing the digest looked thin.

So this does not test code paths — the unit tests do that. It reads the store
the way the bot will and asks whether the *data* says the machinery ran:

- Is there a digest for the week today falls in, and is it recent?
- Has every enabled source contributed an event recently? A source whose
  scrape returns nothing keeps its old events in the digest (render reads the
  store, not the fetch), which is exactly why nobody notices — but its
  `last_seen` stops advancing, and that is visible.
- Would the bot's default reply have anything in it?
- Is the curator queue being drained?

Pure over the store: `check()` returns problems, `summary()` returns lines, and
the CLI decides the exit status. Thresholds are wide on purpose. The cron lands
2.5-5 hours late and a single dropped firing is expected (CLAUDE.md § Open
threads); one miss must pass, two in a row must not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .models import KARACHI, DigestWindow
from .store import Store

FAIL = "FAIL"

# Two renders a day, each up to five hours late: a healthy digest is never more
# than ~12h old, and 18h means one firing was dropped and the next has not
# landed yet — which the project treats as expected. 36h is two misses.
MAX_DIGEST_AGE = timedelta(hours=18)
# A source is re-seen on every render that finds its events, so 36h is roughly
# three consecutive fetches that came back empty for it.
MAX_SOURCE_AGE = timedelta(hours=36)
# A queued listing outlives two scheduled runs only if the drain is not
# running — the API key is unset on the runner, or the step is failing.
MAX_INTAKE_AGE = timedelta(hours=24)
# What the bot's default reply covers. Must match `bot/app.py` UPCOMING_DAYS;
# the bot cannot import this package so the number is duplicated, not shared.
UPCOMING_DAYS = 7


@dataclass(frozen=True)
class Problem:
    level: str
    message: str

    def __str__(self) -> str:
        return f"{self.level}: {self.message}"


def _hours(delta: timedelta) -> str:
    return f"{delta.total_seconds() / 3600:.1f}h"


def check(
    store: Store,
    *,
    now: datetime,
    sources: list[str],
    week_of: date | None = None,
    max_digest_age: timedelta = MAX_DIGEST_AGE,
    max_source_age: timedelta = MAX_SOURCE_AGE,
    max_intake_age: timedelta = MAX_INTAKE_AGE,
) -> list[Problem]:
    """Every way the store says the pipeline has stopped, or `[]` if it has not.

    `sources` is the list of slugs that are *supposed* to be contributing — the
    enabled scrapers. Intake channels are deliberately not in it: a forwarded
    listing is extracted once and never re-seen, so its age says nothing.
    """
    now = now.astimezone(KARACHI)
    problems: list[Problem] = []

    # 1. The digest for the week today falls in exists and is fresh.
    window = DigestWindow.week_of(week_of) if week_of else DigestWindow.current_week(now=now)
    monday = window.start.date()
    row = store.get_digest(monday)
    if row is None:
        problems.append(
            Problem(FAIL, f"no digest row for the week of {monday} — nothing has rendered it")
        )
    else:
        age = now - datetime.fromisoformat(row["created_at"]).astimezone(KARACHI)
        if age > max_digest_age:
            problems.append(
                Problem(
                    FAIL,
                    f"digest for the week of {monday} was last rendered {_hours(age)} ago "
                    f"(limit {_hours(max_digest_age)}) — the cron has not landed",
                )
            )

    # 2. Every enabled source has been seen recently.
    seen = store.last_seen_by_source()
    for slug in sources:
        found = seen.get(slug)
        if found is None:
            problems.append(Problem(FAIL, f"source {slug} has never contributed an event"))
            continue
        age = now - found.last_seen.astimezone(KARACHI)
        if age > max_source_age:
            problems.append(
                Problem(
                    FAIL,
                    f"source {slug} last contributed {_hours(age)} ago "
                    f"(limit {_hours(max_source_age)}) — its scrape is returning nothing: "
                    "rate limit, markup change, or the site is down. The digest still "
                    "shows its old events, which is why nothing else noticed",
                )
            )

    # 3. The bot's default reply would not be empty.
    today = now.date()
    upcoming = store.upcoming_event_count(today, UPCOMING_DAYS)
    if upcoming == 0:
        problems.append(
            Problem(
                FAIL,
                f"0 events in digest_events for the {UPCOMING_DAYS} days from {today} — "
                "the bot would answer 'nothing listed'",
            )
        )

    # 4. Forwarded listings are being processed.
    oldest = store.oldest_pending_intake()
    if oldest is not None:
        age = now - oldest.astimezone(KARACHI)
        if age > max_intake_age:
            pending = store.intake_counts()["pending"]
            problems.append(
                Problem(
                    FAIL,
                    f"{pending} listing(s) queued, the oldest for {_hours(age)} — intake is "
                    "not being drained (ANTHROPIC_API_KEY unset on the runner, or the "
                    "extractor is failing)",
                )
            )

    return problems


def summary(store: Store, *, now: datetime, sources: list[str]) -> list[str]:
    """The numbers behind `check`, as lines for a run summary.

    Printed whether or not anything failed: a green run that reports "0 events
    upcoming" is what used to go unnoticed, and the numbers are what a person
    scans when the digest merely looks thin.
    """
    now = now.astimezone(KARACHI)
    lines = [f"- now: {now:%Y-%m-%d %H:%M} PKT"]

    digests = store.list_digests()
    if digests:
        parts = []
        for d in digests[-4:]:
            age = now - datetime.fromisoformat(d["created_at"]).astimezone(KARACHI)
            parts.append(f"{d['week_of']} (rendered {_hours(age)} ago, {d['chars']} chars)")
        lines.append(f"- digests: {', '.join(parts)}")
    else:
        lines.append("- digests: none")

    seen = store.last_seen_by_source()
    parts = []
    for slug in sources:
        found = seen.get(slug)
        if found is None:
            parts.append(f"{slug} NEVER SEEN")
        else:
            age = now - found.last_seen.astimezone(KARACHI)
            parts.append(f"{slug} seen {_hours(age)} ago ({found.events} events)")
    for slug, found in sorted(seen.items()):
        if slug not in sources:
            parts.append(f"{slug} {found.events} events (intake, not checked)")
    lines.append(f"- sources: {', '.join(parts) or 'none'}")

    today = now.date()
    lines.append(
        f"- upcoming: {store.upcoming_event_count(today, UPCOMING_DAYS)} events in "
        f"digest_events for the {UPCOMING_DAYS} days from {today}"
    )
    intake = store.intake_counts()
    lines.append(
        f"- intake: {intake['pending']} pending, {intake['extracted']} extracted, "
        f"{intake['declined']} declined"
    )
    subs = store.subscriber_counts()
    lines.append(f"- subscribers: {subs['contacts']} contacted, {subs['opted_in']} opted in")
    return lines
