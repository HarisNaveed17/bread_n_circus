"""M0 done-when: `run --dry-run` executes end to end with zero sources
registered and prints an empty digest without crashing.

These pin `load_enabled_sources` to `[]` so they stay offline and
deterministic regardless of what's flipped on in `sources.yaml`.
"""

from typer.testing import CliRunner

import isb_events.pipeline as pipeline
from isb_events.cli import app

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
