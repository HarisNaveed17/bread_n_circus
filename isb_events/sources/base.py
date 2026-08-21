"""The `Source` protocol and the registry that loads it from `sources.yaml`.

A source is anything that can turn a `DigestWindow` into a list of `Event`s.
Sources declare themselves in `sources.yaml` with an `enabled` flag, so a
broken scraper can be switched off without a code change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

import yaml

from ..models import DigestWindow, Event

SOURCES_YAML = Path(__file__).resolve().parent.parent.parent / "sources.yaml"


@runtime_checkable
class Source(Protocol):
    slug: str

    def fetch(self, window: DigestWindow) -> list[Event]: ...


# Populated by scraper modules as they are built (M1, M2). Maps slug -> factory.
_REGISTRY: dict[str, type[Source]] = {}


def register(slug: str):
    """Class decorator: register a scraper under its slug."""

    def wrap(cls: type[Source]) -> type[Source]:
        _REGISTRY[slug] = cls
        return cls

    return wrap


def load_enabled_sources(path: Path = SOURCES_YAML) -> list[Source]:
    """Instantiate every enabled, registered source from `sources.yaml`.

    Entries that are disabled, or whose slug has no registered scraper yet, are
    skipped silently — that's how M0 runs green with zero scrapers built.
    """
    if not path.exists():
        return []
    config = yaml.safe_load(path.read_text()) or {}
    sources: list[Source] = []
    for entry in config.get("sources", []) or []:
        if not entry.get("enabled"):
            continue
        slug = entry["slug"]
        cls = _REGISTRY.get(slug)
        if cls is None:
            continue
        sources.append(cls())
    return sources
