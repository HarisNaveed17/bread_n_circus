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
