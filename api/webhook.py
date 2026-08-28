"""Vercel entrypoint. Adapter only — the logic lives in `bot/app.py`.

Vercel's Python runtime serves each file under `api/` as a function and looks
for a `BaseHTTPRequestHandler` subclass named `handler`.
"""

from __future__ import annotations

import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# The function's working directory is not the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import app  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def _respond(self, status: int, body: str) -> None:
        encoded = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        params = {k: v[0] for k, v in query.items()}
        self._respond(*app.handle_verify(params))

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length)
        signature = self.headers.get("X-Hub-Signature-256")
        self._respond(*app.handle_event(raw_body, signature))
