"""The `Notifier` protocol. Delivery sits behind this so a second channel
(WhatsApp, email) can be added later without touching the pipeline.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Notifier(Protocol):
    def send(self, messages: list[str]) -> None: ...


class DryRunNotifier:
    """Prints messages to stdout instead of delivering. Default in tests."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, messages: list[str]) -> None:
        for msg in messages:
            self.sent.append(msg)
            print("---8<--- message ---8<---")
            print(msg)
