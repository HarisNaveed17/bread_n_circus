"""Suite-wide guards.

`tests/test_cli.py` and others set `ISB_DB_PATH` to a temporary file, which is
only safe if nothing else points the store somewhere real. Sourcing `.env` —
which you do for nearly every other command in this project — used to be enough
to send the whole suite at the production Turso database.

`Store.open()` now prefers an explicit `ISB_DB_PATH`, so that specific route is
closed. This is the second lock: no test can reach Turso or the Anthropic API
even if the variables are exported, whatever any individual test forgets.

Keeping the invariant mechanical rather than remembered is the point — see
CLAUDE.md § Working notes, "Tests never touch the network".
"""

import pytest

from isb_events import linkpage

# Cleared for every test. Anything here is a credential that reaches a real
# service and costs money, mutates shared state, or both.
REAL_SERVICE_VARS = (
    "TURSO_DATABASE_URL",
    "TURSO_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "GITHUB_DISPATCH_TOKEN",
)


@pytest.fixture(autouse=True)
def _no_real_services(monkeypatch):
    for name in REAL_SERVICE_VARS:
        monkeypatch.delenv(name, raising=False)


class _NetworkAttempt(BaseException):
    pass


@pytest.fixture(autouse=True)
def _no_page_fetches(monkeypatch):
    """`extract()` fetches a linked page when a listing has no date.

    It does that through `linkpage._get`, which this replaces for every test —
    a listing gaining a URL should never quietly turn a unit test into an HTTP
    request. Tests that exercise the fetch pass their own `fetch=`/`read_page=`
    and never reach this.

    `_NetworkAttempt` derives from `BaseException` on purpose: `page_text`
    swallows every `Exception` so that a dead link costs one listing and
    nothing more, which would turn this guard into a silent pass.
    """

    def _refuse(url):
        raise _NetworkAttempt(f"a test tried to fetch {url}")

    monkeypatch.setattr(linkpage, "_get", _refuse)
