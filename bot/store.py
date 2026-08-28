"""Read the stored digest from Turso over its HTTP API.

No libSQL driver: `libsql-experimental` is a compiled extension and a poor fit
for a serverless function, and this side only ever runs one SELECT. The HTTP
API needs nothing but `httpx`.
"""

from __future__ import annotations

import os

import httpx

TIMEOUT = 10.0

# The pipeline joins a multi-part digest with this marker before storing it
# (`isb_events/cli.py`). Duplicated rather than imported, because importing it
# would drag the whole package — and its compiled driver — into the function.
# `tests/test_bot.py` pins the two copies together.
MESSAGE_SEPARATOR = "\n\n===MESSAGE===\n\n"

# The newest digest is always the right one to serve: the cron only ever
# renders forward. Mon-Fri the newest row is the current week; from Saturday
# 10:00, when the cron has run, it is the week about to start — which is what
# someone asking "what's on" on a Saturday means.
LATEST_DIGEST_SQL = "SELECT rendered_text, week_of FROM digests ORDER BY week_of DESC LIMIT 1"


def _http_url(database_url: str) -> str:
    """`libsql://host` -> `https://host`; the HTTP API lives on the same host."""
    _, _, rest = database_url.partition("://")
    return f"https://{rest or database_url}"


def _cell(value: dict) -> str | None:
    """Turso returns typed cells: {"type": "text", "value": "..."}."""
    return None if value.get("type") == "null" else value.get("value")


def query(sql: str, args: list[str] | None = None) -> list[list[str | None]]:
    url = os.environ["TURSO_DATABASE_URL"]
    token = os.environ["TURSO_AUTH_TOKEN"]
    stmt: dict = {"sql": sql}
    if args:
        stmt["args"] = [{"type": "text", "value": a} for a in args]
    resp = httpx.post(
        f"{_http_url(url)}/v2/pipeline",
        headers={"Authorization": f"Bearer {token}"},
        json={"requests": [{"type": "execute", "stmt": stmt}, {"type": "close"}]},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    first = body["results"][0]
    if first.get("type") != "ok":
        raise RuntimeError(f"Turso query failed: {first.get('error')}")
    return [[_cell(c) for c in row] for row in first["response"]["result"]["rows"]]


def latest_digest() -> tuple[str, str] | None:
    """`(rendered_text, week_of)` for the newest digest, or None if there is none."""
    rows = query(LATEST_DIGEST_SQL)
    if not rows or rows[0][0] is None:
        return None
    return rows[0][0], rows[0][1] or ""


def digest_messages() -> list[str]:
    """The digest split back into the messages the renderer packed it into."""
    found = latest_digest()
    if found is None:
        return []
    text, _ = found
    return [part for part in text.split(MESSAGE_SEPARATOR) if part.strip()]
