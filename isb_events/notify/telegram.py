"""Telegram Bot API delivery. Plain httpx, no bot framework — we send only.

Kept only until the WhatsApp notifier lands; Telegram is being dropped (it is
banned in Pakistan — see CLAUDE.md § Delivery). It sends **unformatted** text:
`render.py` now emits WhatsApp flavour, which has none of the backslash
escaping MarkdownV2 demands, so asking for `parse_mode: MarkdownV2` here would
make the Bot API reject every message with a 400. Asterisks show up literally.

Env: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
"""

from __future__ import annotations

import os

import httpx

API_BASE = "https://api.telegram.org"
TIMEOUT = 20.0


class TelegramNotifier:
    """Sends each message as a plain-text Telegram message."""

    def __init__(self, token: str | None = None, chat_id: str | None = None) -> None:
        self.token = token or os.environ["TELEGRAM_BOT_TOKEN"]
        self.chat_id = chat_id or os.environ["TELEGRAM_CHAT_ID"]

    def send(self, messages: list[str]) -> None:
        url = f"{API_BASE}/bot{self.token}/sendMessage"
        with httpx.Client(timeout=TIMEOUT) as client:
            for msg in messages:
                resp = client.post(
                    url,
                    json={
                        "chat_id": self.chat_id,
                        "text": msg,
                        "disable_web_page_preview": True,
                    },
                )
                # Surface the Bot API's error body — a bare 400 hides the reason.
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"Telegram sendMessage failed {resp.status_code}: {resp.text}"
                    )
