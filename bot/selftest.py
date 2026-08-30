"""One-off live check against the WhatsApp Cloud API. Not part of the suite.

    uv run python -m bot.selftest                  # check configuration only
    uv run python -m bot.selftest 923001234567     # free-form text
    uv run python -m bot.selftest 923001234567 -t  # hello_world template
    uv run python -m bot.selftest --diagnose       # is the WABA subscribed?
    uv run python -m bot.selftest --subscribe      # ...subscribe it if not

Everything in `bot/whatsapp.py` was written from Meta's docs and has never
been exercised against the live API, so this exists to find the mismatch
before a deploy does. It prints which variables are set — never their values.

The recipient must be on the test number's allowlist, or Meta rejects the send.

A 200 from the send endpoint means *accepted*, not *delivered*. Free-form text
is only deliverable inside the 24-hour service window a recipient opens by
messaging the number first; outside it the API accepts the request and the
message is silently dropped. Use `-t` to send the pre-approved `hello_world`
template instead — that is deliverable cold, so it isolates "token and
allowlist are wrong" from "there is no open window".
"""

from __future__ import annotations

import os
import sys

from . import store, whatsapp

REQUIRED = [
    "WHATSAPP_PHONE_NUMBER_ID",
    "WHATSAPP_TOKEN",
    "WHATSAPP_VERIFY_TOKEN",
    "WHATSAPP_APP_SECRET",
    "TURSO_DATABASE_URL",
    "TURSO_AUTH_TOKEN",
]


def _token_status() -> str:
    """When does WHATSAPP_TOKEN die?

    The API Setup page hands out short-lived tokens and does not say so on the
    way past, so the bot goes quiet hours later with no deploy having changed.
    A System User token reports `never`; anything with a date is temporary.
    """
    import datetime

    from . import whatsapp

    token = os.environ["WHATSAPP_TOKEN"]
    body = whatsapp.graph_get("debug_token", {"input_token": token, "access_token": token})
    if "error" in body:
        return f"Token: REJECTED — {body['error'].get('message')}"

    data = body.get("data") or {}
    expires = data.get("expires_at")
    if expires in (0, None):
        return "Token: valid, never expires (System User token)"
    when = datetime.datetime.fromtimestamp(expires, datetime.UTC)
    left = when - datetime.datetime.now(datetime.UTC)
    hours = left.total_seconds() / 3600
    if hours < 0:
        return f"Token: EXPIRED at {when:%Y-%m-%d %H:%M UTC}"
    return (
        f"Token: TEMPORARY — expires {when:%Y-%m-%d %H:%M UTC} "
        f"({hours:.1f}h left). Replace it with a System User token."
    )


def diagnose() -> int:
    """Is the WhatsApp Business Account subscribed to this app?

    A webhook can pass verification and answer Meta's Test button while inbound
    messages go nowhere, because the account is subscribed to the app
    separately from the app's webhook fields. Needs WHATSAPP_WABA_ID from the
    API Setup page.
    """
    from . import whatsapp

    waba_id = os.environ.get("WHATSAPP_WABA_ID")
    if not waba_id:
        print("Set WHATSAPP_WABA_ID (API Setup page) to check subscriptions.")
        return 1

    subs = whatsapp.graph_get(f"{waba_id}/subscribed_apps")
    if "error" in subs:
        print(f"subscribed_apps: ERROR — {subs['error'].get('message')}")
        return 1

    apps = subs.get("data") or []
    if not apps:
        print("subscribed_apps: NONE — inbound messages will go nowhere.")
        print("Fix: WhatsApp -> Configuration -> Webhook, subscribe the app,")
        print("then tick the `messages` field.")
        return 1

    for entry in apps:
        info = entry.get("whatsapp_business_api_data") or {}
        print(f"subscribed: {info.get('name') or info.get('id') or entry}")
    return 0


def subscribe() -> int:
    """Subscribe the app to the WhatsApp Business Account.

    The dashboard toggle for this is easy to miss and does not always take.
    This is the same call it makes, and it is idempotent.
    """
    from . import whatsapp

    waba_id = os.environ.get("WHATSAPP_WABA_ID")
    if not waba_id:
        print("Set WHATSAPP_WABA_ID (API Setup page) first.")
        return 1

    result = whatsapp.graph_post(f"{waba_id}/subscribed_apps")
    if "error" in result:
        print(f"subscribe: ERROR — {result['error'].get('message')}")
        return 1
    print(f"subscribe: {result}")
    print("Re-run --diagnose to confirm, then text the number again.")
    return 0


def main() -> int:
    if "--diagnose" in sys.argv:
        return diagnose()
    if "--subscribe" in sys.argv:
        return subscribe()

    missing = [name for name in REQUIRED if not os.environ.get(name)]
    for name in REQUIRED:
        print(f"  {'set    ' if os.environ.get(name) else 'MISSING'}  {name}")
    if missing:
        print(f"\n{len(missing)} variable(s) missing; export them and re-run.")
        return 1

    print(f"\n{_token_status()}")

    try:
        parts = store.digest_messages()
        print(f"Turso: reachable, digest has {len(parts)} message(s)")
    except Exception as exc:
        print(f"Turso: FAILED — {type(exc).__name__}: {exc}")
        return 1

    if len(sys.argv) < 2:
        print("\nPass a recipient to send a real message.")
        return 0

    recipient = sys.argv[1]
    as_template = "-t" in sys.argv or "--template" in sys.argv

    try:
        if as_template:
            result = whatsapp.send_template(recipient, "hello_world")
        else:
            result = whatsapp.send_text(
                recipient, "isb-events self-test — the webhook is wired up."
            )
    except Exception as exc:
        # The Graph API's reason is in the body, which _post attaches.
        print(f"\nSend: FAILED — {exc}")
        return 1

    contacts = result.get("contacts") or [{}]
    messages = result.get("messages") or [{}]
    resolved = contacts[0].get("wa_id")
    print(f"\nAccepted by Meta ({'template' if as_template else 'free-form text'})")
    print(f"  requested : {recipient}")
    print(f"  wa_id     : {resolved or '(none returned)'}")
    print(f"  message id: {messages[0].get('id') or '(none returned)'}")
    if resolved and resolved != recipient.lstrip("+"):
        print("  note      : Meta rewrote the number; it dials the wa_id above.")

    print("\nAccepted is not delivered.")
    if as_template:
        print("A template is deliverable cold, so if this one does not arrive the")
        print("problem is the token, the phone number id, or the allowlist.")
    else:
        print("Free-form text only reaches someone with an open 24-hour service")
        print("window. If nothing arrives, message the number from that phone")
        print("first and re-run — or re-run with -t to test with a template.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
