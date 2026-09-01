"""Core data model: `Event` and `DigestWindow`.

All datetimes are timezone-aware in Asia/Karachi. A naive datetime is a bug;
`Event` rejects them at construction time.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator

KARACHI = ZoneInfo("Asia/Karachi")

DESCRIPTION_MAX_CHARS = 300


def _require_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return value
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


class DigestWindow(BaseModel):
    """The half-open week a digest covers, [start, end), in Karachi time."""

    start: datetime
    end: datetime

    model_config = {"frozen": True}

    @field_validator("start", "end")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _require_aware(v)

    @classmethod
    def week_of(cls, monday: date) -> DigestWindow:
        """Mon 00:00 through the following Mon 00:00 (exclusive) in Karachi."""
        start = datetime.combine(monday, datetime.min.time(), tzinfo=KARACHI)
        return cls(start=start, end=start + timedelta(days=7))

    @classmethod
    def coming_week(cls, *, now: datetime | None = None) -> DigestWindow:
        """The next Mon–Sun window relative to `now` (defaults to today, Karachi)."""
        now = now.astimezone(KARACHI) if now else datetime.now(KARACHI)
        today = now.date()
        # Monday of *next* week; if today is Monday, that's today.
        days_ahead = (7 - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7 if now.weekday() != 0 else 0
        monday = today + timedelta(days=days_ahead)
        return cls.week_of(monday)

    @classmethod
    def current_week(cls, *, now: datetime | None = None) -> DigestWindow:
        """The Mon-Sun window `now` falls inside — the week people are living in.

        Distinct from `coming_week`, which from Tuesday onwards points at the
        *next* week. Rendering only that leaves today and tomorrow frozen at
        whatever the last Monday's run found, which is exactly what the bot's
        day views read. A cron that runs more than once a week wants this one.
        """
        now = now.astimezone(KARACHI) if now else datetime.now(KARACHI)
        return cls.week_of(now.date() - timedelta(days=now.weekday()))

    def contains(self, when: datetime) -> bool:
        return self.start <= when.astimezone(KARACHI) < self.end


class Event(BaseModel):
    """A single normalized event. `id` is derived, not supplied."""

    title: str
    venue: str | None = None
    starts_at: datetime
    ends_at: datetime | None = None
    category: str | None = None
    price_text: str | None = None
    url: str
    sources: list[str] = Field(default_factory=list)
    series_key: str | None = None
    description: str | None = None
    raw: dict | None = None

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        return _require_aware(v)

    @field_validator("description")
    @classmethod
    def _truncate_description(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        return v[:DESCRIPTION_MAX_CHARS] if len(v) > DESCRIPTION_MAX_CHARS else v

    @model_validator(mode="after")
    def _times_in_karachi(self) -> Event:
        object.__setattr__(self, "starts_at", self.starts_at.astimezone(KARACHI))
        if self.ends_at is not None:
            object.__setattr__(self, "ends_at", self.ends_at.astimezone(KARACHI))
        return self

    @property
    def primary_source(self) -> str | None:
        return self.sources[0] if self.sources else None

    @property
    def id(self) -> str:
        """Deterministic id from (primary_source, url).

        Stable across runs so the store can upsert. Dedup may merge two events
        into one; the survivor keeps its own id.
        """
        primary = self.primary_source or ""
        digest = hashlib.sha256(f"{primary}\n{self.url}".encode()).hexdigest()
        return digest[:16]
