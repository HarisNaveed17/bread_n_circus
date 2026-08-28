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
            _reply(message["from"])
        except Exception:
            log.exception("bot: failed to reply to %s", message.get("id"))

    return 200, "ok"


def _reply(to: str) -> None:
    """Phase 1: any message gets this week's digest. No parsing, no LLM."""
    parts = store.digest_messages()
    if not parts:
        whatsapp.send_text(to, NO_DIGEST_REPLY)
        return
    for part in parts:
        whatsapp.send_text(to, part)
