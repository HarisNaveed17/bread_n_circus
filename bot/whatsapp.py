"""WhatsApp Cloud API: verify inbound calls, send outbound text.

Phase 1 sends only free-form text, which the Cloud API allows exclusively
inside the 24-hour service window a user opens by messaging first. Every reply
here is a reply, so no template and no per-message cost is involved. Templates
arrive in Phase 2 with the weekly nudge.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os

import httpx

log = logging.getLogger(__name__)

GRAPH_VERSION = "v21.0"
TIMEOUT = 10.0


def verify_signature(raw_body: bytes, header: str | None) -> bool:
    """Check Meta's `X-Hub-Signature-256` against the app secret.

    The webhook URL is public, so without this anyone who finds it can make the
    bot send messages. Missing app secret is treated as *not verified* rather
    than as permission — a misconfigured deploy should reject, not open up.
    """
    secret = os.environ.get("WHATSAPP_APP_SECRET")
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def verify_challenge(params: dict[str, str]) -> str | None:
    """Meta's subscription handshake: echo `hub.challenge` iff the token matches."""
    token = os.environ.get("WHATSAPP_VERIFY_TOKEN")
    if params.get("hub.mode") == "subscribe" and token and params.get("hub.verify_token") == token:
        return params.get("hub.challenge")
    return None


def incoming_messages(payload: dict) -> list[dict]:
    """Pull user-sent messages out of a webhook payload.

    Delivery receipts and read receipts arrive on the same webhook under
    `statuses`. Those must be ignored or the bot answers its own receipts in a
    loop; only `messages` entries are real inbound traffic.
    """
    found = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for message in change.get("value", {}).get("messages", []):
                if message.get("from"):
                    found.append(message)
    return found


def send_text(to: str, body: str) -> None:
    phone_number_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
    token = os.environ["WHATSAPP_TOKEN"]
    resp = httpx.post(
        f"https://graph.facebook.com/{GRAPH_VERSION}/{phone_number_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body, "preview_url": False},
        },
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        # The Graph API hides the reason behind a bare status; the body has it.
        raise RuntimeError(f"WhatsApp send failed {resp.status_code}: {resp.text}")
