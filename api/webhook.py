"""Vercel entrypoint. Adapter only — the logic lives in `bot/app.py`.

Vercel's Python runtime serves one WSGI/ASGI application declared by
`[tool.vercel] entrypoint` in `pyproject.toml`, so this exposes a plain WSGI
callable. Bare WSGI rather than a framework: the whole surface is one GET and
one POST, and adding Flask or FastAPI here would mean shipping a web framework
to serve two routes.

Note the import is `handle_event`/`handle_verify` rather than the `app` module
it lives in — `app` is the WSGI callable's name here, and shadowing it with
our own module is what made Vercel's entrypoint scanner point at the wrong
thing in the first place.
"""

from __future__ import annotations

import logging
import sys
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs

# Without this the root logger sits at WARNING and every log.info below is
# dropped, which leaves a healthy request and a broken one looking identical
# in Vercel's log view. Anything written to stdout/stderr is collected.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# The function's working directory is not the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.app import handle_event, handle_health, handle_verify  # noqa: E402


def app(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET").upper()

    if method == "GET":
        params = {k: v[0] for k, v in parse_qs(environ.get("QUERY_STRING", "")).items()}
        if "health" in params:
            status, body = handle_health(params["health"])
        else:
            status, body = handle_verify(params)
    elif method == "POST":
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        raw_body = environ["wsgi.input"].read(length) if length else b""
        # WSGI mangles header names: X-Hub-Signature-256 -> HTTP_X_HUB_SIGNATURE_256.
        signature = environ.get("HTTP_X_HUB_SIGNATURE_256")
        status, body = handle_event(raw_body, signature)
    else:
        status, body = 405, "method not allowed"

    encoded = body.encode()
    start_response(
        f"{status} {HTTPStatus(status).phrase}",
        [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Length", str(len(encoded))),
        ],
    )
    return [encoded]
