"""The failure test: does `check` notice when the pipeline has silently stopped?

Every scenario here is one the project has actually had, or the mirror image of
one. None of them raises anywhere in the pipeline — the run goes green and the
digest keeps rendering from the store — which is why the ordinary tests cannot
catch them and why `check` reads the data instead of the code.

Both backends, as for the store tests: every query `check` relies on has to bind
and return the same way on sqlite3 and on libSQL.
"""

import sqlite3
from datetime import date, datetime, timedelta

import libsql_experimental
import pytest
from typer.testing import CliRunner

import isb_events.cli as cli
import isb_events.pipeline as pipeline
import isb_events.store as store_module
from isb_events.check import FAIL, check, summary
from isb_events.models import KARACHI, Event
from isb_events.store import Store

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=KARACHI)  # a Wednesday
MONDAY = date(2026, 9, 14)
SOURCES = ["blackhole", "ticketwala"]


@pytest.fixture(params=["sqlite3", "libsql"])
def store(request):
    if request.param == "sqlite3":
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
    else:
        conn = libsql_experimental.connect(":memory:")
    with Store(conn) as s:
        yield s


def _event(title, source, day=17, hour=19):
    return Event(
        title=title,
        starts_at=datetime(2026, 9, day, hour, tzinfo=KARACHI),
        url=f"https://{source}.pk/{title.lower().replace(' ', '-')}",
        sources=[source],
    )


def _seen(store, event, when):
    """Upsert `event` as if the pipeline had found it at `when`."""
    store.upsert_event(event)
    store._conn.execute(
        "UPDATE events SET last_seen = ?, first_seen = ? WHERE id = ?",
        (when.isoformat(), when.isoformat(), event.id),
    )
    store._conn.commit()


def _digest(store, when, week=MONDAY, upcoming=("Open Mic",)):
    store.save_digest(week, "*Islamabad*", [])
    store._conn.execute(
        "UPDATE digests SET created_at = ? WHERE week_of = ?", (when.isoformat(), week.isoformat())
    )
    store._conn.commit()
    store.save_digest_events(
        week,
        [
            {
                "id": f"id-{i}",
                "event_date": "2026-09-17",
                "day_label": "Thu 17 Sep",
                "starts_at": "2026-09-17T19:00:00+05:00",
                "category": None,
                "block": f"• *{title}*",
            }
            for i, title in enumerate(upcoming)
        ],
    )


def _healthy(store, when=NOW):
    _digest(store, when - timedelta(hours=2))
    _seen(store, _event("Open Mic", "blackhole"), when - timedelta(hours=2))
    _seen(store, _event("Kaavish Live", "ticketwala"), when - timedelta(hours=2))


def _failures(problems):
    return [p.message for p in problems if p.level == FAIL]


# -- the healthy case is quiet -----------------------------------------------


def test_a_healthy_store_raises_no_problems(store):
    _healthy(store)
    assert check(store, now=NOW, sources=SOURCES) == []


def test_one_dropped_cron_firing_is_tolerated(store):
    """A single missed run is expected (CLAUDE.md § Open threads), not an alert."""
    _healthy(store, when=NOW - timedelta(hours=14))
    assert check(store, now=NOW, sources=SOURCES) == []


# -- the digest stopped being rendered ---------------------------------------


def test_no_digest_for_this_week_is_a_failure(store):
    _seen(store, _event("Open Mic", "blackhole"), NOW)
    _seen(store, _event("Kaavish Live", "ticketwala"), NOW)
    problems = _failures(check(store, now=NOW, sources=SOURCES))
    assert any("no digest row for the week of 2026-09-14" in p for p in problems)


def test_a_digest_two_missed_runs_old_is_a_failure(store):
    _healthy(store, when=NOW - timedelta(hours=28))
    problems = _failures(check(store, now=NOW, sources=SOURCES))
    assert any("last rendered 30.0h ago" in p for p in problems)


