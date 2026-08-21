"""Persistence over libSQL/Turso, with a local sqlite file for dev and tests.

The store speaks one sqlite dialect. Which backend it hits is decided by env:

- `TURSO_DATABASE_URL` (+ `TURSO_AUTH_TOKEN`) set -> remote libSQL.
- otherwise -> local file at `ISB_DB_PATH` (default `./isb_events.db`),
  or an in-memory db when `ISB_DB_PATH=":memory:"`.

No ORM. Upsert on `id`, refresh `last_seen`.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path

from .models import KARACHI, Event

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _now_iso() -> str:
    return datetime.now(KARACHI).isoformat()


class Store:
    """Thin wrapper over a sqlite/libSQL connection.

    Use as a context manager so the connection is always closed.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._migrate()

    # -- construction ---------------------------------------------------------

    @classmethod
    def open(cls) -> Store:
        url = os.environ.get("TURSO_DATABASE_URL")
        if url:
            return cls(_connect_libsql(url, os.environ.get("TURSO_AUTH_TOKEN")))
        path = os.environ.get("ISB_DB_PATH", "isb_events.db")
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return cls(conn)

    def _migrate(self) -> None:
        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            self._conn.executescript(sql_file.read_text())
        self._conn.commit()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    # -- events ---------------------------------------------------------------

    def upsert_event(self, event: Event) -> None:
        """Insert or refresh an event. Preserves `first_seen`, bumps `last_seen`."""
        now = _now_iso()
        self._conn.execute(
            """
            INSERT INTO events (
                id, title, venue, starts_at, ends_at, category, price_text,
                url, sources, series_key, description, raw_json,
                first_seen, last_seen
            ) VALUES (
                :id, :title, :venue, :starts_at, :ends_at, :category, :price_text,
                :url, :sources, :series_key, :description, :raw_json,
                :now, :now
            )
            ON CONFLICT(id) DO UPDATE SET
                title       = excluded.title,
                venue       = excluded.venue,
                starts_at   = excluded.starts_at,
                ends_at     = excluded.ends_at,
                category    = excluded.category,
                price_text  = excluded.price_text,
                url         = excluded.url,
                sources     = excluded.sources,
                series_key  = excluded.series_key,
                description = excluded.description,
                raw_json    = excluded.raw_json,
                last_seen   = excluded.last_seen
            """,
            {
                "id": event.id,
                "title": event.title,
                "venue": event.venue,
                "starts_at": event.starts_at.isoformat(),
                "ends_at": event.ends_at.isoformat() if event.ends_at else None,
                "category": event.category,
                "price_text": event.price_text,
                "url": event.url,
                "sources": json.dumps(event.sources),
                "series_key": event.series_key,
                "description": event.description,
                "raw_json": json.dumps(event.raw) if event.raw is not None else None,
                "now": now,
            },
        )
        self._conn.commit()

    def upsert_events(self, events: list[Event]) -> None:
        for event in events:
            self.upsert_event(event)

    # -- digests --------------------------------------------------------------

    def save_digest(self, week_of: date, rendered_text: str, event_ids: list[str]) -> None:
        self._conn.execute(
            """
            INSERT INTO digests (week_of, rendered_text, event_ids, created_at, sent_at)
            VALUES (:week_of, :rendered_text, :event_ids, :created_at, NULL)
            ON CONFLICT(week_of) DO UPDATE SET
                rendered_text = excluded.rendered_text,
                event_ids     = excluded.event_ids,
                created_at    = excluded.created_at,
                sent_at       = NULL
            """,
            {
                "week_of": week_of.isoformat(),
                "rendered_text": rendered_text,
                "event_ids": json.dumps(event_ids),
                "created_at": _now_iso(),
            },
        )
        self._conn.commit()

    def get_digest(self, week_of: date) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM digests WHERE week_of = ?", (week_of.isoformat(),)
        ).fetchone()
        return dict(row) if row else None

    def mark_digest_sent(self, week_of: date) -> None:
        self._conn.execute(
            "UPDATE digests SET sent_at = ? WHERE week_of = ?",
            (_now_iso(), week_of.isoformat()),
        )
        self._conn.commit()


def _connect_libsql(url: str, auth_token: str | None) -> sqlite3.Connection:
    """Lazily open a libSQL connection; import only when Turso is configured."""
    import libsql_experimental as libsql  # noqa: PLC0415

    conn = libsql.connect(database=url, auth_token=auth_token or "")
    return conn
