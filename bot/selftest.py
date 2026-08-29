"""One-off live check against the WhatsApp Cloud API. Not part of the suite.

    uv run python -m bot.selftest                  # check configuration only
    uv run python -m bot.selftest 923001234567     # free-form text
    uv run python -m bot.selftest 923001234567 -t  # hello_world template
    uv run python -m bot.selftest --diagnose       # is the WABA subscribed?

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


def diagnose() -> int:
    """Answer the question the dashboard is bad at: is anything subscribed?

    A webhook can verify, and Meta's Test button can reach it, while inbound
    messages still go nowhere — because the *WhatsApp Business Account* has to
    be subscribed to the app, separately from the app's webhook fields. This
    reads that state back rather than trusting a checkbox.
    """
    from . import whatsapp

    token = os.environ.get("WHATSAPP_TOKEN")
    if not token:
        print("WHATSAPP_TOKEN is unset.")
        return 1

    waba_ids = []
    explicit = os.environ.get("WHATSAPP_WABA_ID")
    if explicit:
        waba_ids.append(explicit)
    else:
        # The token itself knows which accounts it was granted against.
        debug = whatsapp.graph_get("debug_token", {"input_token": token})
        data = debug.get("data") or {}
        if "error" in debug:
            print(f"debug_token failed: {debug['error'].get('message')}")
        for scope in data.get("granular_scopes") or []:
            if "whatsapp_business" in (scope.get("scope") or ""):
                waba_ids.extend(scope.get("target_ids") or [])
        waba_ids = list(dict.fromkeys(waba_ids))

    if not waba_ids:
        print("No WhatsApp Business Account id found from the token.")
        print("Set WHATSAPP_WABA_ID from the app's API Setup page and re-run.")
        return 1

    ok = True
    for waba_id in waba_ids:
        print(f"\nWhatsApp Business Account {waba_id}")
        subs = whatsapp.graph_get(f"{waba_id}/subscribed_apps")
        if "error" in subs:
            print(f"  subscribed_apps: ERROR — {subs['error'].get('message')}")
            ok = False
            continue
        apps = subs.get("data") or []
        if not apps:
            print("  subscribed_apps: NONE — this is why inbound messages go nowhere.")
            print("  Fix: WhatsApp -> Configuration -> Webhook, subscribe the app,")
            print("  then tick the `messages` field.")
            ok = False
        for entry in apps:
            app_info = entry.get("whatsapp_business_api_data") or {}
            print(f"  subscribed: {app_info.get('name') or app_info.get('id') or entry}")
    return 0 if ok else 1


def main() -> int:
    if "--diagnose" in sys.argv:
        return diagnose()

    missing = [name for name in REQUIRED if not os.environ.get(name)]
    for name in REQUIRED:
        print(f"  {'set    ' if os.environ.get(name) else 'MISSING'}  {name}")
    if missing:
        print(f"\n{len(missing)} variable(s) missing; export them and re-run.")
        return 1

    try:
        parts = store.digest_messages()
        print(f"\nTurso: reachable, digest has {len(parts)} message(s)")
    except Exception as exc:
        print(f"\nTurso: FAILED — {type(exc).__name__}: {exc}")
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
