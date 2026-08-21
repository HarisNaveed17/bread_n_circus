# isb-events

A weekly pipeline that scrapes Islamabad event listings, deduplicates them,
renders a digest, and delivers it to Telegram on a GitHub Actions cron.

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

## Development

```bash
uv run pytest          # offline — no test touches the network
uv run ruff check .
uv run ruff format .
```

All datetimes are timezone-aware in `Asia/Karachi`.
