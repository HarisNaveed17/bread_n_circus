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
| `render` | Render stored events into the `digests` row (this week + next) |
| `send`   | Print the stored digest (`--dry-run`); no push channel yet |
| `run`    | `fetch` → `render` → `send` in one shot           |
| `check`  | Read the store back and exit 1 if the data says the pipeline has stopped |

All take `--dry-run` and `--week-of YYYY-MM-DD`. With no `--week-of`, `render`
covers **two** weeks — the one today falls in and the one after it — so that the
bot's "what's on today" stays fresh between runs; `fetch`, `send` and `run` use
the coming Mon–Sun as before. An explicit `--week-of` always means that week
alone.

`check` writes nothing. It reports the age of each digest, when each enabled
source last contributed an event, how many events the bot would serve for the
next seven days, and how long the oldest forwarded listing has been queued,
then fails on: no digest for this week, a digest older than 18h, a source
silent for 36h, zero upcoming events, or a listing queued for over 24h. Every
threshold is a flag (`--max-source-age 12`). Add `--digest` to print the
rendered text too. See [Monitoring](#monitoring).

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

`.github/workflows/weekly-digest.yml` runs `render` **twice a day, at ~11:17
and ~19:17 Karachi** (06:17 and 14:17 UTC), scraping the enabled sources and
saving the digest into the store. It sends nothing — that lands with the
WhatsApp work.

Each run refreshes the current week and the coming one, so today's and
tomorrow's listings are never more than a few hours stale. GitHub's scheduler
is best-effort and drops firings; running twice a day means a missed one costs
hours rather than a week. Re-running is safe — events upsert by id, and
`dedupe` merges a listing that was re-published under a new URL.

## Monitoring

The failures this project has had were all silent: a source answering a rate
limit with an empty 200, a run persisting to a file that died with the runner,
a migration that never reached Turso. Nothing raised and every run was green.
So monitoring here means **reading the data back**, not watching for errors.

| Layer | What it catches | Set-up |
|---|---|---|
| `isb-events check`, last step of every digest run | a source that stopped contributing, an empty upcoming window, a stuck intake queue | nothing — it is in `weekly-digest.yml` |
| `digest-health.yml`, twice daily on its own cron | a digest run that never fired, or fired and wrote nothing | nothing |
| Heartbeat: a URL pinged only after a green check | both crons being dropped, or GitHub Actions being down — nothing inside GitHub can report that | one repo secret, below |
| `?health=` polled from outside | the bot side: Turso unreachable from Vercel, a missing table, an expired WhatsApp token | an uptime monitor with a keyword alert |

GitHub emails you when a scheduled workflow fails — check *Settings →
Notifications → Actions* is on, and note the email goes to whoever last
edited the workflow file. That is the alert for the first two layers.

**The heartbeat** is a dead man's switch. Create a check at
[healthchecks.io](https://healthchecks.io) (free) with a period of 12 hours
and a grace of 8 hours — the cron lands up to five hours late — and store its
ping URL:

```bash
gh secret set HEARTBEAT_URL --body "https://hc-ping.com/<uuid>"
```

Both workflows ping it after a passing check and skip silently if the secret
is unset. If no ping arrives inside period + grace, Healthchecks emails you.
That is the only layer that fires when *nothing* runs.

**The bot** has no cron to watch, so watch its health endpoint instead. Any
uptime monitor (UptimeRobot, Better Stack, Healthchecks' own HTTP checks) can
poll `https://<project>.vercel.app/api/webhook?health=<verify token>` every
hour and alert on the words `EXPIRED`, `REJECTED`, `MISSING` or `UNREACHABLE`
in the body. The token line now asks Meta how long the token has left, so a
temporary token shows as `TEMPORARY … (Nh left)` well before it dies.

Datadog and friends would work, but they solve a different problem: a
dashboard is for a team watching many services in real time. This is one
cron and one function, with one person, and the questions are "did it run"
and "did the numbers move". A failed workflow email and a dead man's switch
answer both for free.

Needs two repo secrets: `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN`. The job
fails fast without them rather than silently writing to a throwaway sqlite
file on the runner. Trigger it by hand from the Actions tab ("Run workflow"),
optionally passing a `week_of` date; the rendered digest is echoed into the
run summary either way.

## WhatsApp bot (Phase 1)

`bot/` answers inbound WhatsApp messages from the stored digest for the week
containing today;
`api/webhook.py` is the Vercel entrypoint, a bare WSGI callable. It never imports `isb_events` — it
reads Turso over the HTTP API with plain `httpx`, because the pipeline's libSQL
driver is a compiled extension and a poor fit for a serverless runtime. The two
sides share a database table, not a process.

What it understands:

| Message | Reply |
|---------|-------|
| the first message from a number, whatever it says | The greeting ("Hello ji! 👀 Welcome to Kya Scene Hai?…") with the three buttons |
| `today`, `tonight` | Just today's events |
| `tomorrow`, `tmrw` | Just tomorrow's |
| `ksh`, `week`, `what's on`, `events`… | The next seven days (the default) |
| a tap on *Today* / *Tomorrow* / *This week* | Same as typing the word |
| anything else | "Didn't catch that", with the buttons |
| `subscribe` / `start` / `join` | Recorded as consent for a future weekly heads-up; the reply says none is sent yet |
| `stop` / `unsubscribe` / `cancel` | Opt out |
| a bare Instagram post link, from a curator | Queued for the pipeline, which fetches the post's caption |

Every listing reply ends with a short message carrying three reply buttons,
*Today*, *Tomorrow* and *This week*. Short replies carry the buttons on
themselves. A tap comes back as an `interactive` message with the button's
title, which is parsed exactly like typed text. Buttons are free inside the
24-hour window like any other reply, and need no template.

Day words are matched as whole words anywhere in the message, so "what's on
tomorrow?" works and an event called "Tomorrowland" does not narrow the digest.
The opt-in/opt-out words must be the *entire* message.

The greeting goes out once per number. The bot knows a number is new because
`record_contact` upserts the `subscribers` row with `RETURNING message_count`,
so the same round trip that logs the contact says whether it is the first
(verified over Turso's HTTP API 2026-09-17). If that write fails the message
is treated as *not* the first, so a database blip costs a greeting rather than
sending a duplicate one.

Day replies are assembled from `digest_events`, a table the pipeline fills on
every render with one row per event — the block text already rendered by
`isb_events/render.py`, plus the day and category to filter on. The bot
concatenates; it owns no formatting rules. **A day view therefore only works
once the pipeline has rendered against that database**; until then the bot says
so and falls back to the week, and `?health=` reports the table as missing.

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
