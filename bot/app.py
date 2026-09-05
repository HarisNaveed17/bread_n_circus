"""Webhook logic, independent of any HTTP framework.

Kept free of request/response objects so it can be tested directly and so the
serverless adapter in `api/webhook.py` stays a thin shim.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta

from . import intent, store, whatsapp
from .store import KARACHI

log = logging.getLogger(__name__)

NO_DIGEST_REPLY = (
    "No digest has been published yet — the week's listings go out on Saturday. "
    "Message again then and I'll send you what's on."
)
OPT_IN_REPLY = (
    "You're subscribed — I'll message you once a week when the new listings are up. "
    "Reply STOP any time to stop."
)
OPT_OUT_REPLY = (
    "Done — I won't message you first again. "
    "You can still message me any time to get the week's events."
)
# Appended to the last message of a full-week reply. Nobody discovers a filter
# they were never told about, and the alternative — putting it in the stored
# digest — would bake a bot affordance into text the pipeline also renders for
# other purposes.
WEEK_HINT = '_Ask for "today" or "tomorrow" for just that day._'
NOTHING_ON = "Nothing listed for {when}. Reply *this week* for everything that's on."
DAY_UNAVAILABLE_NOTE = "Here's the whole week instead."

# Meta's cap on a text message body.
WHATSAPP_LIMIT = 4096

# How far ahead the default reply looks. Seven days rather than "this week"
# because a calendar week is the wrong unit for the question people ask: on a
# Sunday, the week containing today is over, and the week that has not started
# is the one they mean. A rolling window sidesteps the boundary entirely — it
# never shows a day that has passed and never hides one that is coming.
# The pipeline renders two weeks every run, so seven days is always covered.
UPCOMING_DAYS = 7
MAX_UPCOMING = 20
NOTHING_UPCOMING = (
    "Nothing listed for the next {days} days yet — new listings go up through the week."
)

# Honouring a typed STOP is a Meta requirement, not a feature. Kept to exact
# words rather than substring matching: "stop commenting on my body" is an
# actual event title in this week's digest, and someone asking about it must
# not be silently unsubscribed.
OPT_OUT_WORDS = {"stop", "unsubscribe", "stop promotions", "cancel"}
OPT_IN_WORDS = {"subscribe", "start", "join"}


def handle_verify(params: dict[str, str]) -> tuple[int, str]:
    """GET: Meta's subscription handshake."""
    challenge = whatsapp.verify_challenge(params)
    if challenge is None:
        return 403, "verification failed"
    return 200, challenge


def handle_health(token: str | None) -> tuple[int, str]:
    """A GET diagnostic: can *this deployment* read the digest out of Turso?

    That is the one path a webhook cannot tell you about from outside. When it
    is broken the bot still answers, with the "no digest yet" placeholder, so
    a misconfigured database and an empty one look identical from a phone.

    Gated on the verify token so it is not a public endpoint, and it reports
    only whether variables are set, never their values.
    """
    expected = os.environ.get("WHATSAPP_VERIFY_TOKEN")
    if not expected or token != expected:
        return 403, "forbidden"

    url = os.environ.get("TURSO_DATABASE_URL") or ""
    lines = [
        f"turso url : {url.split('://')[0] + '://' if '://' in url else 'UNSET'}",
        f"turso token: {'set' if os.environ.get('TURSO_AUTH_TOKEN') else 'MISSING'}",
        f"wa token  : {'set' if os.environ.get('WHATSAPP_TOKEN') else 'MISSING'}",
        f"wa number : {'set' if os.environ.get('WHATSAPP_PHONE_NUMBER_ID') else 'MISSING'}",
        f"app secret: {'set' if os.environ.get('WHATSAPP_APP_SECRET') else 'MISSING'}",
    ]
    try:
        found = store.current_digest()
    except Exception as exc:
        lines.append(f"digest    : UNREACHABLE — {type(exc).__name__}: {exc}")
        return 200, "\n".join(lines)

    if found is None:
        lines.append("digest    : store reachable, but NO ROWS in digests")
    else:
        text, week_of = found
        lines.append(f"digest    : ok — week of {week_of}, {len(text)} chars")

    # The bot writes subscribers but never creates them: migrations run in the
    # pipeline's Store.open(), so a migration that has not been applied yet
    # shows up here as a missing table rather than as a mid-message traceback.
    try:
        store.query("SELECT COUNT(*) FROM subscribers")
        lines.append("subscribers: table present")
    except Exception as exc:
        lines.append(f"subscribers: MISSING — {exc}")
        lines.append("            run the pipeline against this database once")
        lines.append("            (`gh workflow run weekly-digest.yml`) to migrate")

    # Same story for the day views: without this table "what's on today" quietly
    # falls back to the whole week, which looks like a parsing bug from a phone.
    try:
        rows = store.query("SELECT COUNT(*) FROM digest_events")
        lines.append(f"day views : table present — {rows[0][0]} event row(s)")
    except Exception as exc:
        lines.append(f"day views : MISSING — {exc}")
        lines.append("            same fix: run the pipeline once to migrate")

    return 200, "\n".join(lines)


