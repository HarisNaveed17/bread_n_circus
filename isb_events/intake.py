"""Drain the intake queue: forwarded text in, `Event`s out.

The other half of the split described in `bot/store.py` — the bot writes rows,
this reads them. Extraction needs an LLM client and the Vercel function is kept
to `httpx` alone, so nothing here can run in the bot.

Every row is marked processed whether or not it became an event. A declined
listing that stayed pending would be retried on every run forever, re-paying for
the same refusal and keeping the pending count permanently above the dispatch
threshold — which would fire a pipeline run every time a curator sent anything.
"""

from __future__ import annotations

import logging
from datetime import datetime

from .extract import extract
from .models import KARACHI, Event
from .sources import whatsapp
from .store import Store

log = logging.getLogger(__name__)

# One run will not process more than this. A curator pasting a backlog should
# not turn into an unbounded bill or a ten-minute job on a runner.
MAX_PER_RUN = 25


def _listing_for(row: dict):
    """Build a `Listing` from a queued row, by channel."""
    received = datetime.fromisoformat(row["received_at"]).astimezone(KARACHI).date()
    if row["channel"] == "whatsapp":
        return whatsapp.listing_from_message(row["body"], received_at=received)
    log.warning("intake: no adapter for channel %r", row["channel"])
    return None


def drain(store: Store, *, client=None, limit: int = MAX_PER_RUN) -> list[Event]:
    """Extract every pending listing, persist what comes out, mark all seen.

    Returns the events created, which the caller does not strictly need — they
    are already in the store — but which makes the run summary honest about
    what intake contributed.
    """
    rows = store.pending_intake(limit=limit)
    if not rows:
        return []

    log.info("intake: %d pending listing(s)", len(rows))
    created: list[Event] = []
    for row in rows:
        listing = _listing_for(row)
        if listing is None:
            store.mark_intake_processed(row["id"], decline_reason="no_adapter")
            continue
        event = extract(listing, client=client)
        if event is None:
            # `extract` re-raises a 4xx, so reaching here means the model looked
            # at it and declined, or the call failed transiently. Either way the
            # row is done; a retry would just pay for the same answer.
            store.mark_intake_processed(row["id"], decline_reason="declined")
            continue
        store.upsert_event(event)
        store.mark_intake_processed(row["id"], event_id=event.id)
        created.append(event)

    log.info("intake: %d of %d became events", len(created), len(rows))
    return created
