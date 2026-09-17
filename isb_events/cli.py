"""Typer CLI: `fetch`, `render`, `send`, `run`, `check`.

Every command takes `--dry-run` and `--week-of YYYY-MM-DD` (default: the coming
Mon–Sun). `render` writes the `digests` row; `send` reads it. Text never passes
from render to send in memory. `check` reads the store back and exits non-zero
if the data says the pipeline has stopped — see `check.py`.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import typer

from . import check as health
from . import pipeline
from .intake import drain
from .models import KARACHI, DigestWindow, Event
from .normalize import dedupe
from .notify.base import DryRunNotifier, Notifier
from .pipeline import run_fetch
from .render import event_blocks
from .render import render as render_events
from .store import Store

app = typer.Typer(add_completion=False, help="Islamabad weekly event digest.")

WeekOpt = typer.Option(None, "--week-of", help="Monday of the target week (YYYY-MM-DD).")
DryRunOpt = typer.Option(False, "--dry-run", help="Print instead of persisting/sending.")


def _window(week_of: str | None) -> DigestWindow:
    if week_of:
        return DigestWindow.week_of(date.fromisoformat(week_of))
    return DigestWindow.coming_week()


def _windows(week_of: str | None) -> list[DigestWindow]:
    """The weeks a bare `render` should refresh: the current one and the next.

    An explicit `--week-of` means exactly that week and nothing else.

    Rendering only `coming_week` was right while the cron ran once, on a
    Saturday. It is wrong for a cron that runs daily: from Tuesday onwards
    `coming_week` is *next* week, so today's and tomorrow's listings — the ones
    the bot's day views read — would never be refreshed again until Monday.
    Both weeks are rendered so that "what's on today" is as fresh as the last
    run, and next week's digest is still built ahead of time.
    """
    if week_of:
        return [DigestWindow.week_of(date.fromisoformat(week_of))]
    current = DigestWindow.current_week()
    coming = DigestWindow.coming_week()
    return [current] if current == coming else [current, coming]


def _week_start(window: DigestWindow) -> date:
    return window.start.date()


def _drain_intake(store: Store) -> None:
    """Turn forwarded listings into events before rendering anything.

    Before the windows, not inside them: a listing can land in either week, and
    extracting once per window would pay twice for the same text.

    A failure here must not cost the digest. Intake is additive — the scraped
    sources are the bulk of it — so a missing API key or a model outage should
    mean "no new forwarded events this run", not "no digest".
    """
    try:
        created = drain(store)
    except Exception:
        logging.getLogger(__name__).exception("intake failed; rendering without it")
        return
    if created:
        typer.echo(f"intake: {len(created)} forwarded listing(s) became events")


def _events_for(window: DigestWindow, store: Store, result) -> list[Event]:
    """What the digest should show: everything stored for the week, deduped.

    Not `result.events`. A fetch is one sample — theblackhole.pk answers a rate
    limit with an empty 200, so an unlucky run used to render a digest with that
    source silently missing while its events sat in the store untouched. Reading
    the store back makes a failed fetch mean "nothing new", not "nothing at all".

    Falls back to the fetch if the store read fails, so a broken query degrades
    to the old behaviour rather than to an empty digest.
    """
    try:
        stored = store.events_in_window(window)
    except Exception:
        logging.getLogger(__name__).exception("could not read stored events; using the fetch")
        return result.events
    return dedupe(stored)


def _notifier(dry_run: bool) -> Notifier:
    """Delivery is pull, not push — there is nothing to send digests *to*.

    Telegram is gone (banned in Pakistan). Its replacement is not another
    push channel: the WhatsApp bot in `bot/` serves the stored digest when
    someone asks for it, which is why the cron renders and stops. The weekly
    nudge is Phase 2 and needs a Meta-approved template, so until it exists a
    non-dry-run send has no channel and says so rather than failing obscurely.
    """
    if dry_run:
        return DryRunNotifier()
    typer.echo(
        "No push channel is configured. Digests are delivered by the WhatsApp "
        "bot on request (see bot/ and CLAUDE.md § Delivery); the weekly nudge "
        "is Phase 2. Use --dry-run to print the stored digest.",
        err=True,
    )
    raise typer.Exit(1)


@app.callback()
def _setup(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


@app.command()
def fetch(week_of: str = WeekOpt, dry_run: bool = DryRunOpt) -> None:
    """Scrape enabled sources into the store."""
    window = _window(week_of)
    with Store.open() as store:
        result = run_fetch(window, store)
    typer.echo(f"fetched {len(result.events)} events; failures: {result.failures or 'none'}")


@app.command()
def render(week_of: str = WeekOpt, dry_run: bool = DryRunOpt) -> None:
    """Render stored events into the `digests` row.

    With no `--week-of`, refreshes both the current week and the coming one.
    """
    with Store.open() as store:
        _drain_intake(store)
        for window in _windows(week_of):
            result = run_fetch(window, store)
            events = _events_for(window, store, result)
            messages = render_events(events, window)
            for line in result.footer_lines():
                messages[-1] += f"\n{line}"
            text = "\n\n===MESSAGE===\n\n".join(messages)
            if dry_run:
                typer.echo(text)
                continue
            store.save_digest(_week_start(window), text, [e.id for e in events])
            # The same render, per event, so the bot can serve "what's on today"
            # without a second copy of the formatting rules. See render.event_blocks.
            store.save_digest_events(_week_start(window), event_blocks(events, window))
            typer.echo(f"saved digest for week of {_week_start(window)}")


@app.command()
def send(week_of: str = WeekOpt, dry_run: bool = DryRunOpt, prefix: str = typer.Option("")) -> None:
    """Send the stored digest for the week. Reads from the `digests` row."""
    window = _window(week_of)
    week_start = _week_start(window)
    with Store.open() as store:
        row = store.get_digest(week_start)
        if row is None:
            typer.echo(f"no digest stored for week of {week_start}; run `render` first")
            raise typer.Exit(1)
        messages = row["rendered_text"].split("\n\n===MESSAGE===\n\n")
        if prefix:
            messages[0] = f"{prefix} {messages[0]}"
        _notifier(dry_run).send(messages)
        if not dry_run:
            store.mark_digest_sent(week_start)
            typer.echo(f"sent digest for week of {week_start}")


@app.command()
def run(week_of: str = WeekOpt, dry_run: bool = DryRunOpt) -> None:
    """End to end: fetch -> render -> send."""
    window = _window(week_of)
    week_start = _week_start(window)
    with Store.open() as store:
        _drain_intake(store)
        result = run_fetch(window, store)
        events = _events_for(window, store, result)
        messages = render_events(events, window)
        for line in result.footer_lines():
            messages[-1] += f"\n{line}"
        text = "\n\n===MESSAGE===\n\n".join(messages)
        if not dry_run:
            store.save_digest(week_start, text, [e.id for e in events])
            store.save_digest_events(week_start, event_blocks(events, window))
        _notifier(dry_run).send(messages)
        if not dry_run:
            store.mark_digest_sent(week_start)


def _now() -> datetime:
    """Seam for tests to freeze the clock `check` compares ages against."""
    return datetime.now(KARACHI)


@app.command()
def check(
    week_of: str = WeekOpt,
    max_digest_age: float = typer.Option(
        health.MAX_DIGEST_AGE.total_seconds() / 3600,
        help="Hours before a digest row counts as stale.",
    ),
    max_source_age: float = typer.Option(
        health.MAX_SOURCE_AGE.total_seconds() / 3600,
        help="Hours since a source last contributed before it counts as silent.",
    ),
    max_intake_age: float = typer.Option(
        health.MAX_INTAKE_AGE.total_seconds() / 3600,
        help="Hours a forwarded listing may wait unprocessed.",
    ),
    digest: bool = typer.Option(
        False, "--digest", help="Also print the rendered digest text for the checked weeks."
    ),
) -> None:
    """Read the store back and fail if the data says the pipeline has stopped.

    Runs after every render, and on its own schedule so that a render that never
    fired still gets noticed. Nothing here fetches or writes.
    """
    now = _now()
    sources = [s.slug for s in pipeline.load_enabled_sources()]
    with Store.open() as store:
        for line in health.summary(store, now=now, sources=sources):
            typer.echo(line)
        if digest:
            for window in _windows(week_of):
                row = store.get_digest(_week_start(window))
                typer.echo(f"\n### Digest for week of {_week_start(window)}\n")
                typer.echo("```")
                typer.echo(row["rendered_text"] if row else "(no row)")
                typer.echo("```")
        problems = health.check(
            store,
            now=now,
            sources=sources,
            week_of=date.fromisoformat(week_of) if week_of else None,
            max_digest_age=timedelta(hours=max_digest_age),
            max_source_age=timedelta(hours=max_source_age),
            max_intake_age=timedelta(hours=max_intake_age),
        )
    typer.echo()
    if not problems:
        typer.echo("OK — every check passed")
        return
    for problem in problems:
        typer.echo(f"**{problem}**")
    raise typer.Exit(1)


if __name__ == "__main__":
    app()