def handle_event(raw_body: bytes, signature: str | None) -> tuple[int, str]:
    """POST: an inbound webhook event.

    Always answers 200 once the signature checks out, whatever happens next.
    Meta retries non-2xx deliveries and disables webhooks that keep failing, so
    an error while replying must not be reported as a delivery failure — it is
    logged and swallowed instead.
    """
    if not whatsapp.verify_signature(raw_body, signature):
        return 403, "bad signature"

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        log.warning("bot: webhook body was not JSON")
        return 200, "ignored"

    messages = whatsapp.incoming_messages(payload)
    if not messages:
        # Status callbacks land here constantly; this is the normal path.
        return 200, "no messages"

    for message in messages:
        try:
            _handle_message(message)
        except Exception:
            log.exception("bot: failed to handle %s", message.get("id"))

    return 200, "ok"


def _body_text(message: dict) -> str:
    """The message text, or "" for stickers, images and everything else."""
    return (message.get("text") or {}).get("body") or ""


def _handle_message(message: dict) -> None:
    sender = message["from"]
    log.info("bot: inbound %s message from %s", message.get("type", "?"), sender)

    # Best-effort: a bookkeeping failure must never cost someone their reply.
    try:
        store.record_contact(sender)
    except Exception:
        log.exception("bot: could not record contact for %s", sender)

    word = _body_text(message).strip().lower()

    if word in OPT_OUT_WORDS:
        store.opt_out(sender)
        whatsapp.send_text(sender, OPT_OUT_REPLY)
        return

    if word in OPT_IN_WORDS:
        store.opt_in(sender)
        whatsapp.send_text(sender, OPT_IN_REPLY)
        _send_upcoming(sender)
        return

    _reply(sender, intent.parse(_body_text(message), today=_today()))


def _today() -> date:
    """The reader's today, not the runner's — Vercel functions run in UTC."""
    return datetime.now(KARACHI).date()


def _reply(to: str, wanted: intent.Filter) -> None:
    if wanted.kind == intent.WEEK:
        _send_upcoming(to)
        return
    _send_day(to, wanted)


def _send_upcoming(to: str) -> None:
    """The default reply: everything from today to `UPCOMING_DAYS` out.

    Built from `digest_events`, not from the stored weekly text, so it crosses
    the week boundary and never shows a day that has already happened.
    """
    today = _today()
    try:
        rows = store.events_between(today, today + timedelta(days=UPCOMING_DAYS - 1))
    except Exception:
        # `digest_events` not migrated yet (CLAUDE.md § Working notes). The
        # stored weekly text is worse but it is not nothing.
        log.exception("bot: upcoming view unavailable for %s; falling back", to)
        _send_digest(to)
        return

    if not rows:
        log.info("bot: nothing upcoming for %s", to)
        whatsapp.send_text(to, NOTHING_UPCOMING.format(days=UPCOMING_DAYS))
        return

    cut = max(0, len(rows) - MAX_UPCOMING)
    rows = rows[:MAX_UPCOMING]
    days: list[tuple[str, list[str]]] = []
    for _, label, block in rows:
        if days and days[-1][0] == label:
            days[-1][1].append(block)
        else:
            days.append((label, [block]))

    header = f"*Islamabad — {days[0][0]} onwards*"
    note = f"…and {cut} more not shown." if cut else ""
    log.info("bot: sending %d upcoming event(s) to %s", len(rows), to)
    for part in _with_hint(_pack_days(header, days, note)):
        whatsapp.send_text(to, part)