def test_an_explicit_week_is_checked_instead_of_the_current_one(store):
    _healthy(store)
    problems = check(store, now=NOW, sources=SOURCES, week_of=date(2026, 9, 21))
    assert any("week of 2026-09-21" in p for p in _failures(problems))


# -- a source went quiet while its old events kept the digest looking full ---


def test_a_source_whose_scrape_returns_nothing_is_named(store):
    """The Black Hole failure: an empty 200, six digests, nobody noticed.

    The digest is fresh and full — render reads the store — so every other
    signal is green. The only thing that stops moving is `last_seen`.
    """
    _digest(store, NOW - timedelta(hours=1))
    _seen(store, _event("Kaavish Live", "ticketwala"), NOW - timedelta(hours=1))
    _seen(store, _event("Open Mic", "blackhole"), NOW - timedelta(hours=48))
    (problem,) = _failures(check(store, now=NOW, sources=SOURCES))
    assert problem.startswith("source blackhole last contributed 48.0h ago")
    assert "ticketwala" not in problem


def test_a_source_that_has_never_contributed_is_named(store):
    _digest(store, NOW)
    _seen(store, _event("Kaavish Live", "ticketwala"), NOW)
    (problem,) = _failures(check(store, now=NOW, sources=SOURCES))
    assert problem == "source blackhole has never contributed an event"


def test_a_merged_event_counts_for_both_of_its_sources(store):
    """Dedup can fold two listings into one row carrying both slugs."""
    _digest(store, NOW)
    both = _event("Kaavish Live", "ticketwala").model_copy(
        update={"sources": ["ticketwala", "blackhole"]}
    )
    _seen(store, both, NOW)
    assert check(store, now=NOW, sources=SOURCES) == []


def test_intake_channels_are_not_held_to_the_source_rule(store):
    """A forwarded listing is extracted once and never re-seen; its age means nothing."""
    _healthy(store)
    _seen(store, _event("Chess N Jams", "whatsapp"), NOW - timedelta(days=10))
    assert check(store, now=NOW, sources=SOURCES) == []


def test_the_source_threshold_is_adjustable(store):
    _healthy(store, when=NOW - timedelta(hours=10))
    assert check(store, now=NOW, sources=SOURCES) == []
    tight = check(store, now=NOW, sources=SOURCES, max_source_age=timedelta(hours=6))
    assert len(_failures(tight)) == 2


# -- the bot would have nothing to say ---------------------------------------


def test_an_empty_upcoming_window_is_a_failure(store):
    _digest(store, NOW, upcoming=())
    _seen(store, _event("Open Mic", "blackhole"), NOW)
    _seen(store, _event("Kaavish Live", "ticketwala"), NOW)
    (problem,) = _failures(check(store, now=NOW, sources=SOURCES))
    assert problem.startswith("0 events in digest_events for the 7 days from 2026-09-16")


# -- forwarded listings are stuck in the queue -------------------------------


def _queue(store, when, intake_id="i1"):
    store._conn.execute(
        "INSERT INTO intake (id, channel, sender, body, received_at) VALUES (?,?,?,?,?)",
        (intake_id, "whatsapp", "923001234567", "a listing", when.isoformat()),
    )
    store._conn.commit()


def test_a_listing_queued_for_a_day_is_a_failure(store):
    _healthy(store)
    _queue(store, NOW - timedelta(hours=30))
    (problem,) = _failures(check(store, now=NOW, sources=SOURCES))
    assert problem.startswith("1 listing(s) queued, the oldest for 30.0h")


def test_a_freshly_queued_listing_is_fine(store):
    _healthy(store)
    _queue(store, NOW - timedelta(minutes=5))
    assert check(store, now=NOW, sources=SOURCES) == []


def test_a_processed_listing_does_not_count(store):
    _healthy(store)
    _queue(store, NOW - timedelta(days=3))
    store.mark_intake_processed("i1", decline_reason="declined")
    assert check(store, now=NOW, sources=SOURCES) == []


