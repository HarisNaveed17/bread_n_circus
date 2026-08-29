"""Typer CLI: `fetch`, `render`, `send`, `run`.

Every command takes `--dry-run` and `--week-of YYYY-MM-DD` (default: the coming
Mon–Sun). `render` writes the `digests` row; `send` reads it. Text never passes
from render to send in memory.
"""

from __future__ import annotations

import logging
from datetime import date

import typer

from .models import DigestWindow
from .notify.base import DryRunNotifier, Notifier
from .pipeline import run_fetch
from .render import render as render_events
from .store import Store

app = typer.Typer(add_completion=False, help="Islamabad weekly event digest.")

WeekOpt = typer.Option(None, "--week-of", help="Monday of the target week (YYYY-MM-DD).")
DryRunOpt = typer.Option(False, "--dry-run", help="Print instead of persisting/sending.")


def _window(week_of: str | None) -> DigestWindow:
    if week_of:
        return DigestWindow.week_of(date.fromisoformat(week_of))
    return DigestWindow.coming_week()


def _week_start(window: DigestWindow) -> date:
    return window.start.date()


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
    """Render this week's stored events into the `digests` row."""
    window = _window(week_of)
    with Store.open() as store:
        result = run_fetch(window, store)
        messages = render_events(result.events, window)
        for line in result.footer_lines():
            messages[-1] += f"\n{line}"
        text = "\n\n===MESSAGE===\n\n".join(messages)
        if dry_run:
            typer.echo(text)
        else:
            event_ids = [e.id for e in result.events]
            store.save_digest(_week_start(window), text, event_ids)
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
        result = run_fetch(window, store)
        messages = render_events(result.events, window)
        for line in result.footer_lines():
            messages[-1] += f"\n{line}"
        text = "\n\n===MESSAGE===\n\n".join(messages)
        if not dry_run:
            store.save_digest(week_start, text, [e.id for e in result.events])
        _notifier(dry_run).send(messages)
        if not dry_run:
            store.mark_digest_sent(week_start)


if __name__ == "__main__":
    app()
