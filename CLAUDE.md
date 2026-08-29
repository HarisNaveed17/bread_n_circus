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
```

Commands: `fetch` (scrape enabled sources into the store) → `render` (render
stored events into the `digests` row) → `send` (deliver the stored digest).
`run` does all three. All take `--dry-run` and `--week-of YYYY-MM-DD`
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
  blocks the digest.
- `normalize.py` — sets `series_key` (strips trailing "Session: N" markers) so
  `render.py` can collapse recurring series into one line. `dedupe()` is a
  pass-through until M3.
- `render.py` — pure function, `Event`s → WhatsApp message list. No escaping
  and no `[text](url)`: WhatsApp is not Markdown. See [Delivery](#delivery).
- `store.py` — thin sqlite/libSQL wrapper, upsert-by-id, no ORM.
- `cli.py` — Typer app: `fetch` / `render` / `send` / `run`. `send` prints
  under `--dry-run` and otherwise reports that there is no push channel.
- `notify/base.py` — the `Notifier` seam. `DryRunNotifier` is the only
  implementation; the nudge sender lands in Phase 2.
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
  `price_text` is always `None` for Ticketwala events — unlike Black Hole,
  there's no safe "assume free" default since Ticketwala sells paid tickets.
- **M3 — not started**: real fuzzy dedup/merge. `rapidfuzz` is already a
  dependency, unused until this lands.
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
  runs `render` Saturdays 10:00 Karachi (05:00 UTC), scraping and saving the
  digest into the store. **Sends nothing on purpose** — delivery is
  mid-migration, and the WhatsApp bot reads the digest straight out of Turso,
  so populating that row is the whole job and delivery stays additive. Guards
  on `TURSO_DATABASE_URL` being set, because without it the store silently
  falls back to a runner-local sqlite file and the run goes green having
  persisted nothing. `workflow_dispatch` takes an optional `week_of`, and the
  digest is echoed into the run summary so you can eyeball it without opening
  Turso. Not tied to a numbered milestone.

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

Checkable in seconds — verify rather than trust, this list goes stale.

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

## Working notes

- **Tests never touch the network.** Every scraper test uses a fixture
  captured from the real site/API (`tests/fixtures/`), with the fetch
  function monkeypatched. Keep this invariant — the two `test_cli.py` "zero
  sources" tests pin `pipeline.load_enabled_sources` to `[]` specifically
  because a live source in `sources.yaml` would otherwise make them hit the
  network.
- **The store must speak a dialect both backends accept.**
  `libsql_experimental` is qmark-only — a named `:param` dict raises
  `TypeError: 'dict' object cannot be converted to 'PyTuple'` — and it has no
  `row_factory`, so rows arrive as plain tuples rather than `sqlite3.Row`.
  `store.py` was written sqlite3-first and hit both (fixed in `f8d7e6c`);
  nothing caught it because the Turso path had never once been executed.
  `tests/test_store.py` now runs the whole store against sqlite3 *and* an
  in-memory libSQL connection — put any new query through it, and bind
  positionally.
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
  this reason.
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
