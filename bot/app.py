"""Webhook logic, independent of any HTTP framework.

Kept free of request/response objects so it can be tested directly and so the
serverless adapter in `api/webhook.py` stays a thin shim.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timedelta

from . import dispatch, intent, store, whatsapp
from .store import KARACHI

log = logging.getLogger(__name__)

# The first message from a number gets this and nothing else. It is the one
# moment the bot can explain itself; the buttons under it are the "try it".
GREETING = (
    "Hello ji! \U0001f440\n\n"
    "Welcome to *Kya Scene Hai?* Your rundown of everything that's happening in "
    "Islamabad. Don't want your entire social life to revolve around eating? Just "
    "text KSH here and it'll give you a summary of everything happening today, "
    "tomorrow and all of next week. Ask for a kind of thing too — try "
    "\"music this week\" or \"comedy tonight\", or tap *Browse* for the full list."
)
# The bot never writes first, so "no digest" means the listings are between
# refreshes, not that the week has not been published.
NO_DIGEST_REPLY = (
    "Nothing to show just now — the listings refresh twice a day. Try again in a few hours."
)
# There is no weekly nudge (CLAUDE.md § Delivery, Phase 2). Consent is still
# recorded so the list exists if one ever ships, but the reply must not
# promise a message that will not come.
OPT_IN_REPLY = (
    "Noted — you're on the list for a weekly heads-up if I ever start sending one. "
    "For now I only reply when you text me, so just message any time. "
    "Reply STOP if you'd rather not be on the list."
)
OPT_OUT_REPLY = (
    "Done — I won't message you first again. "
    "You can still message me any time to get the week's events."
)
# The body of the button message that follows every listing reply. Short,
# because the buttons are the content; the text is there because Meta requires
# a body on an interactive message.
PICK_BODY = "Pick a view \U0001f447"
NOTHING_ON = "Nothing listed for {when}."
DAY_UNAVAILABLE_NOTE = "Here's the whole week instead."

# A bare Instagram post link from a curator is a submission, like a forward.
# Sharing from the Instagram app sends a plain text message with the URL — not
# a forward — so without this a curator's share would get "didn't catch that".
# The pipeline's `sources/instagram.py` has the same pattern; the bot cannot
# import it, so this is a copy and `tests/test_bot.py` pins the two together.
INSTAGRAM_POST_RE = re.compile(r"https?://(?:www\.)?instagram\.com/(?:p|reel)/[\w-]+/?(?:\?\S*)?")

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
# A category that matched nothing says so, rather than widening to everything:
# someone who asked for sports and got a comedy gig learns the filter does not
# work. The buttons still follow, so the reply is an offer, not a dead end.
NOTHING_UPCOMING_IN = "Nothing listed for {what} in the next few days. Try another view?"

# Honouring a typed STOP is a Meta requirement, not a feature. Kept to exact
# words rather than substring matching: "stop commenting on my body" is an
# actual event title in this week's digest, and someone asking about it must
# not be silently unsubscribed.
OPT_OUT_WORDS = {"stop", "unsubscribe", "stop promotions", "cancel"}
OPT_IN_WORDS = {"subscribe", "start", "join"}

# The one button that asks a question instead of answering one, so it is routed
# before `intent.parse` — there is no filter "browse" could mean.
BROWSE_WORDS = {"browse", "categories", "category", "menu", "options"}
BROWSE_BODY = "What are you in the mood for? 👀"

# Curators forward listings with this prefix. A keyword rather than "anything
# from a curator", because curators also just ask what's on.
INSERT_PREFIX = "/insert"

INSERT_SAVED = "Saved — I'll pull the details out of that and it'll show up at the next refresh."
INSERT_SOON = (
    "Saved — I'm pulling the details out now, so it'll be in the listings in a few minutes."
)
INSERT_EMPTY = "Send the listing text after /insert and I'll add it."
# A forwarded photo is the obvious next thing a curator will try. Flyer intake
# needs the bot to download media bytes at receipt, because Meta's media URLs
# expire long before the pipeline runs — so say so rather than failing quietly.
INSERT_NO_TEXT = (
    "I can only read text listings at the moment — a forwarded photo won't work yet. "
    "Paste the details and I'll take them."
)
NOT_UNDERSTOOD = (
    "Ooops, I don't know what you're trying to say there \U0001f440\n\n"
    "Are you interested in what's on? Pick a view below, or text *KSH*."
)
INSERT_FAILED = "Couldn't save that one — try again in a minute."
# Deliberately identical to what a stranger gets for any other message: the
# reply must not reveal that /insert means anything, or that an allowlist
# exists. An unknown sender simply gets the digest, as before.


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
        f"wa token  : {_token_line()}",
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

    curators = len(_curators())
    lines.append(f"curators  : {curators or 'NONE — /insert is closed to everyone'}")
    lines.append(
        f"dispatch  : {'configured' if dispatch.configured() else 'off (schedule only)'}"
        f" — {dispatch.cooldown_state()}"
    )
    try:
        rows = store.query("SELECT COUNT(*) FROM intake WHERE processed_at IS NULL")
        lines.append(f"intake    : table present — {rows[0][0]} pending")
    except Exception as exc:
        lines.append(f"intake    : MISSING — {exc}")

    # Same story for the day views: without this table "what's on today" quietly
    # falls back to the whole week, which looks like a parsing bug from a phone.
    try:
        rows = store.query("SELECT COUNT(*) FROM digest_events")
        lines.append(f"day views : table present — {rows[0][0]} event row(s)")
    except Exception as exc:
        lines.append(f"day views : MISSING — {exc}")
        lines.append("            same fix: run the pipeline once to migrate")

    # How much of what the bot can serve is actually classified. The category
    # picker is only worth offering once this is high: below it, "Music" comes
    # back thin while the gigs sit unclassified under Mixed Bag. Unclassified
    # events are not lost — Mixed Bag serves them — so this is a "is the picker
    # honest yet" gauge, not an outage.
    try:
        rows = store.query(
            "SELECT COUNT(*), COUNT(category) FROM digest_events WHERE event_date >= ?",
            [_today().isoformat()],
        )
        total, labelled = int(rows[0][0] or 0), int(rows[0][1] or 0)
        share = f"{labelled * 100 // total}%" if total else "n/a"
        lines.append(f"categories: {labelled} of {total} upcoming classified ({share})")
    except Exception as exc:
        lines.append(f"categories: unknown — {exc}")

    return 200, "\n".join(lines)


def _token_line() -> str:
    """`set` plus what Meta says about the token's lifetime.

    "set" alone was the one line in `?health=` that could be green while the
    bot was dead: a 24-hour token from the API Setup page is set right up until
    it is not. Asking Meta costs one Graph call, and it means an external
    monitor polling this endpoint sees EXPIRED (or TEMPORARY, with hours left)
    before a reader sees silence. A Graph outage must not fail the health
    check for that, so it degrades to "could not check".
    """
    if not os.environ.get("WHATSAPP_TOKEN"):
        return "MISSING"
    try:
        return f"set — {whatsapp.token_status()}"
    except Exception as exc:
        return f"set — could not check ({type(exc).__name__})"


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
    """The message text, or "" for stickers, images and everything else.

    A tapped reply button arrives as an `interactive` message carrying the
    button's title. The title is one of the words `intent.parse` already
    understands, so a tap is handled exactly as if it had been typed.
    """
    if message.get("type") == "interactive":
        interactive = message.get("interactive") or {}
        # A button tap and a list-row tap arrive in different keys and are the
        # same thing to us: both carry the title, and the title is a word the
        # parser knows.
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        return reply.get("title") or ""
    return (message.get("text") or {}).get("body") or ""


def _handle_message(message: dict) -> None:
    sender = message["from"]
    log.info("bot: inbound %s message from %s", message.get("type", "?"), sender)

    # Best-effort: a bookkeeping failure must never cost someone their reply.
    # It also says whether this is the number's first message, which decides
    # whether they get the greeting; a failed write means "not first", so a
    # Turso blip costs a greeting rather than producing a duplicate one.
    first = False
    try:
        first = bool(store.record_contact(sender))
    except Exception:
        log.exception("bot: could not record contact for %s", sender)

    body = _body_text(message)
    word = body.strip().lower()
    curator = _wa_id(sender) in _curators()

    # A forward carries `context.forwarded` and nothing else; a normal message
    # has no `context` at all. Confirmed against a real forward on 2026-09-17 —
    # see CLAUDE.md § Intake. WhatsApp gives you no way to add a prefix to a
    # forward, so for a curator the forward *is* the submission. A bare
    # Instagram link is one too: the share button sends plain text.
    if curator and (_is_forwarded(message) or _is_post_link(body)):
        _handle_insert(sender, body.strip(), forwarded=_is_forwarded(message))
        return

    if curator and word.startswith(INSERT_PREFIX):
        _handle_insert(sender, body.strip()[len(INSERT_PREFIX) :].strip())
        return

    if word in OPT_OUT_WORDS:
        store.opt_out(sender)
        whatsapp.send_text(sender, OPT_OUT_REPLY)
        return

    # A first message gets the greeting and nothing else — whatever it said.
    # The greeting explains what to text and carries the buttons, so the
    # sender's second message is the one that gets answered.
    if first:
        log.info("bot: first contact from %s; sending the greeting", sender)
        _send_with_buttons(sender, GREETING)
        return

    if word in OPT_IN_WORDS:
        store.opt_in(sender)
        whatsapp.send_text(sender, OPT_IN_REPLY)
        _send_upcoming(sender)
        return

    # "Browse" is the one button whose job is to ask a question rather than
    # answer one, so it is routed here rather than through `intent.parse` —
    # there is no filter it could mean.
    if word in BROWSE_WORDS:
        log.info("bot: sending the category list to %s", sender)
        whatsapp.send_list(sender, BROWSE_BODY)
        return

    _reply(sender, intent.parse(body, today=_today()))


def _is_post_link(body: str) -> bool:
    """Is the whole message one Instagram post URL?"""
    tokens = body.split()
    return len(tokens) == 1 and INSTAGRAM_POST_RE.fullmatch(tokens[0]) is not None


def _wa_id(number: str) -> str:
    """Digits only, which is the form Meta puts in a message's `from` field.

    Applied to the configured list as well as the incoming sender, so
    `+92 300 1234567`, `92 300 1234567` and `923001234567` all mean the same
    curator. Getting this wrong is otherwise silent — the number simply never
    matches and `/insert` quietly behaves as if you were a stranger.
    """
    return "".join(c for c in number if c.isdigit())


def _curators() -> set[str]:
    """wa_ids allowed to submit listings, from `CURATORS`, comma-separated.

    **Not optional.** Without it the bot's number is an open pipe into the
    digest and, worse, into a model — the bodies are untrusted text. Unset
    means nobody is a curator, which fails closed.
    """
    raw = os.environ.get("CURATORS", "")
    return {_wa_id(c) for c in raw.split(",") if _wa_id(c)}


def _is_forwarded(message: dict) -> bool:
    return bool((message.get("context") or {}).get("forwarded"))


def _handle_insert(sender: str, listing: str, *, forwarded: bool = False) -> None:
    """A curator submitting a listing. Store it; the pipeline parses it later.

    A non-curator gets the ordinary reply and nothing is stored. That is
    deliberately indistinguishable from any other message — telling a stranger
    "you are not authorised" teaches them that `/insert` does something.
    """
    if _wa_id(sender) not in _curators():
        log.info("bot: submission from a non-curator %s; treating as a normal message", sender)
        _reply(sender, intent.parse(listing, today=_today()))
        return

    if not listing:
        whatsapp.send_text(sender, INSERT_NO_TEXT if forwarded else INSERT_EMPTY)
        return

    try:
        store.record_intake(sender, listing)
        pending = store.pending_intake()
    except Exception:
        log.exception("bot: could not queue a listing from %s", sender)
        whatsapp.send_text(sender, INSERT_FAILED)
        return

    log.info("bot: queued a listing from %s; %d pending", sender, pending)
    if dispatch.should_fire(pending) and dispatch.fire():
        whatsapp.send_text(sender, INSERT_SOON)
        return
    whatsapp.send_text(sender, INSERT_SAVED)


def _today() -> date:
    """The reader's today, not the runner's — Vercel functions run in UTC."""
    return datetime.now(KARACHI).date()


def _reply(to: str, wanted: intent.Filter) -> None:
    if wanted.kind == intent.UNKNOWN:
        log.info("bot: nothing recognised in a message from %s", to)
        _send_with_buttons(to, NOT_UNDERSTOOD)
        return
    if wanted.kind == intent.WEEK:
        _send_upcoming(to, wanted)
        return
    _send_day(to, wanted)


def _send_upcoming(to: str, wanted: intent.Filter = intent.WEEK_FILTER) -> None:
    """The default reply: everything from today to `UPCOMING_DAYS` out.

    Built from `digest_events`, not from the stored weekly text, so it crosses
    the week boundary and never shows a day that has already happened.

    Takes the whole `Filter` rather than just a category, because the empty
    reply has to echo the asker's own words back.
    """
    today = _today()
    try:
        rows = store.events_between(
            today, today + timedelta(days=UPCOMING_DAYS - 1), wanted.category
        )
    except Exception:
        # `digest_events` not migrated yet (CLAUDE.md § Working notes). The
        # stored weekly text is worse but it is not nothing.
        log.exception("bot: upcoming view unavailable for %s; falling back", to)
        _send_digest(to)
        return

    if not rows:
        log.info("bot: nothing upcoming for %s (category=%s)", to, wanted.category)
        # A category that matched nothing says so rather than widening to
        # everything: someone who asked for sports and got a comedy gig learns
        # the filter does not work.
        if wanted.category:
            _send_with_buttons(to, NOTHING_UPCOMING_IN.format(what=wanted.label))
        else:
            _send_with_buttons(to, NOTHING_UPCOMING.format(days=UPCOMING_DAYS))
        return

    cut = max(0, len(rows) - MAX_UPCOMING)
    rows = rows[:MAX_UPCOMING]
    days: list[tuple[str, list[str]]] = []
    for _, label, block in rows:
        if days and days[-1][0] == label:
            days[-1][1].append(block)
        else:
            days.append((label, [block]))

    # Name the category in the header when there is one, so a short list reads
    # as "this is the music" rather than as a thin week. The bare category name,
    # not `label` — that carries the timeframe too, which the header already has.
    what = f" — {intent.CATEGORY_LABELS[wanted.category]}" if wanted.category else ""
    header = f"*Islamabad{what} — {days[0][0]} onwards*"
    note = f"…and {cut} more not shown." if cut else ""
    log.info("bot: sending %d upcoming event(s) to %s", len(rows), to)
    _send_parts(to, _pack_days(header, days, note))


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
        rows = store.day_events(wanted.day, wanted.category)
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
        _send_with_buttons(to, NOTHING_ON.format(when=wanted.label))
        return

    label = rows[0][0]
    what = intent.CATEGORY_LABELS[wanted.category] if wanted.category else ""
    parts = _pack_day(label, [block for _, block in rows], what)
    log.info("bot: sending %d event(s) for %s to %s", len(rows), wanted.day, to)
    _send_parts(to, parts)


def _pack_day(label: str, blocks: list[str], what: str = "") -> list[str]:
    """Header plus blocks, split at block boundaries under the char limit.

    A single day overflowing 4096 would take a dozen events and is not expected;
    this is here so that if it ever happens the reply truncates nowhere.
    """
    header = f"*Islamabad — {what} — {label}*" if what else f"*Islamabad — {label}*"
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
        _send_with_buttons(to, NO_DIGEST_REPLY)
        return
    log.info("bot: sending digest (%d message(s)) to %s", len(parts), to)
    _send_parts(to, parts)


def _send_parts(to: str, parts: list[str]) -> None:
    """A listing reply: the text messages, then the buttons.

    The buttons ride on a separate, short message rather than on the last part
    because an interactive body is capped at 1024 characters and a day's
    listings are routinely longer. Every listing reply ends this way — after
    "today", the buttons offer tomorrow and the week, which is the next thing
    a reader wants without having to know what to type.
    """
    for part in parts:
        whatsapp.send_text(to, part)
    whatsapp.send_buttons(to, PICK_BODY)


def _send_with_buttons(to: str, text: str) -> None:
    """A short reply with the buttons under it, in one message when it fits."""
    if len(text) <= whatsapp.BUTTON_BODY_LIMIT:
        whatsapp.send_buttons(to, text)
        return
    whatsapp.send_text(to, text)
    whatsapp.send_buttons(to, PICK_BODY)
