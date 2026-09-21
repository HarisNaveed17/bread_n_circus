# isb-events

A weekly pipeline that scrapes Islamabad event listings, deduplicates them,
renders a digest, and delivers it on a GitHub Actions cron. Full usage/config
docs live in `README.md` — this file is project memory and working notes for
future sessions, not a duplicate of it.

**Delivery is WhatsApp, pull not push** — see [Delivery](#delivery). Telegram
is gone from the code entirely. What is *not* built is the Phase 2 nudge, so
`send` has no push channel and says so.

## Quickstart

```bash
uv sync --extra pipeline    # bare `uv sync` gets the bot's deps only
uv run isb-events run --dry-run          # end to end, prints, persists nothing
uv run isb-events run --dry-run --week-of 2026-08-24
uv run pytest          # offline — no test touches the network
uv run ruff check . && uv run ruff format .
git config core.hooksPath hooks   # once per clone; enforces the commit format
uv run isb-events check           # read-only: is the live store still being fed?
```

`architecture-v1.md` is the map of the whole system — read it first in a new
session. Its appendix is the 2026-09-17 audit of what could be deleted.

Commands: `fetch` (scrape enabled sources into the store) → `render` (render
stored events into the `digests` row) → `send` (deliver the stored digest).
`run` does all three; `check` reads the store back and fails if the data says
the pipeline has stopped (§ Knowing it is alive). All take `--dry-run` and `--week-of YYYY-MM-DD`
(default: the coming Mon–Sun). Storage is local sqlite by default
(`ISB_DB_PATH`) or Turso/libSQL if `TURSO_DATABASE_URL` is set. All
datetimes are timezone-aware in `Asia/Karachi`.

## Architecture

- `models.py` — `Event` and `DigestWindow` (pydantic, frozen, tz-aware only).
- `sources/base.py` — the `Source` protocol + `@register`/`load_enabled_sources`,
  which reads `sources.yaml` and instantiates whichever registered scrapers are
  `enabled: true`. A slug with no registered scraper is skipped silently.
- `sources/<name>.py` — one scraper per source. Each implements `.fetch(window)`
  and registers itself via `@register("slug")` on import (wired through
  `sources/__init__.py`).
- `pipeline.py` — orchestrates fetch → normalize → dedupe → persist. Each
  source is fetched inside its own try/except so one broken source never
  blocks the digest. **The digest is rendered from the store, not from the
  fetch** (`cli._events_for`) — see [Render from the store](#render-from-the-store).
- `normalize.py` — sets `series_key` (strips trailing "Session: N" markers) so
  `render.py` can collapse recurring series into one line, and tidies `title`
  and `venue` at ingest: de-shouts runs of 2+ all-caps words (a *lone* caps
  word is left alone as an acronym), drops a trailing ", Islamabad", collapses
  a sector restated several ways ("F-8/3 F 8/3 F-8"), and shortens a "TBA -
  to be Disclosed…" venue to "TBA". Every rule was written against a string a
  live source actually produced; `tests/test_normalize.py` names them.
  `series_key` derives from the *cleaned* title, so a caps-typed session still
  groups with its siblings. `dedupe()` merges listings that are the same event
  (M3) — see [Dedup](#dedup-m3).
- `render.py` — pure function, `Event`s → WhatsApp message list. No escaping
  and no `[text](url)`: WhatsApp is not Markdown. See [Delivery](#delivery).
  `event_blocks()` is the same render broken into one dict per event (block
  text + `event_date`/`day_label`/`category`), which is how the bot serves
  "what's on today" without a second copy of the formatting rules.
- `store.py` — thin sqlite/libSQL wrapper, upsert-by-id, no ORM.
- `cli.py` — Typer app: `fetch` / `render` / `send` / `run`. `send` prints
  under `--dry-run` and otherwise reports that there is no push channel.
- `notify/base.py` — the `Notifier` seam. `DryRunNotifier` is the only
  implementation; the nudge sender lands in Phase 2.
- `extract.py` — **one extractor for every intake path.** A `Listing` (text +
  source + optional url/source_ref/posted_at/image) goes in, an `Event` or a
  refusal comes out. Sources differ in how the text is *obtained*, not in what
  must be pulled from it, so the adapters are thin and the prompt, schema, date
  resolution and refusal rules are shared. See
  [Intake extraction](#intake-extraction).
- `sources/instagram.py`, `sources/whatsapp.py` — the two adapters. Neither
  parses an event; they build a `Listing`.
- `linkpage.py` — fetches the page a listing links to, reduced to plain text,
  for the one case `extract` cannot answer from the message: a listing that
  gives a weekday and no date. One GET, capped and never raising. See
  [The second look](#the-second-look-at-a-listings-link).
- `bot/intent.py` — message text → a `Filter` (`today`/`tomorrow`/the week).
  Word-list matching, not a model: three phrases is not an NLP problem and Meta
  retries a webhook that answers slowly. Categories join as another word list
  against another `digest_events` column, which is why `Filter` carries fields
  rather than being an enum.
- `bot/` + `api/webhook.py` — the WhatsApp webhook (Phase 1). Stands apart
  from the package on purpose: it imports no `isb_events` and no libSQL
  driver, reaching Turso over the HTTP API instead. `bot/app.py` holds the
  logic, `api/webhook.py` is a bare WSGI callable (Vercel's runtime serves
  one WSGI/ASGI app per project, named by `[tool.vercel] entrypoint`).
  **This is why `pyproject.toml`'s base dependencies are just `httpx` and
  everything else sits in the `pipeline` extra**: Vercel resolves from
  `pyproject.toml` + `uv.lock` with no way to point it at `requirements.txt`
  instead, so anything in the base set gets shipped into the function.
  Use `uv sync --extra pipeline` for pipeline work.

## Where this is, 2026-09-17

Live and working end to end. Measured, not assumed:

| | |
|---|---|
| Events in the store | 76 — 44 Ticketwala, 31 Black Hole, 1 forwarded |
| Digests rendered | 4 weeks, 44 day-view rows |
| Subscribers | 1 (me). **0 opted in** — nobody has been nudged, nothing sends |
| Curator intake | 5 submitted, 1 became an event, 2 declined, 2 queued |
| Scheduled cron runs | 17 in the last week, none missed, all 2.5-5h late |

What a reader gets today: text the bot, get the next seven days; text `today`
or `tomorrow`, get that day; text anything unrecognised, get a short nudge
toward `this week`. A curator forwards a listing and it appears after the next
pipeline run.

**The thing that is not true yet: nobody uses it.** One subscriber, on a `+1`
sandbox number that can only message allowlisted recipients. Every piece of
machinery below works; none of it has an audience. Weigh new work against that
before building more of it.

Verified against the live API rather than inferred, in case a future session
doubts it: inbound and outbound WhatsApp, Turso from inside Vercel, the
extractor on eight real listings across three models, a forwarded message's
payload shape, Instagram `og:` tags from a GitHub runner, Ticketwala event
pages 403ing from the same runner, and the digest rendering from the store
after a source returns nothing.

## Roadmap

The milestone plan isn't (and wasn't) written up as a separate doc — it lives
as inline `M<n>` comments across `sources.yaml`, `pipeline.py`,
`sources/base.py`, `normalize.py`. This is the current reconstruction of it;
grep `M[0-9]` across the repo before trusting this if it's been a while.

- **M0 — done** (`17dea13`): skeleton. Data model, store, pipeline, render,
  CLI. Zero scrapers registered; `run --dry-run` prints an empty digest
  without crashing.
- **M1 — done** (`d29f8e4`): The Black Hole scraper
  (`isb_events/sources/blackhole.py`), scraping
  `theblackhole.pk/upcoming-events/` — a WP Event Manager site (redirects to
  `site.theblackhole.pk`), parsed with `selectolax`. A missing end time or
  missing price doesn't sink the card: end time just becomes `None`, and
  price defaults to `"Free"` since every event observed there is free.
- **M2 — done** (`a54cb13`): Ticketwala scraper
  (`isb_events/sources/ticketwala.py`). Not an HTML scrape — its homepage
  city/date search calls a plain, unauthenticated JSON endpoint,
  `https://ticketwala.pk/api/public/events/public?type=events|workshops&countryId=167&city=Islamabad&present=true&page=&perPage=`,
  found by watching the network tab while using the site's own search box.
  Plain `httpx` reaches it fine — no Cloudflare/TLS-fingerprint block despite
  the site sitting behind Cloudflare, so `curl_cffi` (still a `pyproject.toml`
  dependency) turned out to be unnecessary for this source. No price field
  exists anywhere in the API (checked list and detail responses), so
  `price_text` is always `None` for Ticketwala events. A page-scraping price
  fetch was built and removed — read
  [Ticketwala prices](#ticketwala-prices--built-then-removed) before building
  it again.
- **M3 — done** (2026-09-02): fuzzy dedup/merge in `normalize.dedupe()`,
  using `rapidfuzz`. Built when the cron went twice-daily, because re-scraping
  the same week repeatedly is what makes a re-published listing land twice.
  See [Dedup](#dedup-m3) for the rule and why it is as strict as it is.
- **M4–M6 — not yet described anywhere in the repo.**
- **M7 — gate LIFTED 2026-08-30.** The condition was a digest going
  scrape → store → WhatsApp for real; it has now done so. Original entry
  follows.
- **M7** (decided 2026-08-26):
  hand-curated Instagram organiser bio-link track. Schema stub only, commented
  out in `sources.yaml`. **v0 ships with Black Hole + Ticketwala only**; the
  gate lifts once a digest goes scrape → store → WhatsApp for real. Curating
  handles is then a `sources.yaml` edit, not a code change.
  Two things to know before building it: (1) it never calls Instagram — the
  handles are curated by hand and the fetch target is the *bio-link
  destination* (linktr.ee etc.), a plain public page. This is deliberate:
  Instagram's Graph API only reaches accounts you own, and scraping Instagram
  directly means login walls, bans, and a ToS violation. (2) `bio_links` is
  its own top-level key, separate from `sources` — it wants **one** scraper
  iterating the list, not one module per organiser. The variable part is data.
- **GitHub Actions cron — done** (`.github/workflows/weekly-digest.yml`):
  runs `render` **twice daily, ~11:17 and ~19:17 Karachi** (06:17 and 14:17
  UTC), scraping and saving the digest into the store. Was Saturdays only
  until 2026-09-02; the move to twice-daily is what makes "what's on today"
  worth asking, and it is what motivated M3 dedup.
  **A bare `render` now covers two weeks, the current one and the coming
  one** — see `cli._windows`. This is load-bearing, not tidiness:
  `DigestWindow.coming_week()` is *next* week from Tuesday onwards, so a daily
  cron rendering only that would refresh a week nobody is in yet and leave
  today's and tomorrow's listings frozen until Monday. `current_week()` was
  added for exactly this.
  Two consequences to keep in mind. The bot no longer serves "the newest
  digest row" — nor a calendar week at all; see [The rolling
  window](#the-rolling-window). And `save_digest` resets `sent_at`
  to NULL on every upsert, so it is now cleared twice a day — **Phase 2's
  nudge must not use `sent_at` as its "already nudged" flag**, or it will
  re-nudge every run. **Sends nothing on purpose** — delivery is
  mid-migration, and the WhatsApp bot reads the digest straight out of Turso,
  so populating that row is the whole job and delivery stays additive. Guards
  on `TURSO_DATABASE_URL` being set, because without it the store silently
  falls back to a runner-local sqlite file and the run goes green having
  persisted nothing. `workflow_dispatch` takes an optional `week_of`, and the
  digest is echoed into the run summary so you can eyeball it without opening
  Turso. Not tied to a numbered milestone.

## CI and deploys

Added 2026-08-30, after the pipeline was proven end to end; **both paths ran
green on 2026-08-30** — a preview deploy from a push to master, and a manual
production deploy that health-checked the alias. Three moving parts, and the
non-obvious one is the third.

- **Commit format.** `<tag>: description`, tag one of
  `docs|tests|feat|fix|refactor`, under 72 chars, no full stop.
  `hooks/commit-msg` is a single Python file that is *also* CI's checker
  (`hooks/commit-msg --range A..B`) — deliberately one file, so the local hook
  and the workflow cannot drift. Not Node commitlint: a `package.json` at the
  root risks Vercel re-detecting this as a Node project, and the rule is a
  regex. History before this point does not comply; CI only checks the commits
  a push introduces.
- **`ci.yml`** — every push and PR: subjects, `ruff check`, `ruff format
  --check`, `pytest`. Then, on pushes whose tip commit is `feat`/`fix`/
  `refactor` only, a Vercel *preview* deploy followed by
  `GET /api/webhook?health=` against the new URL. `docs:`/`tests:` commits
  cannot change what the function serves, so they do not spend a deploy.
- **`deploy-production.yml`** — `workflow_dispatch` only. Optional `sha` input
  pins the deploy to the commit whose preview was verified; it re-runs the
  checks rather than trusting that CI ever saw that sha.

**The thing that makes this work is one line in `vercel.json`:**
`git.deploymentEnabled: false`. Vercel's Git integration would otherwise treat
every push to master as a production deploy, and no amount of workflow
configuration would stop it — the deploy is created by Vercel, not by us. With
it off, Vercel builds nothing on push, and the CLI in Actions does the build
(`vercel pull` → `vercel build` → `vercel deploy --prebuilt`). Corollary: the
Vercel dashboard's "Deployments from Git" goes quiet; a missing preview after
a push is a *workflow* failure, not a Vercel one.

Getting the first run green cost an evening, on one trap worth naming here:
**a Vercel token created with Team scope cannot drive the CLI at all.** It
reads projects fine over REST, so it looks valid, but every CLI command
preflights against `/v2/user`, which team-scoped tokens are blocked from —
surfacing as `Could not retrieve Project Settings … remove the .vercel
directory`, advice that is meaningless on a runner that has no such directory.
The token must be **Full Account** scope. Both workflows now probe
`/v2/user` before touching the CLI; `RUNBOOK.md` has the full symptom table.

Two setup facts that only bite at runtime: the bot's env vars must be set for
the **Preview** environment in Vercel too (otherwise the health check fails on
`MISSING` — which is the check doing its job), and if Deployment Protection is
left on for previews, the smoke test needs
`VERCEL_AUTOMATION_BYPASS_SECRET` as a repo secret. curl can send the bypass
header; Meta cannot, which is why production protection stays off.

## The bot's front door (beta prep, 2026-09-17)

Four changes made before handing the number to beta testers. Each is small;
what matters is what was decided.

- **First contact gets the greeting and nothing else.** `bot/app.py GREETING`
  ("Hello ji! 👀 Welcome to *Kya Scene Hai?*…") goes out as the reply to a
  number's first message, whatever it said, with the three buttons under it.
  "First" comes from `record_contact`, which now upserts with `RETURNING
  message_count` — verified over Turso's HTTP API with a throwaway row. A
  failed write means "not first": a blip costs a greeting, never duplicates
  one. STOP as a first message is still honoured before the greeting; a
  curator's forward or link as a first message is still a submission.
- **Reply buttons instead of the text hint.** `whatsapp.send_buttons` sends an
  `interactive` button message; `BUTTONS` is Today / Tomorrow / This week.
  Every listing reply ends with a short `PICK_BODY` message carrying them,
  because an interactive body is capped at 1024 chars and a day's listings
  are longer. Short replies (greeting, "didn't catch that", "nothing listed")
  carry the buttons on themselves. A tap arrives as `type: interactive` with
  the button title, and `_body_text` hands the title to `intent.parse`, so a
  tap is literally the typed word. `test_the_button_titles_are_words_the_parser_knows`
  pins that: rename a button and it must still parse. `WEEK_HINT` is gone.
- **`ksh` is a week word**, because the greeting tells people to text it.
- **The two false promises are fixed.** `NO_DIGEST_REPLY` no longer says
  Saturday; `OPT_IN_REPLY` no longer promises a weekly message. Consent is
  still recorded so the list exists if a nudge ever ships.
- **A bare Instagram post link from a curator is a submission.** The share
  button sends plain text, not a forward, so it needed its own rule. The bot
  matches the URL with its own copy of the pipeline's regex (it cannot import
  `isb_events`; `test_the_bots_post_pattern_matches_the_pipelines` pins the
  two), stores the URL as a `whatsapp` intake row, and `intake._listing_for`
  routes a single-URL body to `instagram.fetch_listing` — so the adapter
  written 2026-09-14 is finally reachable. An unfetchable post leaves the
  queue as `no_listing`.

**The nudge is not needed for beta.** Pull-only is the design: readers text
first, replies are free, and the buttons keep them tapping inside the window.
What pull-only cannot do is bring back someone who stopped texting. Build the
nudge only if the beta shows people forgetting the number exists.

## Knowing it is alive

Built 2026-09-17. Every failure this project has had was silent and the run
was green, so the monitoring reads the *data* back rather than watching for
errors. `isb_events/check.py` asks four questions of the store and
`isb-events check` exits 1 if any answer is bad:

| Check | Fails when | Why that threshold |
|---|---|---|
| digest age | no row for the week containing today, or `created_at` > 18h old | two renders a day, each up to 5h late: one dropped firing must pass, two must not |
| source silence | an enabled slug's `MAX(events.last_seen)` > 36h old, or absent | `last_seen` bumps on every upsert, so a scrape that keeps returning nothing is the only thing that stops it moving — while render-from-store keeps the digest looking full. This is the Black Hole failure, made visible |
| upcoming | 0 rows in `digest_events` for today..today+6 | the bot would answer "nothing listed" |
| intake | a pending row older than 24h | the drain is not running (`ANTHROPIC_API_KEY`?) |

Intake channels (`whatsapp`) are exempt from the source rule: a forwarded
listing is extracted once and never re-seen.

It runs three ways. As the last step of `weekly-digest.yml`, replacing the
inline summary script (so a bad render fails the run). On its own schedule in
`digest-health.yml`, twice daily, so a digest run that *never fired* still
gets checked. And whichever passes pings `HEARTBEAT_URL` if the secret is set
— a Healthchecks.io dead man's switch, the only layer that fires when nothing
runs at all. GitHub's failed-run email is the alert for the first two.

The bot's `?health=` now asks Meta for the token's lifetime (`bot/whatsapp.py`
`token_status`, moved out of `selftest`), so an uptime monitor polling it sees
`EXPIRED` before a reader sees silence. CI and the production deploy fail on
`EXPIRED`/`REJECTED` in the health body. **Measured 2026-09-17: the live token
is TEMPORARY and expires 2026-10-29 10:53 UTC.** Replace it with a System
User token before then or the bot goes quiet with no deploy having changed.

Verified against the live store the day it was built: `check` passed with both
sources seen 0.0h ago and 18 upcoming events. `tests/test_check.py` reproduces
the empty-200 scenario end to end through the CLI: four green renders, the
event still in the digest, and only `check` failing.

## Render from the store

**Fixed 2026-09-11, after several days of digests silently missing a whole
source.** `cli.render` used to do `render_events(result.events, window)` — the
events *this run's scrape returned*. The `events` table was written on every
run and never read back, so a source that came back empty vanished from that
run's digest entirely, with its events sitting in the store untouched.

It is not hypothetical and it is not rare: theblackhole.pk answers a rate limit
with an **HTTP 200 and an empty body**, which parses to zero events and is not
an error, so `result.failures` stays empty and the run goes green. Black Hole
was absent from six consecutive real digests while seven of its events sat in
the store for that very week. The trigger was the move to a twice-daily cron
rendering two windows — 4 fetches a day where there had been 1 a week.

`Store.events_in_window()` reads the week back, `dedupe` runs over that, and
the render uses it. A failed fetch now means "nothing new", not "nothing".

The tradeoff, accepted deliberately: an event withdrawn at the source lingers
until it ages out of the window. One stale line beats a silently half-empty
digest. `tests/test_cli.py` pins the regression with a source that returns
events once and nothing after.

## Intake: curators forward listings

Built 2026-09-17. A curator texts the bot `/insert <listing text>`; the bot
stores it; the next pipeline run extracts it. Migration `004_intake.sql`.

```
curator ──"/insert …"──> webhook ──> intake (queued)
                            │
                            └── POST workflow_dispatch (skip_fetch=true)
                                                      │
                     pipeline: drain ──> extract ──> events ──> digest
                     (no scrape: `render --no-fetch`)
```

**A curator's forward *is* the submission — no keyword.** WhatsApp gives you
no way to add a prefix to a forwarded message, so `/insert` could never ride
along with the gesture people actually reach for. Confirmed against a real
forward in production, 2026-09-17:

```
bot: shape type=text
     keys=['context', 'from', 'from_user_id', 'id', 'text', 'timestamp', 'type']
     context_keys=['forwarded']  forwarded=True  frequently=None
```

Three things in that payload worth keeping:

- `context` is **absent entirely** on a normal message, so the signal is
  unambiguous in both directions — no need to distinguish false from missing.
- `context_keys` is `['forwarded']` **only**. A plain forward does not leak the
  original sender's number. (A *reply* would: there `context` carries the
  quoted message's id and author.)
- `frequently_forwarded` is **absent, not false** — test truthiness. It is also
  a useful signal in its own right: a heavily-circulated chain message is
  exactly what you would not want ingested.

The forward check runs *before* intent parsing, because a listing that says
"tonight" would otherwise be answered as a day query instead of stored.
`/insert` stays for pasted text.

**The bot no longer answers every message with the digest.** It used to, which
meant a wrong number, a "thanks!", or a forwarded chain letter all got fifteen
events back. `intent.parse` returns `UNKNOWN` for anything that is not a
recognised day or week phrase, and that gets a short "didn't catch that" reply
naming what to text instead. The cost is that a real question the word list
does not cover — "anything free on Friday?" — now gets the fallback rather than
the digest; widening `WEEK_WORDS` is the lever, and Phase 3 Q&A is the real
answer.

**The allowlist is the security boundary, and it fails closed.** `CURATORS` is
a comma-separated list of wa_ids; unset means nobody. Without it the number is
an open pipe into the digest *and* into a model, on a public WhatsApp number. A
non-curator sending `/insert` gets the ordinary digest reply and nothing is
stored — deliberately indistinguishable from any other message, because "you
are not authorised" teaches a stranger that the keyword does something.

**Why the bot triggers a run at all.** GitHub's `schedule:` is 2.5-5 hours late
here (§ Open threads), so a listing forwarded at lunchtime would otherwise
appear near midnight. A `workflow_dispatch` from the bot is the only prompt
trigger, and the bot is the only thing always on. The token is a fine-grained
PAT, this repo, `actions: write`, nothing else, and it lives in Vercel. Unset,
everything no-ops and listings wait for the schedule — intake still works.

**Every listing fires a run, and the run does not scrape** (changed
2026-09-18; it batched at five until then). Batching was the wrong trade: by
the time a fifth listing arrives, the first one's event can be that same
evening. What made batching look necessary was that a run scraped every
source, and theblackhole.pk answers a rate limit with an empty HTTP 200 — the
failure that silently emptied six digests (§ Render from the store).

So the cost was removed instead of the frequency. The dispatch carries
`skip_fetch=true`, and `isb-events render --no-fetch` drains the queue and
re-renders from the store without touching a source at all. Scraping stays on
the schedule, where the rate limit is not a problem; a forward costs one
Actions run and one extraction. Note this only works *because* the digest
renders from the store — a no-fetch run has the full picture already.

Two consequences. `_events_for` takes `fetched=False` on that path and
**re-raises a failed store read instead of falling back to the fetch**: the
fetch is empty by construction, so falling back would overwrite a good digest
with "no events found". And the cooldown is now 5 minutes rather than 15 — it
exists to collapse a burst, not to batch, and a listing it catches is not lost
because the run already in flight drains the whole queue.

**Every row leaves the queue, extracted or not.** A declined listing left
pending would be re-extracted on every run forever — re-paying for the same
refusal. (It also used to hold the pending count above the old batch threshold
so that every later curator message fired a run; the threshold is 1 now, but
the re-payment argument stands on its own.)

Two things this turned up that were not obvious:

- **`events.url` was `NOT NULL`** from `001_init.sql`, and `Event.url` had been
  made optional three days earlier. Two of three real WhatsApp samples have no
  link, so the whole intake path failed at the first insert. SQLite cannot
  `ALTER COLUMN`, so `Store._relax_not_null()` rebuilds the table from
  `PRAGMA table_info` — dynamically, so later-added columns survive without it
  knowing about them — and is guarded so it runs once.
- **The prompt now lives in `isb_events/prompts/extract.md`**, not in a Python
  string. It is the thing most likely to be edited, and a plain-text diff beats
  a wall of escaped continuations. It ships inside the wheel (verified); the
  HTML comment at the top is stripped before the model sees it.

Intake is additive, so a failure there is caught and the digest renders without
it: a missing `ANTHROPIC_API_KEY` means "no new forwarded events this run", not
"no digest". `?health=` reports the curator count, whether dispatch is
configured, and how many listings are pending.

## Intake extraction

Built 2026-09-14, **verified against the live API 2026-09-17** — see
[What the run showed](#what-the-live-run-showed). `extract.Listing` is the
seam: Instagram and WhatsApp adapters build one, `extract()` turns it into an
`Event` or declines. Runs on **Haiku 4.5**, for reasons measured rather than
assumed.

Runs in the pipeline, never the bot. `bot/` reaches Turso over HTTP with
`httpx` alone so Vercel ships no compiled driver into the function; an LLM
client there would break that for nothing. The bot stores raw text, the
pipeline parses it. `anthropic` is in the `pipeline` extra.

**The schema is the PII filter.** Every field is an enum or a constrained
string, and `decline_reason` is an enum rather than free text, both
deliberately. A real forwarded newsletter
carried an IBAN, a bank account title, a stranger's mobile number and the
recipient's name; one of the WhatsApp samples carries a phone number. If there
is nowhere to put a phone number, one cannot reach Turso — and a free-text
"why I declined" would quote it straight back. `tests/test_extract.py` asserts
the exact field set, so widening it is a conscious act.

**`registration_phone` is the one deliberate exception**, added 2026-09-14.
"To register, WhatsApp: 0303 5667670" is the whole call to action for that
event, and a listing nobody can act on is not worth printing. A number an
organiser published *so that people would use it* is not the same as a bank
account that happened to be in the thread — and `_clean_phone` enforces the
difference by shape rather than by trusting the prompt: exactly 11 digits
starting `03`, or the same with `+92` in place of the leading zero. No
brackets, no landlines (you cannot WhatsApp one). That length is exact enough
that an IBAN or an account number structurally cannot fit.

**Refusal is a feature, not a failure.** A caption reading "the first time was
so nice, we had to do it twice / this sunday! / -/600 per person" must produce
nothing. `to_event` re-checks rather than trusting the model: `is_event: true`
with no date still yields None, because `Event.starts_at` is required and a
digest grouped by day cannot hold an undated listing.

What the real samples forced, each from a message that actually arrived:

- **`Event.url` is now optional.** Two of three WhatsApp samples have no link
  at all — "DM us", or a phone number. `render` omits the line rather than
  printing `None`.
- **`Event.source_ref` decides identity when the URL does not.** It defaults to
  the URL, so every existing source keeps the ids it already has. A forwarded
  message hashes its own body: the earlier plan of using the organiser's page
  would have collapsed everything one organiser ever sends into a single row,
  because the store upserts by id. Instagram uses the per-post `og:url`, which
  is unique for the same reason.
- **One message can list several cities.** The film-society sample runs in
  Lahore, Islamabad and Karachi on two dates; the prompt takes the Islamabad
  occurrence only. A message describing two *distinct* Islamabad events would
  still yield one — a known limit, not yet worth a list return type.
- **`starts_at` is the earliest time an attendee is expected.** The same sample
  gives doors 6:00, film 6:30 and doors *closed* 6:25. The headline start sends
  someone to a door that shut five minutes earlier.
- **Prices vary by channel, not just by tier.** "PKR 1,500 online | PKR 2,000
  on-spot" is not a range; the prompt asks for both, briefly.

`source_ref` is a column added outside the `.sql` migrations, in
`Store.ADDED_COLUMNS`. `_migrate()` replays every file on every `open()` and
`ALTER TABLE ... ADD COLUMN` is not idempotent, so the column is added only
when `PRAGMA table_info` says it is missing. Note `.fetchall()` there: a libSQL
cursor is not iterable, which the dual-backend store tests caught.

### What the live run showed

All eight real listings, three models, 2026-09-17. Four things were learned,
and three of them were bugs.

**`thinking: adaptive` is not portable, and switching `MODEL` is not a one-line
change.** It is a hard `400 invalid_request_error` on anything before 4.6 —
Haiku 4.5 and Sonnet 4.5 both refused every call. `_thinking_kwargs` now sends
it only to models that accept it.

**A 4xx used to look exactly like a refusal.** Those eight rejected calls each
printed "declined", which reads as "none of these were events" and sends you
inspecting the listings instead of the request. `extract()` now re-raises 4xx
(except 429) and swallows only transient failures: a malformed request fails
identically for every listing, so failing loudly on the first is strictly
better.

**The prompt did not state the time format, and only the strongest model
guessed it.** The schema comment said `HH:MM, 24-hour`; no rule did. Haiku
returned `"5:00 PM"`, `_starts_at` could not parse it, and three good listings
were dropped as "incomplete". Rule 12 states the format now. **Model choice was
masking a prompt defect** — worth remembering before concluding a small model
is not capable enough.

**Thinking buys nothing here.** Opus 5 with and without produced identical
extractions on all eight, at 1,905 vs 867 output tokens. `USE_THINKING = False`.

Measured cost per 1,000 listings, after those fixes:

| Model | per 1,000 | at 40/mo | Result on the eight |
|---|---|---|---|
| Opus 5 | $14.91 | $7.16/yr | 6 extracted, 2 refused |
| Sonnet 4.5 | $7.27 | $3.49/yr | 7 extracted, 1 refused; put the film in 2025 |
| **Haiku 4.5** | **$2.44** | **$1.17/yr** | identical to Opus, field for field |

Real input is ~2,050 tokens per call — a chars÷4 estimate had said 1,268, so
**that heuristic understated it by 60%**. Use `count_tokens`.

The sample is eight listings. Haiku matching Opus on eight is not a guarantee
it matches on eighty, and the failure that matters is inventing an event rather
than a formatting slip. `MODEL` is one string.

### The second look at a listing's link

Built 2026-09-22, **verified against the live API and the live site the same
day.** A weekly event names its day, not its date — and the message that
carries it is a reminder, not an announcement:

```
Reminder for Today! 🏃‍♂️
IRU Monday Intervals are happening today at the Sports Complex!
Meeting time: 6:20 pm
*IRU Web* https://app.islamabadrunwithus.com/event/1041
```

Refusing that is correct (rule 3 — "today" in a forward is often days old, and
re-dating it puts a past event in front of readers as a future one). Discarding
it is not, because **the link at the bottom knows exactly which Monday it
was.** So a refusal for want of a *when* — `no_date`, `no_time`,
`date_conflict`, or `is_event: true` with no date — is retried **once**, with
the linked page's text appended between `--- Linked page (url) ---` fences.
If the page names no date either, the listing is discarded exactly as before.
Everything else (`not_an_event`, `unclear`) is final: an advert stays an advert
however good its website is.

Four things worth keeping:

- **The IRU event page is a spinner.** It renders nothing server-side and
  fetches the event from a plain, unauthenticated JSON endpoint named in its
  own script — `islamabadrunwithus.com/iru-api/api.php?action=eventInfo&eventId=`
  — which is where `{"when": "Monday 21 September, 6:20 pm"}` lives. Fetching
  the HTML returns a stylesheet, so the listing would be dropped for want of a
  page rather than for want of a date. `linkpage.RESOLVERS` rewrites the URL;
  it is the Ticketwala lesson (§ M2) on one more host. **Add a resolver only
  for a link a curator actually forwarded**, after checking its HTML is empty.
  Generic pages need none of this — `page_text` strips the markup and returns
  the text, and returns None when there is too little of it to hold a date.
- **The page is untrusted bytes fetched from a URL in an untrusted message.**
  Hence the caps (20s, 500KB streamed, 4,000 chars), the fences, and rule 15
  telling the model the page is data and not instructions. `is_fetchable` is a
  shape check against private and loopback addresses — it does not resolve the
  host, and the curator allowlist is still the real boundary.
- **The `Event` is built against the original message, never the page.**
  `_clean_url` therefore still checks `event_url` against what the curator
  forwarded, so a link that appears only on a fetched page cannot become the
  link a reader taps.
- **Rule 14 now asks for `event_url` even on a refusal**, because that is the
  case where the link matters most. Without it the model returns nothing to
  fetch on exactly the listings this was built for; `first_url` is the fallback.

What the live run showed, 2026-09-22, Haiku 4.5 on the real message and the
real endpoint: forwarded on Fri 19 Sep it declines `date_conflict`, fetches,
and comes back with **Mon 2026-09-21 18:20, venue "Outer Track, Sports
Complex", "Free"** — the venue and price coming from the page, which the
message never states. The *same* message forwarded on Tue 22 Sep is still
discarded, because the page's Monday is then yesterday. The three older
WhatsApp samples extract unchanged; the film sample refuses on the old prompt
too, so the new rules did not make the extractor more refusing.

Cost is one extra model call and one GET, only on listings that would have been
thrown away. `tests/conftest.py` patches `linkpage._get` for the whole suite —
a listing that grows a URL must not quietly turn a unit test into an HTTP
request — and it raises a `BaseException`, because `page_text` swallows every
`Exception` by design.

## Dedup (M3)

Built 2026-09-02. `normalize.dedupe()` merges two records only when **all** of
these hold: same calendar day in Karachi, start times within an hour, titles
scoring ≥88 on `rapidfuzz.token_set_ratio`, and — when both sides have one —
venues scoring ≥88 too. The survivor keeps its own `id` and `url` (so ids stay
stable), gains any field the other had and it lacked, takes the union of
`sources`, and takes the *earlier* `starts_at`.

**The asymmetry is the whole design.** A missed duplicate is a scruffy digest;
a wrong merge silently deletes an event nobody can get back. So every rule
errs toward not merging.

Three things worth knowing before loosening any of it:

- **Nothing looks past a single day, and that is deliberate.** Ticketwala
  listed *Kaavish Live* on 11 and 12 September 2026 under two slugs, two
  titles ("Kaavish Live (Jinnah Convention Centre, Islamabad)" and "Kaavish
  Live - Islamabad - 12th September") and one venue. Both were live and
  `status: publish` when checked against the API on 2026-09-02 — **two real
  concerts.** Any rule that merges across days deletes one of them.
  `tests/test_normalize.py` pins this pair.
- **`fuzz.token_set_ratio` needs `processor=utils.default_process`.** Without
  it that pair's *same-night* variant scores 74 rather than 100, because
  "(Jinnah" and "Jinnah" are different tokens — so the threshold never fires
  and dedup silently does nothing. This was the rule's first bug.
- **Run it against the live store before changing a threshold.** At the time
  of writing the 27 stored events contain *zero* true duplicates, so `dedupe`
  is a no-op on real data; its job is the duplicate that frequent scraping
  creates, a listing re-published under a new slug. A threshold change that
  starts merging real events will not show up in the unit tests.

## The rolling window

**A calendar week is the wrong unit for "what's on", and this was shipped wrong
twice before it was fixed** (2026-09-06). Both attempts served a pre-rendered
week from `digests.rendered_text`, and each failed at one end of it:

- *Newest row wins* — from Tuesday, `coming_week()` has already written next
  week's row, so the bot answered with a week that had not started.
- *The week containing today* — the fix for that, which then failed on the
  other side: asked on Sunday 6 Sep it replied with Fri 4 and Sat 5 (both
  already past) plus today, and hid the eight events from Wed 9 onwards
  because they sat in the next week's row.

The default reply is now a **rolling seven days from today**, built from
`digest_events` — the same table, blocks and day labels the `today`/`tomorrow`
filters use. It cannot show a day that has passed and cannot hide one that is
coming, and there is one reply path instead of two. The pipeline renders two
weeks every run, so seven days is always covered.

`rendered_text` is still written, and still what the run summary shows, but the
bot only falls back to it when `digest_events` is unreachable.

**`digest_events` stores rendered block text.** So a change to `render.py` —
a new price line, a wording tweak — does not reach the bot until the pipeline
next runs and rewrites those rows. Deploying the bot is not enough.

## Ticketwala prices — built, then removed

**Decided 2026-09-14: the digest shows no price for Ticketwala events.** Do
not rebuild this. The reasoning, in the order it was learned:

1. **The API has no price.** Checked twice: `entry_fee` and `isFree` are null
   on both list and detail responses, and no "Rs" string appears anywhere in
   the 96-key detail response. Two decoys sit right where a price would be —
   `platformFee` and `paymentProcessingFee` are populated, and are booking
   fees, not the ticket. `priceRange` exists and is `"$$"`.
2. **The prices are in the event page**, inside the Next.js `self.__next_f`
   payload as JSON in an escaped JS string. A scraper for it worked, and took
   the live week from 0 of 8 events priced to 8 of 8.
3. **It does not work from CI.** The event page returns 403 from a GitHub
   runner while the listings API on the same host answers fine — per-resource,
   not per-host. The 403 body is Cloudflare's `Just a moment...` interstitial,
   and **`curl_cffi` does not get past it either**: it wants a browser, not a
   better TLS fingerprint. That closes the obvious fix.
4. **So it only ever worked on a laptop.** A local render produced a richer
   digest than the cron did, which is the worst kind of difference — nothing
   surfaces it.

Deleted 2026-09-14 with 195KB of page fixtures. It was not small: escaped-JSON
extraction, accumulation across one `tickets` array per `eventShowId`,
group-ticket filtering (a "Group of 5" at Rs 4,750 is Rs 950 a head, and
folding it into a range trebles the apparent price), and a circuit breaker for
the 403s.

**The reason it is not missed: the URL is already on the line below.** Anyone
who cares taps through to a booking page more current than a weekly scrape.

A "Check with organiser" placeholder was built alongside it and removed for the
same reason — with prices blocked it landed on every paid event, and a line
that appears on everything tells the reader nothing. A missing price is now
omitted, like a missing venue.

If prices are ever wanted again the shape is a separate local tool writing to
Turso, as for [Instagram automation](#instagram-automation--considered-not-recommended),
not a scrape inside the cron.

## Delivery

**Decided 2026-08-26. Not yet built — no code in the repo reflects this.**

Telegram is banned in Pakistan; reaching it needs a VPN, which kills it as a
delivery channel for a local audience. Email was considered and rejected —
trivial to build, but a weekly digest email gets ignored. **WhatsApp is the
channel**, because it's where the audience already is.

### The model: pull, not push

WhatsApp bills business-initiated messages (approved templates, paid) but
**not** user-initiated ones — a user messaging the bot opens a 24-hour service
window in which replies are free and need no template. So the design inverts
the usual broadcast:

- **The bot is the product.** Someone messages "what's on" → reply with the
  stored digest. Free, no template approval, unlimited.
- **The weekly nudge is one template message** whose only job is to reopen
  that free window: *"Your Islamabad week is ready — 12 events. Reply **what's
  on** to see them."* Don't put the digest in the template — more variables
  means more approval friction, and it's the costlier category.

This keeps the only recurring cost at one message per subscriber per week, and
keeps approval surface at exactly one template.

### Shape

The store is already remote (Turso), so the bot and the pipeline never talk to
each other — they share a table:

```
GitHub Actions (weekly cron)          Webhook (stateless, always on)
  fetch → render → store                 ← inbound WhatsApp message
         ↓                                        ↓
      Turso  ────────────────────────→  read this week's digest row
         ↑                                        ↓
   (already built, M0)                  reply via Cloud API
```

Phase 1 needs no LLM: any inbound message → reply with the stored digest text.
A SELECT and a POST.

### Cloud API, not the Business app

These are different Meta products with confusingly similar names, and a phone
number lives on **exactly one of them, never both**:

- **WhatsApp Business App** — a phone app. No API, no webhooks. Cannot do this.
- **WhatsApp Business Platform / Cloud API** — REST API hosted by Meta. No
  device or SIM in the loop after registration. This is the one.

Number plan: a cheap **local Pakistani SIM** (a US number works and costs the
same — Meta prices by *recipient* country — but a +1 number messaging Pakistani
users reads as spam). Register it **directly to Cloud API in the Meta
dashboard; never install WhatsApp on it.** A number with a live consumer
WhatsApp account has to have that account deleted first, sometimes with a
cooldown. Registration is a one-time SMS/voice code; the SIM can go dormant
immediately after, but keep it from being recycled — re-verification needs it.

Do **not** automate the consumer app with the unofficial WhatsApp Web libraries
(Baileys, whatsapp-web.js). ToS violation, numbers get permanently banned.

### Build order

Meta's dashboard provides a **free test number** (messages only allowlisted
recipients, but real webhooks). Build all of Phase 1 against it — the real
SIM's one-shot registration shouldn't be in play until the thing works.

- **Phase 1** — webhook + dumb "any message → this week's digest". Zero cost,
  no templates. Share the `wa.me` link by hand.
- **Phase 2** — the nudge template, once there's a list worth nudging. This is
  the `Notifier` swap the architecture was built for.
- **Phase 3, optional** — real Q&A ("what's on Friday?", "anything free?") by
  passing the week's structured events as context. The data is already clean
  enough that this is a prompt, not a RAG project.

### Code changes this implies

Smaller than it looks — `notify/base.py` was written for exactly this swap:

1. **`notify/whatsapp.py`** — still to build (Phase 2): a `Notifier` that
   sends the nudge template to `Store.opted_in_subscribers()`. Telegram's
   notifier is deleted, so `_notifier()` currently has nothing to return for
   a real send and exits with an explanation.
2. **`render.py` WhatsApp flavour — done** (`51abfed`). Switched outright
   rather than dual-flavoured, since Telegram was being dropped anyway.
   `escape_md2` is deleted; `[text](url)` is gone too, which the original plan
   missed — WhatsApp has no link syntax and renders it literally, so URLs sit
   on their own line and are auto-linked. `_pack`'s 4096 splitting carried
   over untouched.
3. **The webhook — done** (`021e1ea`): `bot/` plus `api/webhook.py`.

Hosting: **Vercel Python function + Turso's HTTP API** (free tier, stays in
Python, HTTPS from a `git push`). Use Turso over HTTP, *not* the
`libsql-experimental` driver — it's a compiled extension and fights serverless
runtimes; plain `httpx` sidesteps it. Cloudflare Workers is the equivalent
option if TypeScript is ever acceptable. Avoid the no-code platforms (Wati,
Twilio Studio) — $30–50/mo for what a free tier covers here.

### If it scales

Cost is linear and modest; the real gates are Meta's, not money.

- **Rates are set by the recipient's country**, not the sender's — Pakistani
  recipients bill at Pakistan rates regardless of the bot's number.
- **Template category is the main cost lever.** Marketing is the expensive
  tier; utility is cheaper (and free inside an open service window). A digest
  someone opted into can plausibly be phrased as utility rather than
  marketing — the wording drives Meta's classification, so phrase the nudge
  as a requested-update notification, and appeal a marketing classification.
- **Business verification is not a prerequisite. Nothing in this plan needs
  it.** The test number never needs it. A real number on an unverified
  account works too; what verification buys is a higher cap on
  *business-initiated* conversations per rolling 24h (unverified sits in the
  low hundreds; verified tiers up on volume and quality).
  That cap is the one number the pull model was chosen to avoid caring
  about. Replies inside a service window the user opened are not
  business-initiated, so Phase 1 does not touch the cap at all, and Phase 2
  spends exactly one nudge per subscriber per week. Verification only starts
  to matter at more subscribers than this project is likely to have, and it
  is a thing to do *then*, not a gate to clear first.
  Note that opt-in documentation is a separate, policy-level requirement
  that applies regardless of verification — see the quality bullet.
- **Quality rating throttles you.** Mutes, blocks, and reports on the nudge
  drop the rating and cut the tier. A weekly blast is exactly the shape that
  tanks it, which is another argument for the pull model: keep
  business-initiated volume at one message and make it genuinely wanted.
  Documented opt-in is a Meta requirement for business-initiated messages
  anyway.

Don't trust remembered per-message rates — Meta moved from per-conversation to
per-message pricing during 2025 and the rate card shifts. Check the live
pricing page before committing to any number.

## Open threads

Live diagnostics — the commands, their expected output, and what each Graph
error code means — live in `RUNBOOK.md`. Prefer running those over reasoning
about what might be wrong; every entry there is something that actually
happened.


Checkable in seconds — verify rather than trust, this list goes stale.

1. **SETTLED 2026-09-05: the schedule fires, but hours late.** Eight
   consecutive scheduled runs since 2026-09-02, twice daily, none missed — so
   whatever ailed `17 5 * * 6`, the twice-daily crons do not share it. What
   they do share is delay, consistently and by a lot: the 06:17 UTC slot lands
   10:39-11:22 (+4h22 to +5h05) and the 14:17 slot lands 16:49-17:54 (+2h32 to
   +3h37). Asked for 11:17 and 19:17 Karachi, the digest actually refreshes
   around 16:00 and 22:00.
   Do not chase this by shifting the cron earlier — the delay ranges over two
   and a half hours and is not stable enough to compensate for. If wall-clock
   time ever matters, the fix is the external trigger named below. For
   freshness alone it does not matter: twice a day is twice a day.
   Original entry follows.

1. **The schedule has still never fired; manual dispatch works fine.** Two
   `workflow_dispatch` runs on 2026-08-28 both went green end to end against
   real Turso — scrape, store, and a digest read back out (1462 chars, week
   of 2026-08-31). So secrets, database, and the libSQL path are all proven.
   What has never happened is a *scheduled* run: the 2026-08-29 05:00 UTC
   firing simply did not occur, with the workflow active, the repo public,
   and the cron on the default branch. Nothing was misconfigured.

   `schedule:` is not cron(8). It borrows the syntax, but it is a request to
   a shared multi-tenant event producer: GitHub evaluates every repo's
   schedule, enqueues an event, then allocates a runner, and all three stages
   are contended. There is no catch-up for a missed firing and no punctuality
   SLA — the docs promise only that runs "may be delayed during periods of
   high load" and advise avoiding the start of the hour. Hence the move to
   `17 5 * * 6`. Be careful repeating the stronger claim that GitHub *drops*
   these: what was actually observed here is one absent run at 05:00 UTC and
   still absent at 07:10, which does not distinguish dropped from
   indefinitely delayed.

   **Treat a missed week as expected, not as a bug**; the fallback is
   `gh workflow run weekly-digest.yml`. If it keeps missing, the fix is a
   trigger on hardware someone controls (a real crontab calling the
   `workflow_dispatch` REST endpoint), not more workflow tuning. This is low
   stakes while nobody is messaging the bot: a late cron means a stale digest
   for a few hours, not an outage.
2. **Turso is provisioned and proven — nothing open here.** Kept as a record
   of what was settled: the database exists, both secrets are set, and a
   dispatched run scraped, stored and read a digest back out of it (1462
   chars, week of 2026-08-31). The compiled-extension worry is settled too —
   `uv.lock` pins a cp313 `manylinux_2_17_x86_64` wheel for
   `libsql-experimental`, so the runner installs a binary rather than
   building from source. Provisioning steps live in `README.md` § Storage.
3. **Outbound is verified against the live API** (2026-08-30). A free-form
   text reached a real allowlisted phone via `python -m bot.selftest`, so the
   access token, `WHATSAPP_PHONE_NUMBER_ID`, the allowlist and the request
   shape in `send_text` are all confirmed — not doc-derived guesses any more.
   Confirmed the hard way first: the same call returned 200 and delivered
   nothing while no service window was open. **A 200 from the messages
   endpoint means accepted, not delivered**; free-form text outside an open
   24-hour window is dropped silently, with the only signal a later `failed`
   status on the webhook. `bot/selftest.py -t` sends `hello_world` instead,
   which is deliverable cold, to tell the two cases apart.

   **Inbound is verified too, as far as Meta's own calls go** (2026-08-30).
   Deployed to Vercel at `isb-events-digest-adeni-chai.vercel.app`; against
   the live URL the handshake echoes the challenge, a correctly signed POST
   returns 200, and forged, unsigned and wrong-token requests all 403. A
   statuses-only payload returns "no messages", so delivery receipts are
   ignored rather than answered.

   Two things to know about that deploy. **Vercel Deployment Protection
   breaks the webhook**: with Vercel Authentication on, every request 302s to
   `vercel.com/sso-api` before reaching the function, and Meta reports only
   "verification failed". It has to be disabled — bypass tokens need a custom
   header Meta will not send, and our own signature check is the real
   boundary anyway. **Use the alias without the deploy hash**
   (`isb-events-digest-adeni-chai...`, not `...-ejkxmrsj7-...`); the hashed
   URL is unique to one deploy and dies on the next push.

   Still unverified: reading Turso *from Vercel* (no code path exercised has
   needed it yet), and an actual inbound WhatsApp message.

4. **WhatsApp Phase 1 works end to end** (2026-08-30). Texting the test
   number returns the stored digest. Verified live: handshake, real Meta
   signatures, Turso read from inside Vercel (`?health=`), and the reply.
   What remains is Phase 2 (the nudge template and `notify/whatsapp.py`) and
   a real Pakistani SIM — the sandbox number is `+1 555-667-9407`, and a `+1`
   number reads as spam to a Pakistani audience.

   Original entry, for the record:

5. **WhatsApp Phase 1 is built but not deployed.** `render.py` emits
   WhatsApp flavour (`51abfed`) and the webhook exists (`bot/`,
   `api/webhook.py`), tested offline and exercised over real HTTP locally —
   handshake, signature accept/reject, digest reply. What has never happened
   is a real Meta call: no app, no test number, no Vercel deploy, so the
   Cloud API request shapes in `bot/whatsapp.py` are written from the docs
   and unverified against the live API. Env vars and deploy steps are in
   `README.md` § WhatsApp bot. `notify/whatsapp.py` is **not** part of this —
   the nudge template is Phase 2, and nothing sends until then.
6. **M3 dedup is deliberately deferred.** `dedupe()` is still a pass-through.
   Black Hole (free, single venue) and Ticketwala (paid ticketing) barely
   overlap, so v0 likely doesn't need it — let real digests prove it's needed
   rather than building fuzzy matching against a hypothetical. It gets real
   once M7's Instagram organisers land, since those *will* cross-list with
   Ticketwala.

## TBD — investigated 2026-08-31, none of it built

Everything in this section was verified live against the real sites/APIs on
2026-08-31. **Nothing here is implemented**; it is recorded so the research
doesn't have to happen twice. Endpoints rot — re-verify before trusting.

### Source recon: Islamabad has no listings infrastructure

Nine candidates probed. Seven are dead, and the two that are alive are weak.
This is the finding, not a failed search — it is *why* organisers here live on
Instagram, and it is the strongest argument yet for the M7 track.

| Candidate | Verdict |
|---|---|
| `kuchkhaas.org` | Domain gone — 302s to a parked spam domain (`iiwiars.com`) |
| `pnca.gov.pk` | No DNS record at all |
| `tickets.pk`, `pakistanevents.com` | Parked, listed for sale |
| `goethe.de/ins/pk` | 403, blocks non-browser clients |
| Eventbrite Islamabad | Alive, trivially scrapable, **content is junk** |
| Bookme.pk | Alive, real ticketing, **inventory is thin** |

**Eventbrite** is the easiest scrape found anywhere in this project: full
schema.org JSON-LD sits in `window.__SERVER_DATA__.jsonld` on
`eventbrite.com/d/pakistan--islamabad/events/`, no auth, no token. It is also
close to worthless — 10 results, most of them online events leaking through
the geo filter (real titles returned: "Black Girl Book Fair! Spooky Edition",
"Washington Crossing the Delaware", "Devenir un leader exemplaire", three HBR
webinars). Maybe 1–2 genuinely local. Filterable by requiring a non-null
`location.name`, but the yield after filtering is ~1 event/week.

**Bookme.pk** looked promising and isn't. `/islamabad-events` is pure SEO
marketing copy with **zero listings in the HTML** — they load client-side.
`robots.txt` is fully permissive (`Allow: /`). The JS bundle
(`/_nuxt/*.js`) names the endpoints — `/api/v2/events/list`, `/events/home`,
`/events/categories` on `api.bookme.pk` — but they return
`{"message":"Unauthenticated."}`; the frontend sends a 64-char bearer from
`VITE_BOOKME_HEADER_AUTH_TOKEN`, which is readable straight out of the page's
Nuxt runtime config. All moot: `bookme.pk/sitemaps/events-sitemap.xml` lists
**11 events nationwide**, mostly not Islamabad (Malam Jabba, Army Museum
Lahore, two Riyadh attractions, a tourist train). Bookme is a bus/flight/hotel
site where events are a side category of occasional mega-concerts.

### Alliance Française Islamabad — two usable sources, and the API generalises

**The one worth building: WordPress + The Events Calendar has a public REST
API.** `afislamabad.org` runs it, unauthenticated:

```
https://afislamabad.org/wp-json/tribe/events/v1/events?start_date=2026-01-01&per_page=20
→ {"events": [...], "total": 5, "total_pages": 1}
```

This is the Ticketwala pattern again, and **one scraper serves every org on
this stack** — configured by base URL, not a module per venue. Caveats, all
observed:

- **Bare calls return only upcoming events, which was 0.** `?start_date=` is
  required to see anything; without it `total: 0` and the source looks broken.
  35 events exist historically, 5 dated 2026, latest 2026-04-04.
- `venue`, `organizer`, `cost`, `categories` are **empty on every event** —
  AFI never fills them in. Venue has to come from config.
- **`timezone: "UTC+0"` is a lie.** Those are Karachi wall-clock times
  mislabelled. Read them as naive-local and *localise* to `Asia/Karachi` —
  converting shifts everything five hours.
- Titles carry HTML entities (`&#8211;`, `&#038;`) needing unescaping.

**The linktree (M7 shape).** `linktr.ee/afislamabad` **needs `curl_cffi`** —
plain `httpx` gets a flat 403, `impersonate="chrome"` gets 200. This is the
first real use for that dependency, which M2 concluded was unnecessary.
Structure: `props.pageProps.links[]` inside `__NEXT_DATA__` — note the script
tag carries a `crossorigin` attribute, so a regex must allow attributes or it
silently won't match. Tabs are `type: "GROUP"` entries; children attach via
`parent.id` (Events & Culture = `567434667`), *not* via the group's own
`children`, which is always `[]`.

Two things learned from its contents:

- **`metaData.title` carries the destination's OG title**, captured by
  Linktree at link-creation time — e.g. `"EVENT - Conversation with Ali Akbar
  — 31st August 2026"`. That `EVENT - ` prefix is AFI's own marker and the
  only reliable event/not-event discriminator on the page.
- **Don't bother fetching the linked Google Forms.** `forms.gle/...` returns a
  JS redirect shell: no `<title>`, no OG tags, no `FB_PUBLIC_LOAD_DATA_`. One
  of the four is login-walled outright (Linktree stored its title as `"Google
  Forms: Sign-in"`). Linktree already did the fetch; the form adds nothing.

Of the tab's 4 children, **1 is a dated event and 3 are undated recurring
class registrations** — which resolve themselves, since `Event.starts_at` is
required and `render` groups by day, so an undated item cannot be represented.
`dateutil` parses `"31st August 2026"` out of a title correctly (verified).

**The linktree is currently more current than the website** — it carries a
dated event while the tribe API has nothing upcoming. They're complementary,
not either/or.

### Ticketwala prices are recoverable after all — BUILT AND REMOVED

Built 2026-09-05, deleted 2026-09-14 once it turned out to work everywhere
except CI. See [Ticketwala prices](#ticketwala-prices--built-then-removed).
The API reasoning below still holds; note the sketch's `grep 'Rs'` approach is
*worse* than what was built, because it misses pages whose prices never reach
rendered HTML.

The M2 note says no price field exists anywhere in the API. That is correct
about the *API* — re-confirmed: `pricing`, `entry_fee` and `isFree` are all
`null` on both the list endpoint and the detail endpoint
(`/api/public/events/public/{slug}`), and every guessable ticket endpoint
404s or 401s. Ticket ids are visible in `customFields[].eventTicketIds` but
nothing public serves them.

**But the prices are in the event page HTML:**

```
$ curl -s https://ticketwala.pk/event/kaavish-live-islamabad-12th-september-7208 | grep 'Rs'
Rs 3,000   Rs 6,000   Rs 10,000   Rs 12,000   Rs 18,000
```

So `price_text` could be `"Rs 3,000–18,000"` instead of `None`, at one extra
GET per Ticketwala event (~8/week). This is the single highest-value fix to
the *existing* digest: 8 of 11 events currently show no price at all, on a
listing dominated by paid ticketing.

### Intake by forwarding, and why email got deferred

The reachable-organiser problem has no scraping answer, so the plan became
**a person forwards the listing and a model extracts it**. Two channels were
designed; WhatsApp won and email is deferred, not deleted.

**WhatsApp groups cannot be read programmatically.** A Groups API now exists
on the Cloud API but is useless here (per Meta's own docs): it requires
**Official Business Account** status, caps groups at **8 participants**, and
is built for groups you create and manage — there is no path to join an
organiser's existing 200-person community group. Vendors advertising exactly
that (Whapi, Unipile) are unofficial libraries driving a consumer account,
which stays banned for the reasons already in [Delivery](#delivery).

**What works instead is the bot that already exists.** A curator forwards a
group post to the bot's number; the webhook already receives it. The design:

```
curator forwards ──> webhook (Vercel, httpx only)
                       │ allowlist check → store raw text
                       ▼
                     Turso  ──> pipeline reads unprocessed → LLM extract → Events
```

**The bot stores, the pipeline parses.** That split is load-bearing: keeping
extraction out of the function preserves the `httpx`-only dependency
discipline, and it mirrors how `subscribers` already works (bot is the sole
writer, pipeline the sole reader). One intake table with a `channel` column
keeps email as a future *writer* rather than a second pipeline.

Known gaps to handle when this is built:

- **`bot/app.py` has no route for it.** Anything that isn't STOP/SUBSCRIBE
  falls through to `_send_digest`, so today a forwarded listing gets the whole
  digest back and is dropped on the floor.
- **The migration gotcha applies at full force** — see [Working
  notes](#working-notes). `002_subscribers.sql` already caused a silent `no
  such table` traceback for exactly this reason.
- **Curator allowlist is not optional.** Without it the number is a public
  injection endpoint, and the bodies are untrusted input to a model.
- **Most WhatsApp event posts are flyer images, not text.** Extractable via
  vision, but Meta's media download URLs are short-lived and the pipeline runs
  weekly — the bot would have to download bytes at receipt rather than store a
  media id for later.

**Email (deferred).** Would be IMAP polled from the Actions cron, *not* an
inbound webhook — Vercel serves one WSGI app per project, so a second endpoint
means path-dispatching inside `api/webhook.py` or a second project, and it
drags parsing into the function. `imaplib`/`email` are stdlib, so it adds no
dependency. Gmail app passwords still work in 2026 provided 2-Step
Verification is on (basic auth for IMAP died March 2025). The tradeoff that
decided it: **email is passive and forwarding is active** — a subscription
keeps working unattended, whereas forwarding needs a human to notice every
event forever. Deferred because few Islamabad organisers run newsletters.

### What these break in the current data model

Four things, all cheap now and expensive later:

1. **`Event.id` collides.** It is `sha256(primary_source + "\n" + url)` and
   nothing else. The plan to use *the organiser's Instagram page* as the URL
   for forwarded events means **every event from one organiser hashes to the
   same id**, and the store upserts by id — so an organiser would never have
   more than one event in the digest. Fix without breaking existing rows: add
   a `source_ref` field defaulting to `url` and hash *that*; existing sources
   set nothing and keep their ids. Changing the hash formula directly would
   orphan every stored id and duplicate the whole events table.
2. **`Event.url` is required and `render` always prints it.** Forwarded and
   newsletter events often have no per-event link. Organiser Instagram page is
   the intended fallback (see 1).
3. **All-day and multi-time events render wrong.** `render` always emits a
   `🕒` line, so a tribe `all_day` event prints "12am". Worse, a real
   newsletter gave four times — doors open 18:00, doors close 18:25, film
   starts 18:30 — and showing the headline start would send readers to a door
   that shut five minutes earlier. **`starts_at` must be the earliest time the
   attendee is expected**, not the advertised start.
4. **`raw_json` becomes a liability for forwarded/email content.** A real
   sample newsletter contained an IBAN, a bank account title, a third party's
   mobile number, and the recipient's name. Storing raw bodies persists all of
   that into Turso. Note the mitigation is structural: a **strict
   structured-output schema is itself the PII filter** — if the model can only
   emit `title`/`date`/`start_time`/`venue`/`price_text`/`category`, an IBAN
   has nowhere to go.

### Instagram automation — considered, not recommended

Playwright driving a logged-in personal account, screenshotting event posts,
extracting via vision. Technically fine; the objection is not technical.
Automated access violates Instagram's ToS and Meta's detection is good, so the
realistic outcome is an action-block or permanent disable. The specific reason
that is expensive *here*: **the WhatsApp Cloud API app lives on a Meta
developer account**, and if it is linked to the same identity in Accounts
Center, enforcement can reach the project's only delivery channel. Trading the
distribution path for a few listings is a bad trade.

If it is ever built: run it locally (never on a runner — datacenter IP, and it
would put credentials in repo secrets), persist a `storage_state` file so no
password is ever handled in code, keep it headed and weekly, and keep it a
**separate local tool that writes to Turso** so the pipeline and bot never
know Instagram exists.

**The cheaper alternative that was recommended instead:** ask ~15 organisers
directly to send their listings, using the WhatsApp number that already
exists. Zero account risk, and organisers have a real incentive since the
digest is free distribution. It also gets listings *before* they are public.

### Suggested order when this resumes

The vision/text extractor is needed by every intake path (forwarded flyers,
forwarded text, newsletters), so it is the piece to build first and the one
that can't be wasted. Ticketwala prices are the highest-value fix to what
already ships. The tribe API scraper is the best generic source. Instagram
automation stays the fallback.


## Working notes

- **Tests never touch the network.** Every scraper test uses a fixture
  captured from the real site/API (`tests/fixtures/`), with the fetch
  function monkeypatched. Keep this invariant — the two `test_cli.py` "zero
  sources" tests pin `pipeline.load_enabled_sources` to `[]` specifically
  because a live source in `sources.yaml` would otherwise make them hit the
  network.
  `tests/conftest.py` makes it mechanical rather than remembered: an autouse
  fixture clears `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `ANTHROPIC_API_KEY`
  and `GITHUB_DISPATCH_TOKEN` for every test, so no test can reach a real
  service even with `.env` sourced, and no individual test has to remember to.
- **`ISB_DB_PATH` beats `TURSO_DATABASE_URL`, and that is deliberate**
  (changed 2026-09-17). It used to be the other way round, which meant a
  deliberate `ISB_DB_PATH=/tmp/scratch.db` was silently ignored the moment
  `.env` had been sourced — and `.env` gets sourced for nearly every command
  here. **A run intended for a throwaway file went to the production database
  instead and wrote real rows to it.** Nothing was lost, but nothing warned
  either.
  The narrower, deliberately-set variable wins now: nobody types a database
  path by accident, while `TURSO_DATABASE_URL` arrives ambiently from `.env`
  or repo secrets. Setting both logs a warning naming which one won. The cron
  sets only the URL, and `weekly-digest.yml` now fails fast if `ISB_DB_PATH`
  is set on a runner — there, a local file dies with the job and the run goes
  green having persisted nothing.
- **The store must speak a dialect both backends accept.**
  `libsql_experimental` is qmark-only — a named `:param` dict raises
  `TypeError: 'dict' object cannot be converted to 'PyTuple'` — and it has no
  `row_factory`, so rows arrive as plain tuples rather than `sqlite3.Row`.
  `store.py` was written sqlite3-first and hit both (fixed in `f8d7e6c`);
  nothing caught it because the Turso path had never once been executed.
  `tests/test_store.py` now runs the whole store against sqlite3 *and* an
  in-memory libSQL connection — put any new query through it, and bind
  positionally.
- **Day filtering is a WHERE clause, not a second renderer.** The bot cannot
  import `isb_events` (`vercel.json` excludes it from the function bundle), so
  the obvious way to answer "what's on tomorrow" — re-render a filtered list on
  the bot side — would fork the formatting rules. Instead the pipeline writes
  `digest_events` (migration `003`): one row per event, holding the block
  `render.py` already produced plus `event_date`, `day_label` and `category`.
  The bot selects, concatenates, and heads the message with the stored
  `day_label`; it never formats a date or an event. Adding a category filter is
  one more `AND` and one more word list in `bot/intent.py`.
  Two decisions worth keeping: the day query keys on `event_date` alone and
  never joins through `digests`, because from Saturday's cron onwards the newest
  digest row is *next* week and "what's on today" would come back empty; and
  `event_blocks` leaves recurring series expanded and ignores `MAX_EVENTS`,
  both of which are weekly-message concerns that would silently empty a day.
- **Migrations only run from the pipeline; the bot never applies them.**
  `Store._migrate()` replays `migrations/*.sql` on every `Store.open()`, but
  the bot reaches Turso over HTTP and has no migration runner. So a new
  migration is live only after the pipeline next connects to that database —
  and until then the bot hits `no such table` at runtime. This actually
  happened with `002_subscribers.sql` (2026-08-30): the table did not exist
  remotely until a `Store.open()` was run by hand, and the only symptom was a
  logged traceback from `record_contact`. **After adding a migration, run the
  pipeline against Turso before deploying a bot that depends on it.** The
  webhook's `?health=` now reports a missing `subscribers` table for exactly
  this reason, and a missing `digest_events` (`003`) alongside it — until that
  migration lands remotely, "what's on today" falls back to the whole week.
  A second constraint follows from `_migrate()` replaying *every* file on
  *every* `Store.open()`: each statement has to be idempotent. `CREATE TABLE IF
  NOT EXISTS` is; `ALTER TABLE … ADD COLUMN` is not, and would raise "duplicate
  column name" on the second startup. That is why `003` is a new table rather
  than a column on `digests`.
- **A WhatsApp webhook is two independent things, and the dashboard only
  shows one of them.** (1) *App-level config*: callback URL, verify token, and
  which fields the app wants — this is the Configuration page. (2)
  *Account-level subscription*: the WABA keeps a list of apps subscribed to
  it, at `/{waba-id}/subscribed_apps`, and **that list is what actually routes
  inbound messages**. Config without subscription delivers nothing.
  This cost hours on 2026-08-30. Everything the dashboard offers tests (1):
  verification is a direct GET to the URL, and the *Test* button posts
  straight to the callback, bypassing routing entirely — both pass while real
  messages go nowhere. Worse, `subscribed_apps` was not empty: it held
  `WA DevX Webhook Events 1P App`, Meta's own first-party app used by the
  dashboard's testing UI, so the account looked subscribed while our app was
  absent. `bot/selftest.py --diagnose` reads that list and `--subscribe`
  POSTs to it; the onboarding flow usually does this for you, which is why
  it is both easy to miss and invisible when it fails.
- **theblackhole.pk rate-limits.** It sits on Bluehost shared hosting behind
  what looks like a WAF — after ~6-8 requests within an hour during manual
  testing, it started returning HTTP 200 with an empty body (and eventually
  406 on other paths), independent of user-agent, from both `curl` and
  `httpx`. The scraper already handles this gracefully (empty HTML → zero
  parsed events, not a crash). A real weekly cron run is very unlikely to
  trip this — don't "fix" it defensively without a real signal it's a
  problem in production.
- When adding a new scraper: check for a real backend JSON API before
  reaching for HTML parsing or TLS-impersonation tricks — Ticketwala looked
  like it would need `curl_cffi` and didn't. `claude-in-chrome`'s network
  tab is the fastest way to check.