# -- the summary is the run-summary text -------------------------------------


def test_the_summary_reports_every_number_a_person_would_scan(store):
    _healthy(store)
    _seen(store, _event("Chess N Jams", "whatsapp"), NOW)
    text = "\n".join(summary(store, now=NOW, sources=SOURCES))
    assert "2026-09-14 (rendered 2.0h ago" in text
    assert "blackhole seen 2.0h ago (1 events)" in text
    assert "whatsapp 1 events (intake, not checked)" in text
    assert "upcoming: 1 events" in text
    assert "intake: 0 pending" in text
    assert "subscribers: 0 contacted" in text


def test_the_summary_names_a_source_never_seen(store):
    _digest(store, NOW)
    assert "blackhole NEVER SEEN" in "\n".join(summary(store, now=NOW, sources=SOURCES))


# -- end to end: the scenario that actually happened, through the CLI --------


class _Source:
    """Returns its events until told the site has gone quiet."""

    slug = "blackhole"

    def __init__(self, events):
        self.events = events

    def fetch(self, window):
        return self.events


runner = CliRunner()


def test_a_source_that_goes_quiet_still_renders_but_fails_check(tmp_path, monkeypatch):
    """The empty-200 failure, end to end.

    Run one finds the event. The site then starts answering with nothing. The
    next two days of renders go green and the digest still lists the event —
    that is the render-from-store fix doing its job — and *nothing in the
    render path can tell*. `check` can, because `last_seen` stopped moving.
    """
    monkeypatch.setenv("ISB_DB_PATH", str(tmp_path / "test.db"))
    source = _Source([_event("Open Mic", "blackhole", day=17)])
    monkeypatch.setattr(pipeline, "load_enabled_sources", lambda: [source])

    clock = [datetime(2026, 9, 14, 12, 0, tzinfo=KARACHI)]
    monkeypatch.setattr(store_module, "_now_iso", lambda: clock[0].isoformat())
    monkeypatch.setattr(cli, "_now", lambda: clock[0])

    def render():
        result = runner.invoke(cli.app, ["render", "--week-of", "2026-09-14"])
        assert result.exit_code == 0, result.output
        return result.output

    def check_cli():
        return runner.invoke(cli.app, ["check", "--week-of", "2026-09-14"])

    render()
    assert check_cli().exit_code == 0

    # The site now answers every fetch with an empty body. Not an error.
    source.events = []
    for _ in range(4):
        clock[0] += timedelta(hours=12)
        render()

    # Two days on, the digest is fresh and the event is still in it...
    shown = runner.invoke(cli.app, ["send", "--week-of", "2026-09-14", "--dry-run"]).output
    assert "Open Mic" in shown

    # ...and only `check` knows the source has been silent the whole time.
    result = check_cli()
    assert result.exit_code == 1, result.output
    assert "source blackhole last contributed 48.0h ago" in result.output
    assert "OK" not in result.output


def test_check_passes_and_prints_the_digest_on_a_healthy_store(tmp_path, monkeypatch):
    monkeypatch.setenv("ISB_DB_PATH", str(tmp_path / "test.db"))
    source = _Source([_event("Open Mic", "blackhole", day=17)])
    monkeypatch.setattr(pipeline, "load_enabled_sources", lambda: [source])
    monkeypatch.setattr(cli, "_now", lambda: datetime(2026, 9, 14, 12, 0, tzinfo=KARACHI))
    monkeypatch.setattr(
        store_module, "_now_iso", lambda: "2026-09-14T11:00:00+05:00"
    )  # rendered an hour ago

    assert runner.invoke(cli.app, ["render", "--week-of", "2026-09-14"]).exit_code == 0
    result = runner.invoke(cli.app, ["check", "--week-of", "2026-09-14", "--digest"])
    assert result.exit_code == 0, result.output
    assert "OK — every check passed" in result.output
    assert "### Digest for week of 2026-09-14" in result.output
    assert "Open Mic" in result.output
