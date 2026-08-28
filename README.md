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
- Storage: see [Storage](#storage) below.
- Delivery: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

## Storage

With `TURSO_DATABASE_URL` unset the store writes to a local sqlite file
(`ISB_DB_PATH`, default `isb_events.db`) — that's the default for development
and tests, and needs no setup. Set `TURSO_DATABASE_URL` (plus
`TURSO_AUTH_TOKEN`) and the same code talks to a remote libSQL database
instead. The cron needs the remote one, because a GitHub runner's filesystem
dies with the job.

Schema migrations in `migrations/` run on every `Store.open()`, so a fresh
database needs no separate setup step.

### Provisioning a Turso database

```bash
curl -sSfL https://get.tur.so/install.sh | bash   # then restart your shell
turso auth login
turso db create isb-events
turso db show isb-events --url                   # -> libsql://isb-events-<org>.turso.io
turso db tokens create isb-events                # -> the auth token; shown once
```

Verify it locally before wiring the cron to it — this writes a real digest, so
it doubles as an end-to-end check:

```bash
export TURSO_DATABASE_URL="libsql://isb-events-<org>.turso.io"
export TURSO_AUTH_TOKEN="<token>"
uv run isb-events render
turso db shell isb-events "select week_of, length(rendered_text) from digests"
```

Keep the token out of the repo — `export` it in your shell (or an untracked
`.env`), never a tracked file.

### Giving the cron access

```bash
gh secret set TURSO_DATABASE_URL --body "$TURSO_DATABASE_URL"
gh secret set TURSO_AUTH_TOKEN   --body "$TURSO_AUTH_TOKEN"
gh secret list
```

Then dispatch the workflow by hand (below) before trusting the schedule.

## Automation

`.github/workflows/weekly-digest.yml` runs `render` every Saturday at 10:00
Karachi (05:00 UTC), scraping the enabled sources and saving the digest into
the store. It sends nothing — that lands with the WhatsApp work.

Needs two repo secrets: `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN`. The job
fails fast without them rather than silently writing to a throwaway sqlite
file on the runner. Trigger it by hand from the Actions tab ("Run workflow"),
optionally passing a `week_of` date; the rendered digest is echoed into the
run summary either way.

## WhatsApp bot (Phase 1)

`bot/` answers any inbound WhatsApp message with the latest stored digest;
`api/webhook.py` is the Vercel entrypoint. It never imports `isb_events` — it
reads Turso over the HTTP API with plain `httpx`, because the pipeline's libSQL
driver is a compiled extension and a poor fit for a serverless runtime. The two
sides share a database table, not a process.

Environment (set these in the Vercel project, not in the repo):

| Variable | What it is |
|----------|------------|
| `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN` | Same database the cron writes to |
| `WHATSAPP_PHONE_NUMBER_ID` | From the Meta app dashboard |
| `WHATSAPP_TOKEN` | Cloud API access token |
| `WHATSAPP_VERIFY_TOKEN` | A string you invent; Meta echoes it back at subscribe time |
| `WHATSAPP_APP_SECRET` | Signs inbound webhooks — without it every request is rejected |

Deploy, then point Meta at `https://<deployment>/api/webhook` with the same
`WHATSAPP_VERIFY_TOKEN` and subscribe to the `messages` field. Build against
the dashboard's free test number: it only reaches allowlisted recipients, but
the webhooks are real, so the one-shot registration of a real SIM stays out of
play until the bot works.

Replies are free-form text, which the Cloud API allows only inside the 24-hour
window a user opens by messaging first — so Phase 1 costs nothing and needs no
approved template. The weekly nudge template is Phase 2.

## Development

```bash
uv run pytest          # offline — no test touches the network
uv run ruff check .
uv run ruff format .
```

All datetimes are timezone-aware in `Asia/Karachi`.
