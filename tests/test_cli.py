"""M0 done-when: `run --dry-run` executes end to end with zero sources
registered and prints an empty digest without crashing.

These pin `load_enabled_sources` to `[]` so they stay offline and
deterministic regardless of what's flipped on in `sources.yaml`.
"""

from datetime import datetime

from typer.testing import CliRunner

import isb_events.pipeline as pipeline
from isb_events.cli import app
from isb_events.models import KARACHI, Event

runner = CliRunner()


def test_run_dry_run_with_no_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("ISB_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(pipeline, "load_enabled_sources", lambda: [])
    result = runner.invoke(app, ["run", "--dry-run", "--week-of", "2026-08-24"])
    assert result.exit_code == 0, result.output
    assert "No events found" in result.output


def test_render_then_send_roundtrips_through_store(tmp_path, monkeypatch):
    monkeypatch.setenv("ISB_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(pipeline, "load_enabled_sources", lambda: [])
    r1 = runner.invoke(app, ["render", "--week-of", "2026-08-24"])
    assert r1.exit_code == 0, r1.output
    r2 = runner.invoke(app, ["send", "--week-of", "2026-08-24", "--dry-run"])
    assert r2.exit_code == 0, r2.output
    assert "No events found" in r2.output


def test_send_without_dry_run_reports_that_there_is_no_push_channel(tmp_path, monkeypatch):
    """Telegram is deleted and the Phase 2 nudge does not exist yet.

    The failure has to name the reason: silently doing nothing, or a bare
    traceback from a missing notifier, both read as a bug in the pipeline.
    """
    monkeypatch.setenv("ISB_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(pipeline, "load_enabled_sources", lambda: [])
    runner.invoke(app, ["render", "--week-of", "2026-08-24"])
    result = runner.invoke(app, ["send", "--week-of", "2026-08-24"])
    assert result.exit_code == 1
    assert "no push channel" in result.output.lower()


# -- a source going quiet must not empty it out of the digest ----------------


class _Source:
    """A source that returns events on the first fetch and nothing after.

    theblackhole.pk answers a rate limit with an HTTP 200 and an empty body, so
    this is not hypothetical: it is what a real run looks like when unlucky.
    """

    slug = "flaky"

    def __init__(self, events):
        self.events = events
        self.calls = 0

    def fetch(self, window):
        self.calls += 1
        return self.events if self.calls == 1 else []


def _event(title, day):
    return Event(
        title=title,
        starts_at=datetime(2026, 8, day, 19, 0, tzinfo=KARACHI),
        url=f"https://x.pk/{title}",
        sources=["flaky"],
    )


def test_a_source_returning_nothing_keeps_its_stored_events_in_the_digest(tmp_path, monkeypatch):
    """The regression: the digest used to render the fetch, not the store.

    A run where a source came back empty dropped that source from the digest
    entirely, with its events sitting in the events table untouched. Black Hole
    vanished from several days of real digests this way.
    """
    monkeypatch.setenv("ISB_DB_PATH", str(tmp_path / "test.db"))
    source = _Source([_event("Calligraphy", 25)])
    monkeypatch.setattr(pipeline, "load_enabled_sources", lambda: [source])

    first = runner.invoke(app, ["render", "--week-of", "2026-08-24", "--dry-run"])
    assert "Calligraphy" in first.output

    # Second run: the source is having a bad day and returns nothing at all.
    second = runner.invoke(app, ["render", "--week-of", "2026-08-24", "--dry-run"])
    assert source.calls == 2
    assert "Calligraphy" in second.output, "a quiet source must not empty the digest"


def test_new_events_still_appear_alongside_stored_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("ISB_DB_PATH", str(tmp_path / "test.db"))
    source = _Source([_event("Calligraphy", 25)])
    monkeypatch.setattr(pipeline, "load_enabled_sources", lambda: [source])
    runner.invoke(app, ["render", "--week-of", "2026-08-24", "--dry-run"])

    source.events = [_event("Film Screening", 26)]
    source.calls = 0  # fetch again, with something new this time
    out = runner.invoke(app, ["render", "--week-of", "2026-08-24", "--dry-run"]).output
    assert "Calligraphy" in out and "Film Screening" in out
