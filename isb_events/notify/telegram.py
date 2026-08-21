"""Telegram Bot API delivery. Plain httpx, no bot framework — we send only.

Env: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
"""

from __future__ import annotations

import os

import httpx

API_BASE = "https://api.telegram.org"
TIMEOUT = 20.0


class TelegramNotifier:
    """Sends each message as a MarkdownV2 Telegram message."""

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
                        "parse_mode": "MarkdownV2",
                        "disable_web_page_preview": True,
                    },
                )
                # Surface the Bot API's error body — a bare 400 hides the reason
                # (almost always a MarkdownV2 escaping bug).
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"Telegram sendMessage failed {resp.status_code}: {resp.text}"
                    )
