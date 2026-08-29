"""Webhook logic, independent of any HTTP framework.

Kept free of request/response objects so it can be tested directly and so the
serverless adapter in `api/webhook.py` stays a thin shim.
"""

from __future__ import annotations

import json
import logging
import os

from . import store, whatsapp

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
        found = store.latest_digest()
    except Exception as exc:
        lines.append(f"digest    : UNREACHABLE — {type(exc).__name__}: {exc}")
        return 200, "\n".join(lines)

    if found is None:
        lines.append("digest    : store reachable, but NO ROWS in digests")
    else:
        text, week_of = found
        lines.append(f"digest    : ok — week of {week_of}, {len(text)} chars")
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
        _send_digest(sender)
        return

    _send_digest(sender)


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
    for part in parts:
        whatsapp.send_text(to, part)
