# isb-events

A weekly pipeline that scrapes Islamabad event listings, deduplicates them,
renders a digest, and delivers it.

> Delivery is mid-migration from Telegram (banned in Pakistan) to WhatsApp.
> The `send` command still speaks Telegram; the weekly cron deliberately does
> not send at all yet. See `CLAUDE.md` § Delivery.

## Quickstart

```bash
uv sync
uv run isb-events run --dry-run          # end to end, prints, persists nothing
uv run isb-events run --dry-run --week-of 2026-08-24
```

## Commands

| Command  | Does                                              |
|----------|---------------------------------------------------|
| `fetch`  | Scrape enabled sources into the store             |
| `render` | Render stored events into the `digests` row       |
| `send`   | Read the stored digest and deliver it             |
| `run`    | `fetch` → `render` → `send` in one shot           |

All take `--dry-run` and `--week-of YYYY-MM-DD` (default: the coming Mon–Sun).

## Configuration

- Sources are declared in `sources.yaml` (`enabled` flag per source).
- Storage: set `TURSO_DATABASE_URL` + `TURSO_AUTH_TOKEN` for remote libSQL, or
  leave unset to use a local sqlite file (`ISB_DB_PATH`, default `isb_events.db`).
- Delivery: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

## Automation

`.github/workflows/weekly-digest.yml` runs `render` every Saturday at 10:00
Karachi (05:00 UTC), scraping the enabled sources and saving the digest into
the store. It sends nothing — that lands with the WhatsApp work.

Needs two repo secrets: `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN`. The job
fails fast without them rather than silently writing to a throwaway sqlite
file on the runner. Trigger it by hand from the Actions tab ("Run workflow"),
optionally passing a `week_of` date; the rendered digest is echoed into the
run summary either way.

## Development

```bash
uv run pytest          # offline — no test touches the network
uv run ruff check .
uv run ruff format .
```

All datetimes are timezone-aware in `Asia/Karachi`.
