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


def _post(payload: dict) -> dict:
    """POST to the messages endpoint and return the parsed response.

    A 200 here means *accepted for delivery*, not delivered. Free-form text to
    someone with no open service window is accepted and then silently dropped;
    the only signal is a later `failed` status on the webhook. So the response
    body matters — it carries the resolved `wa_id` and the message id — and is
    returned rather than discarded.
    """
    phone_number_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
    token = os.environ["WHATSAPP_TOKEN"]
    resp = httpx.post(
        f"https://graph.facebook.com/{GRAPH_VERSION}/{phone_number_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={"messaging_product": "whatsapp", **payload},
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        # The Graph API hides the reason behind a bare status; the body has it.
        raise RuntimeError(f"WhatsApp send failed {resp.status_code}: {resp.text}")
    return resp.json()


def graph_get(path: str, params: dict | None = None) -> dict:
    """GET any Graph API node with the configured token. Diagnostics only.

    Returns the parsed body on success and, on failure, the error body rather
    than raising — a 400 from Graph explains itself and is the useful part.
    """
    token = os.environ["WHATSAPP_TOKEN"]
    resp = httpx.get(
        f"https://graph.facebook.com/{GRAPH_VERSION}/{path.lstrip('/')}",
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
        timeout=TIMEOUT,
    )
    try:
        return resp.json()
    except ValueError:
        return {"error": {"message": f"HTTP {resp.status_code}: {resp.text[:200]}"}}


def send_text(to: str, body: str) -> dict:
    """Free-form text. Deliverable only inside an open 24-hour service window."""
    return _post({"to": to, "type": "text", "text": {"body": body, "preview_url": False}})


def send_template(to: str, name: str, language: str = "en_US") -> dict:
    """A template message — the only thing deliverable to a cold contact.

    Every test number has `hello_world` pre-approved, which makes it the way to
    prove the token and allowlist work without needing the recipient to message
    first. The real nudge template is Phase 2.
    """
    return _post(
        {
            "to": to,
            "type": "template",
            "template": {"name": name, "language": {"code": language}},
        }
    )
