# Runbook

What to run when something stops working, and how to read the answer. Every
command here is read-only unless it says otherwise.

Load the environment first — nearly everything below needs it:

```bash
set -a; . ./.env; set +a
```

`.env` holds the same six variables the Vercel project does. **Nothing here
ever prints a token**; the diagnostics report `set` / `MISSING` only.

---

## First five minutes

Run these three in order. They narrow the problem to a layer before you touch
anything.

```bash
# 1. Can the live deployment reach the database and see a digest?
curl -s "https://isb-events-digest-adeni-chai.vercel.app/api/webhook?health=$WHATSAPP_VERIFY_TOKEN"

# 2. Is the local config complete, and is the token still alive?
uv run python -m bot.selftest

# 3. Is the WABA still subscribed to our app? (the silent killer)
uv run python -m bot.selftest --diagnose
```

A healthy system answers roughly:

```
turso url : libsql://
turso token: set
wa token  : set
wa number : set
app secret: set
digest    : ok — week of 2026-08-31, 1462 chars
subscribers: table present
```

Read `wa token: set` precisely: **set, not valid.** The health endpoint checks
that variables exist, never that Meta accepts them. Step 2 is what tests that.

---

## Symptom: I messaged the bot and nothing came back

The reply path is: Meta routes the inbound message → our function replies via
`POST /messages` → Meta delivers. All three fail differently.

### Is the credential being rejected?

```bash
curl -s "https://graph.facebook.com/v21.0/me?access_token=$WHATSAPP_TOKEN" | python3 -m json.tool
```

`/me` is the cheapest possible call. If it fails, nothing else will work, and
the error code tells you which layer is broken:

| Code | Message | What it means |
| --- | --- | --- |
| — | returns an id | The token is fine. The problem is further down. |
| 190 | `Session has expired` | Token dead. The API Setup page's token lasts 24 hours — a deployment needs a **System User** token from Business settings. |
| 200 | `API access blocked` | **Not a token problem.** Access was revoked above the credential, at the app or the business portfolio. No new token will fix it. |
| 100 | names a parameter | Wrong id (phone number id vs. WABA id vs. phone number). |

For 190 or a scope error, `debug_token` shows expiry and granted scopes:

```bash
curl -s "https://graph.facebook.com/v21.0/debug_token?input_token=$WHATSAPP_TOKEN&access_token=$WHATSAPP_TOKEN" | python3 -m json.tool
```

**Observed 2026-08-30:** every endpoint including `/me` returned code 200
`API access blocked`, immediately after generating a valid System User token.
That is an app- or business-level restriction. Nothing in this repo can lift
it — check, in order:

1. developers.facebook.com/apps → the app → the banner at the top, and Alerts.
2. business.facebook.com → Business Settings → **Security Center**. A
   restricted business portfolio blocks every app it owns.
3. Notifications on the developer account itself — unaccepted platform terms
   or an unverified account block API access wholesale.

### Is the inbound message even reaching us?

The dashboard tests the wrong thing here, which is why this cost hours once.

```bash
uv run python -m bot.selftest --diagnose      # reads /{waba-id}/subscribed_apps
uv run python -m bot.selftest --subscribe     # WRITES: subscribes our app
```

A webhook is **two** independent things and the dashboard only shows one:

1. *App-level config* — callback URL, verify token, subscribed fields. This is
   the Configuration page. Its **Test** button posts straight to the callback,
   bypassing routing entirely, so it passes while real messages go nowhere.
2. *Account-level subscription* — `/{waba-id}/subscribed_apps`. **This list is
   what actually routes inbound messages.** Config without subscription
   delivers nothing.

Watch for a false positive: that list is often non-empty because it holds
`WA DevX Webhook Events 1P App`, Meta's own first-party app used by the
dashboard's testing UI. The account looks subscribed while *our* app is
absent. Look for our app id specifically.

### Is the outbound send being accepted but dropped?

```bash
uv run python -m bot.selftest 923001234567       # WRITES: free-form text
uv run python -m bot.selftest 923001234567 -t    # WRITES: hello_world template
```

**A 200 from the messages endpoint means accepted, not delivered.** Free-form
text is only deliverable inside the 24-hour service window a recipient opens
by messaging the number first; outside it Meta accepts the request and
silently drops the message, and the only signal is a later `failed` status on
the webhook.

The `-t` template is deliverable cold, so the pair isolates the cause:

| free-form | template | Conclusion |
| --- | --- | --- |
| ✗ | ✓ | No open service window. Message the number from that phone, then retry. |
| ✗ | ✗ | Token, phone number id, or the allowlist. |

Common send-time codes: **131030** recipient not on the test number's
allowlist; **131047** the 24-hour window has closed. Trust the message string
over the number — Meta's codes shift.

---

## Symptom: the bot replies, but with an old or missing digest

```bash
curl -s "https://isb-events-digest-adeni-chai.vercel.app/api/webhook?health=$WHATSAPP_VERIFY_TOKEN"
```

- `digest : store reachable, but NO ROWS in digests` → the weekly cron never
  populated the row.
- `digest : UNREACHABLE — ...` → the deployment cannot reach Turso. Check the
  Turso pair in the **Vercel** project, not in `.env`.
