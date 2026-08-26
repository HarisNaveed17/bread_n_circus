# isb-events

A weekly pipeline that scrapes Islamabad event listings, deduplicates them,
renders a digest, and delivers it to Telegram on a GitHub Actions cron. Full
usage/config docs live in `README.md` — this file is project memory and
working notes for future sessions, not a duplicate of it.

## Quickstart

```bash
uv sync
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
- `render.py` — pure function, `Event`s → Telegram MarkdownV2 message list.
- `store.py` — thin sqlite/libSQL wrapper, upsert-by-id, no ORM.
- `cli.py` — Typer app: `fetch` / `render` / `send` / `run`.

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
- **M7 — explicitly GATED, do not build against it yet**: hand-curated
  Instagram organiser bio-link track. Schema stub only, commented out in
  `sources.yaml`.
- **GitHub Actions cron**: `.github/workflows/` is empty. The README
  describes a weekly cron but nothing implements it yet — this isn't tied to
  a numbered milestone, it's just a known gap.

## Working notes

- **Tests never touch the network.** Every scraper test uses a fixture
  captured from the real site/API (`tests/fixtures/`), with the fetch
  function monkeypatched. Keep this invariant — the two `test_cli.py` "zero
  sources" tests pin `pipeline.load_enabled_sources` to `[]` specifically
  because a live source in `sources.yaml` would otherwise make them hit the
  network.
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
