# isb-events

A weekly pipeline that scrapes Islamabad event listings, deduplicates them,
renders a digest, and delivers it.

> Delivery is pull, not push: the WhatsApp bot in `bot/` replies with the
> stored digest when someone asks. The cron renders and stops; `send` has no
> push channel until the Phase 2 nudge template exists. Telegram has been
> removed — it is banned in Pakistan. See `CLAUDE.md` § Delivery.

## Quickstart

```bash
uv sync --extra pipeline    # scrapers etc; the bare install is the bot's
uv run isb-events run --dry-run          # end to end, prints, persists nothing
uv run isb-events run --dry-run --week-of 2026-08-24
```

## Commands

| Command  | Does                                              |
|----------|---------------------------------------------------|
| `fetch`  | Scrape enabled sources into the store             |
| `render` | Render stored events into the `digests` row       |
| `send`   | Print the stored digest (`--dry-run`); no push channel yet |
| `run`    | `fetch` → `render` → `send` in one shot           |

All take `--dry-run` and `--week-of YYYY-MM-DD` (default: the coming Mon–Sun).

## Configuration

- Sources are declared in `sources.yaml` (`enabled` flag per source).
- Storage: see [Storage](#storage) below.

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
`api/webhook.py` is the Vercel entrypoint, a bare WSGI callable. It never imports `isb_events` — it
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

`WHATSAPP_PHONE_NUMBER_ID` is the **Phone number ID** shown beside the test
number on the app's API Setup page — an opaque numeric id, not the phone
number itself. `WHATSAPP_APP_SECRET` is under App settings → Basic.
`WHATSAPP_VERIFY_TOKEN` is a string you invent; it just has to match what you
type into Meta's webhook form.

The access token on the API Setup page **expires in 24 hours**. That is fine
for first tests, but a deployment needs a permanent one: create a System User
in Business settings, give it the WhatsApp accounts, and generate a token
there.

Check the values before deploying anything. `.env` is gitignored and is not in
the repo — create it from the template:

```bash
cp .env.example .env                 # then fill in the blanks
set -a; source .env; set +a          # nothing auto-loads it
uv run python -m bot.selftest        # reports which vars are set, tries Turso
uv run python -m bot.selftest 92300XXXXXXX   # ...and sends a real message
```

The recipient has to be on the test number's allowlist. That command exercises
the same `send_text` the webhook uses, so it fails on exactly what a live
webhook would fail on — before Meta is in the loop.

### Deploying

At vercel.com, sign in with GitHub → **Add New → Project** → import this repo.
Framework preset **Other**; leave the root directory alone. There is nothing
to build: Vercel serves each file under `api/` as a Python function and
installs `requirements.txt`.

The runtime serves **one** WSGI app per project and will not guess which:
several modules here expose a name called `app`. `[tool.vercel] entrypoint` in
`pyproject.toml` names it explicitly as `api.webhook:app`. Without that the
build fails before it starts, with "No python entrypoint found in default
locations".

Add the six environment variables in the project settings (these are separate
from the GitHub secrets; GitHub runs the cron, Vercel runs the bot, and both
need the Turso pair), then deploy. The webhook is at
`https://<project>.vercel.app/api/webhook`.

Then point Meta at that URL with the same `WHATSAPP_VERIFY_TOKEN`, and
subscribe to the `messages` field. The test
number only reaches allowlisted recipients, but its webhooks are real, so the
one-shot registration of a real SIM stays out of play until the bot works.

Replies are free-form text, which the Cloud API allows only inside the 24-hour
window a user opens by messaging first — so Phase 1 costs nothing and needs no
approved template. The weekly nudge template is Phase 2.

**Deployments no longer come from Vercel's Git integration.** `vercel.json`
sets `git.deploymentEnabled: false`, so a push to master builds nothing on
Vercel's side; GitHub Actions builds and uploads instead. See
[Deployment flow](#deployment-flow).

When something breaks, `RUNBOOK.md` has the diagnostic commands and how to
read their output.

## Deployment flow

Nothing reaches production automatically. Pushing to master gets you a preview
URL and a health check against it; promoting that to production is a button
you press.

```
push (any branch)                       you, in the Actions tab
      |                                          |
  commit subjects + ruff + pytest                |
      |                                          |
  tip commit is feat/fix/refactor?               |
      | yes                                      |
  vercel build && deploy  ->  preview URL        |
      |                                          |
  GET /api/webhook?health=  (Turso reachable?)   |
                                                 v
                            deploy-production.yml (workflow_dispatch)
                            re-runs the checks, builds --prod, health-checks
                            the production alias
```

`docs:` and `tests:` commits skip the deploy entirely — they cannot change
what the function serves.

To ship, run **Deploy production** from the Actions tab (or
`gh workflow run deploy-production.yml -f sha=<commit> -f reason='...'`).
Leaving `sha` blank ships the tip of the branch you dispatched from; setting
it pins the deploy to the exact commit whose preview you verified.

### One-time setup

Repository secrets (Settings → Secrets and variables → Actions):

| Secret | Where it comes from |
| --- | --- |
| `VERCEL_TOKEN` | vercel.com → Account Settings → Tokens |
| `WHATSAPP_VERIFY_TOKEN` | the value already in the Vercel project; the health check is gated on it |
| `VERCEL_AUTOMATION_BYPASS_SECRET` | only if Deployment Protection stays on for previews (Vercel → Settings → Deployment Protection → Protection Bypass for Automation) |

Repository **variables**, not secrets — these are identifiers, not
credentials, and GitHub masks secrets as `***` in logs, which hides the one
thing you need to read when a deploy targets the wrong project:

| Variable | Where it comes from |
| --- | --- |
| `VERCEL_ORG_ID` | `.vercel/project.json` after a local `vercel link` |
| `VERCEL_PROJECT_ID` | same file |
| `PRODUCTION_URL` | the stable alias, `https://<project>.vercel.app`, so the production health check hits what Meta's callback points at rather than the fresh deploy's hashed URL |

In the Vercel project, set the six bot environment variables for the
**Preview** environment as well as Production. Without them the preview
deploys fine and the health check fails on `MISSING`, which is the point of
having it.

## Development

Install the commit hook once per clone:

```bash
git config core.hooksPath hooks
```

Commit subjects must read `<tag>: short description of work`, with the tag one
of `docs`, `tests`, `feat`, `fix`, `refactor` — under 72 characters, no full
stop. `hooks/commit-msg` enforces it locally and CI re-runs the same file over
every pushed commit, so `--no-verify` only defers the rejection.

```bash
uv run pytest          # offline — no test touches the network
uv run ruff check .
uv run ruff format .
```

All datetimes are timezone-aware in `Asia/Karachi`.
