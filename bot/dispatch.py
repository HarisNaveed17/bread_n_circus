"""Ask GitHub Actions to run the digest now, rather than at the next schedule.

**Why this exists at all.** GitHub's `schedule:` trigger runs hours late here —
measured at +2h32 to +5h05 across eight consecutive firings (CLAUDE.md § Open
threads). So a listing a curator forwards at lunchtime would otherwise wait
until the evening run, which lands near midnight. A `workflow_dispatch` is the
only trigger that is actually prompt, and it has to come from something that is
always on. That is the bot.

**Why every listing fires a run, and why that is cheap.** Batching to five was
wrong: by the time a fifth listing arrives the first one's event can be the
same evening. What made batching look necessary was that a run scraped every
source, and theblackhole.pk answers a rate limit with an empty HTTP 200 — the
failure that silently emptied six digests. So the run the bot asks for now
carries `skip_fetch`, and `isb-events render --no-fetch` drains the queue and
re-renders from the store without touching a source at all. Scraping stays on
the schedule, where it belongs; a forward costs one Actions run and one
extraction.

**Token scope.** A fine-grained PAT, this repository only, `actions: write` and
nothing else. It cannot read code, cannot push, cannot touch secrets. It lives
in the Vercel env, never the repo. Absent or unset, everything here no-ops and
the listing simply waits for the schedule — intake still works.
"""

from __future__ import annotations

import logging
import os
import time

import httpx

log = logging.getLogger(__name__)

WORKFLOW = "weekly-digest.yml"
API = "https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches"
TIMEOUT = 10.0

# How many queued listings before a run is worth firing. One: a listing is no
# use to a reader while it sits in a queue, and the run it triggers does not
# scrape, so waiting buys nothing.
INTAKE_TRIGGER_THRESHOLD = 1

# Never twice inside this window. A webhook can be delivered more than once —
# Meta retries anything that is not a 2xx — and two curators can cross. It is
# five minutes rather than fifteen because a no-fetch run is cheap and a
# listing should not wait a quarter of an hour behind someone else's; it exists
# to collapse a burst, not to batch. A listing caught by the cooldown is not
# lost — the run already in flight drains the whole queue, and the next
# scheduled run would anyway.
COOLDOWN_SECONDS = 5 * 60

# Process-local, which is the honest scope: Vercel functions are short-lived, so
# this stops a burst inside one invocation and not much more. The workflow's own
# `concurrency: weekly-digest` group is what actually prevents overlapping runs;
# this just avoids asking for them.
_last_fired: float = 0.0


def configured() -> bool:
    return bool(os.environ.get("GITHUB_DISPATCH_TOKEN") and os.environ.get("GITHUB_REPO"))


def should_fire(pending: int, *, now: float | None = None) -> bool:
    """Enough queued, not too recent, and configured at all."""
    if not configured() or pending < INTAKE_TRIGGER_THRESHOLD:
        return False
    now = time.monotonic() if now is None else now
    return now - _last_fired >= COOLDOWN_SECONDS


def fire(*, now: float | None = None) -> bool:
    """Dispatch the digest workflow. True if GitHub accepted it.

    Never raises: failing to trigger early is a delay, not a failure — the
    scheduled run still picks the listings up. The curator has already been told
    their listing was saved, and that remains true.
    """
    global _last_fired
    repo = os.environ["GITHUB_REPO"]
    try:
        response = httpx.post(
            API.format(repo=repo, workflow=WORKFLOW),
            headers={
                "Authorization": f"Bearer {os.environ['GITHUB_DISPATCH_TOKEN']}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            # `skip_fetch` goes as a string: the REST API documents input
            # values as strings and coerces them to the input's declared type.
            json={
                "ref": os.environ.get("GITHUB_DISPATCH_REF", "master"),
                "inputs": {"skip_fetch": "true"},
            },
            timeout=TIMEOUT,
        )
    except Exception:
        log.exception("dispatch: could not reach GitHub; the schedule will pick it up")
        return False

    if response.status_code == 204:
        _last_fired = time.monotonic() if now is None else now
        log.info("dispatch: digest run requested")
        return True

    # 404 here usually means the token cannot see the repo, not that the
    # workflow is missing — GitHub hides both behind the same status.
    log.warning("dispatch: GitHub returned %s", response.status_code)
    return False