def _pack_days(header: str, days: list[tuple[str, list[str]]], note: str = "") -> list[str]:
    """Header, then a heading and blocks per day, split at day boundaries."""
    sections = ["\n\n".join([f"*{label}*", *blocks]) for label, blocks in days]
    messages: list[str] = []
    current = header
    for section in sections:
        candidate = f"{current}\n\n{section}"
        if len(candidate) > WHATSAPP_LIMIT and current != header:
            messages.append(current)
            current = section
        else:
            current = candidate
    if note:
        addition = f"\n\n{note}"
        if len(current) + len(addition) > WHATSAPP_LIMIT:
            messages.append(current)
            current = note
        else:
            current += addition
    messages.append(current)
    return messages


def _send_day(to: str, wanted: intent.Filter) -> None:
    """One day's events, assembled from the blocks the pipeline already rendered."""
    try:
        rows = store.day_events(wanted.day)
    except Exception:
        # Most likely `digest_events` does not exist yet: migrations only run
        # when the pipeline connects, and this deployment can be newer than the
        # last cron run (CLAUDE.md § Working notes). Falling back to the week
        # keeps the asker served; `?health=` is where the cause shows up.
        log.exception("bot: day view unavailable for %s; falling back to the week", to)
        whatsapp.send_text(to, DAY_UNAVAILABLE_NOTE)
        _send_digest(to)
        return

    if not rows:
        log.info("bot: nothing on %s for %s", wanted.day, to)
        whatsapp.send_text(to, NOTHING_ON.format(when=wanted.label))
        return

    label = rows[0][0]
    parts = _pack_day(label, [block for _, block in rows])
    log.info("bot: sending %d event(s) for %s to %s", len(rows), wanted.day, to)
    for part in parts:
        whatsapp.send_text(to, part)


def _pack_day(label: str, blocks: list[str]) -> list[str]:
    """Header plus blocks, split at block boundaries under the char limit.

    A single day overflowing 4096 would take a dozen events and is not expected;
    this is here so that if it ever happens the reply truncates nowhere.
    """
    header = f"*Islamabad — {label}*"
    messages: list[str] = []
    current = header
    for block in blocks:
        candidate = f"{current}\n\n{block}"
        if len(candidate) > WHATSAPP_LIMIT and current != header:
            messages.append(current)
            current = block
        else:
            current = candidate
    messages.append(current)
    return messages


def _send_digest(to: str) -> None:
    """Phase 1: anything that isn't STOP/SUBSCRIBE gets the digest. No LLM.

    Logs the outcome but never the message body — these are strangers' texts,
    and the sender id is enough to trace a delivery through the logs.
    """
    parts = store.digest_messages()
    if not parts:
        # Reads the store successfully and finds nothing: either the cron has
        # not run, or this deployment is pointed at the wrong database.
        log.warning("bot: no digest stored; replying with the placeholder to %s", to)
        whatsapp.send_text(to, NO_DIGEST_REPLY)
        return
    log.info("bot: sending digest (%d message(s)) to %s", len(parts), to)
    for part in _with_hint(parts):
        whatsapp.send_text(to, part)


def _with_hint(parts: list[str]) -> list[str]:
    """Append the day-filter hint to the last message, if it fits.

    Only the default reply carries it: someone who already asked for "today"
    does not need telling that "today" works.
    """
    hint = f"\n\n{WEEK_HINT}"
    if parts and len(parts[-1]) + len(hint) <= WHATSAPP_LIMIT:
        parts = [*parts[:-1], parts[-1] + hint]
    return parts
