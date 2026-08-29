"""One-off live check against the WhatsApp Cloud API. Not part of the suite.

    uv run python -m bot.selftest              # check configuration only
    uv run python -m bot.selftest 923001234567 # ...and send a real message

Everything in `bot/whatsapp.py` was written from Meta's docs and has never
been exercised against the live API, so this exists to find the mismatch
before a deploy does. It prints which variables are set — never their values.

The recipient must be on the test number's allowlist, or Meta rejects the send.
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


def main() -> int:
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
    try:
        whatsapp.send_text(recipient, "isb-events self-test — the webhook is wired up.")
    except Exception as exc:
        # The Graph API's reason is in the body, which send_text attaches.
        print(f"\nSend: FAILED — {exc}")
        return 1
    print(f"\nSend: ok, check WhatsApp on {recipient}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
