"""Webhook logic, independent of any HTTP framework.

Kept free of request/response objects so it can be tested directly and so the
serverless adapter in `api/webhook.py` stays a thin shim.
"""

from __future__ import annotations

import json
import logging

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
    """Phase 1: anything that isn't STOP/SUBSCRIBE gets the digest. No LLM."""
    parts = store.digest_messages()
    if not parts:
        whatsapp.send_text(to, NO_DIGEST_REPLY)
        return
    for part in parts:
        whatsapp.send_text(to, part)