- `subscribers: MISSING — no such table` → a migration has not been applied
  remotely. Migrations run only from the pipeline's `Store.open()`; the bot has
  no migration runner, so a new migration is live only after the pipeline next
  connects. Fix it by making that happen:

  ```bash
  gh workflow run weekly-digest.yml        # migrates as a side effect
  uv run python -c "from isb_events.store import Store; Store.open().close()"
  ```

### The weekly cron did not fire

Expected behaviour, not a bug. GitHub's `schedule:` is a best-effort request
to a shared queue with no catch-up for a missed firing. Re-run by hand:

```bash
gh workflow run weekly-digest.yml                      # the coming week
gh workflow run weekly-digest.yml -f week_of=2026-08-31
gh run list --workflow=weekly-digest.yml --limit 5
gh run view --log                                      # the digest is echoed here
```

If it keeps missing, the fix is a trigger on hardware you control (a real
crontab hitting the `workflow_dispatch` REST endpoint), not more workflow
tuning. A late cron means a stale digest for a few hours, not an outage.

---

## Symptom: a push did not produce a preview

```bash
gh run list --limit 5
gh run view --log-failed
```

- **No CI run at all** → the push did not land, or Actions is disabled.
- **The `preview` job says "No deploy"** → working as designed. Only
  `feat:`/`fix:`/`refactor:` tips deploy; `docs:` and `tests:` cannot change
  what the function serves.
- **Health check fails on `MISSING`** → the six bot variables are not set for
  the **Preview** environment in Vercel, only for Production.
- **The deploy step 401s or 302s** → Deployment Protection is on for previews.
  Either turn it off, or add `VERCEL_AUTOMATION_BYPASS_SECRET` as a repo
  secret; curl can send the bypass header.

Nothing deploys from Vercel's side any more — `vercel.json` sets
`git.deploymentEnabled: false`. A missing preview is a *workflow* failure, and
the Vercel dashboard will have nothing to show.

## Symptom: `vercel pull` says "Could not retrieve Project Settings"

```
Error: Could not retrieve Project Settings. To link your Project,
remove the `.vercel` directory and deploy again.
```

**Ignore the advice in that message.** A runner has no `.vercel` directory —
it is gitignored — and deleting the local one changes nothing about CI. The
message is the CLI's catch-all for "I could not establish an account context",
and the real cause is almost always the token.

Establish which layer is broken, in this order:

```bash
T='the token'                                   # not the GitHub secret; the value
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $T" \
  "https://api.vercel.com/v9/projects/$VERCEL_PROJECT_ID?teamId=$VERCEL_ORG_ID"
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $T" \
  "https://api.vercel.com/v2/user"
```

| project | /v2/user | Diagnosis |
| --- | --- | --- |
| 200 | 200 | Token is fine; the problem is elsewhere. |
| 200 | 404 | **Team-scoped token.** See below — this is the common one. |
| 403 | — | The token has no access to that team. |
| 401 | 401 | Bad value: a truncated copy, or a trailing newline from an interactive `gh secret set`. |

**Observed 2026-08-30.** A `vcp_` personal access token created with **Team
scope** is restricted to team-level resources and blocked from `/v2/user` by
design. Every Vercel CLI command preflights against `/v2/user`, so such a
token reads projects perfectly over REST and fails on every single CLI
invocation. Symptoms that mislead: `vercel whoami --token=…` returns
`User not found (404)`, adding `--scope` does not help, and the same commands
work locally because `vercel login` uses a session rather than the token.

Fix: recreate the token at vercel.com/account/tokens with **Full Account**
scope, not Team scope. Team membership still grants access to the team's
projects, and `VERCEL_ORG_ID` selects which one.

Both deploy workflows now run this pair as a preflight, so a future occurrence
fails in the first ten seconds with the scope named.

## Symptom: a config change in Vercel had no effect

Vercel bakes environment variables into a deployment when it is built.
Changing one in the dashboard does **not** touch the running deployment. Ship
a new one:

```bash
gh workflow run deploy-production.yml -f reason="pick up the new token"
gh workflow run deploy-production.yml -f sha=<commit> -f reason="..."
```

## Symptom: Meta reports "verification failed" on the callback URL

Two causes, both configuration rather than code:

1. **Deployment Protection is on.** Every request 302s to `vercel.com/sso-api`
   before reaching the function and Meta reports only a verification failure.
   It has to be off for production: bypass tokens need a custom header Meta
   will not send, and our own signature check is the real boundary.
2. **The URL has a deploy hash in it.** Use the stable alias
   (`isb-events-digest-adeni-chai.vercel.app`), never
   `...-ejkxmrsj7-...` — a hashed URL belongs to one deployment and dies on
   the next.

Verify the handshake yourself:

```bash
curl -s "https://isb-events-digest-adeni-chai.vercel.app/api/webhook?hub.mode=subscribe&hub.verify_token=$WHATSAPP_VERIFY_TOKEN&hub.challenge=12345"
# expect: 12345
```

---

## Things that are not the problem

Time saved by not re-investigating these:

- **theblackhole.pk returning empty HTML.** It rate-limits after ~6–8 requests
  an hour behind a WAF. The scraper degrades to zero events rather than
  crashing. A weekly cron will not trip it.
- **A missed scheduled run.** See above — treat as expected.
- **`price_text` empty on Ticketwala events.** No price field exists anywhere
  in that API. Working as intended.
- **Tests passing while production is broken.** No test touches the network,
  by design. Only the live checks in this file can tell you about Meta,
  Vercel, or Turso.
