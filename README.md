# Comedy Cellar ticket watcher

Polls [the Comedy Cellar NYC lineup](https://www.comedycellar.com/new-york-line-up/)
around the clock and **notifies you the moment showtimes are posted or comedians
are announced** for September 10, 11 or 12, 2026 (dates are configurable). Built
to run on a VPS via [Dokploy](https://dokploy.com/) using the included `Dockerfile`.

## What a date goes through

A date does not simply appear — it moves through stages, and only the last two
are worth waking you up for:

| Stage | What the API returns | Alert |
|---|---|---|
| `not_listed` | empty — beyond the ~4-week window | – |
| `no_lineup` | `<p class="no-shows">No Comedians added yet!</p>` | info only |
| `showtimes` | show times posted, nobody named yet | **push** |
| `lineup` | `<span class="name">…</span>` per comedian | **push, with names** |

Only forward moves alert, and the high-water mark is kept: if the site briefly
serves a worse answer, that neither fires nor re-arms, so recovering can't
double-notify. The push names the comedians and carries per-show booking links
(`reservations-newyork/?showid=…`), which only exist once a lineup is up.

## The thing that makes this hard

The lineup page is **rendered client-side**. Its raw HTML contains no dates at
all, so a plain "download the page and grep for the date" watcher silently never
fires — it fetches HTTP 200 forever and reports itself healthy. This project was
originally built that way and did exactly that for 187 checks.

So the design rule here is: **the watcher must always be able to state the
furthest date the site lists (the "date through"). If it can't, that is an alarm,
not a footnote** — an unknown date-through is precisely the state in which
tickets appear and nobody gets told.

## Detection strategies

Every check runs these, cheapest first, and stops early once dates are found:

1. **Static HTML** — fetches the lineup page, the reservations page and the
   homepage; matches target dates in every format a site plausibly writes
   (`2026-09-10`, `20260910`, `9/10/2026`, `September 10`, `Sept 10th`,
   `value="…"` picker attributes, embedded JSON). Wrong-year matches like
   "September 10, 2025" are excluded. On this site it currently finds nothing —
   kept because it costs one request and would catch a prose announcement.
2. **The site's own lineup API** — the endpoint the date picker calls, captured
   from a real browser session:

   ```
   POST https://www.comedycellar.com/lineup/api/
   action=cc_get_shows&json={"date":"2026-09-10","venue":"newyork","type":"lineup"}
   → {"show":{"html":"<…6:00 pm show - …>"}}
   ```

   It accepts ISO dates and **echoes back the date it answered about**, both as
   `"date":"2026-09-10"` and in prose (`"Thursday September 10, 2026"`). Every
   answer must name the date that was asked for, or it is discarded — a stale or
   ignored date parameter can therefore never produce a false "lineup is up".
   Responses that don't identify their date at all are not trusted either.
   This is the strategy that decides the per-date stage above.
3. **Headless Chromium** — renders the page like a real browser, then harvests
   dates from the live DOM *and* from every same-origin XHR payload. This is
   what establishes the date-through on this site, and what discovered the API
   above. Runs only when no authoritative source produced a date.
4. **WordPress REST** (`USE_REST=1`, off by default) — `/wp-json/` probes for
   The Events Calendar and custom post types. All four 404 here; kept for the
   day the site changes.

## How you get told

- **Every configured channel fires for every alert.** ntfy by default; Telegram,
  Discord, Slack, Pushover, email and generic webhook are one env var each.
- Failed deliveries go to a **persistent retry queue** on the `/data` volume
  (exponential backoff, retried indefinitely) — an alert is never dropped.
- A found date alerts once, then **3 reminders 30 minutes apart**.
- **Alarms for silent failure**, the whole point of the design:
  | Condition | Alert |
  |---|---|
  | No strategy produced *any* date for 1h | "CANNOT read any dates" — treat as broken |
  | An answer describes the wrong date | discarded, never alerted on |
  | Date-through frozen ≥3 days (site rolls daily) | "date-through is STUCK" |
  | Every strategy unreachable for 2h | "checker is BLIND" |
  | Recovery from any of the above | recovery notice |
- **Daily heartbeat** (9am ET) reporting the date-through, which strategy
  produced it, which strategies are alive, and **the current stage of each target
  date** (with the announced comedians once they exist).
- **The startup notification includes the date-through**, so a redeploy tells you
  within a minute whether detection actually works.

### Channel 1 (default, zero signup): ntfy

1. Install the **ntfy** app ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)
   / [iOS](https://apps.apple.com/us/app/ntfy/id1625396347)) or open
   [ntfy.sh](https://ntfy.sh).
2. Subscribe to your topic — default **`comedycellar-alerts-pt-7g3k1x`**.
3. Enable "instant delivery" / disable battery optimisation for that topic.

Anyone who knows the topic name can read it on the public ntfy.sh server, so
consider setting `NTFY_TOPIC` to your own random string.

## Deploy on Dokploy

1. Dokploy → **Create Application** → GitHub → this repo/branch.
2. Build type: **Dockerfile** (auto-detected at the repo root).
3. **Advanced → Volumes**: mount a volume at `/data` so state and the retry
   queue survive redeploys.
4. **Environment**: optionally set `NTFY_TOPIC`, `CHECK_INTERVAL_SECONDS`, etc.
5. **Deploy**, then read the startup push — it states the date-through.

Resources: the container is idle-cheap but launches Chromium each check when the
cheaper strategies find nothing. Budget **~1GB RAM** and ~700MB disk for the
image. Set `USE_BROWSER=0` to disable rendering (only sensible if the site ever
starts serving dates in HTML).

Verify anytime from the container terminal:

```bash
python checker.py --selfcheck     # can this deployment launch Chromium? (run this first)
python checker.py --diagnose      # what every strategy sees, incl. date-through
python checker.py --test-notify   # push a test message through every channel
python checker.py --once          # one full check, then exit
```

## Verified against the live site

Measured on 2026-08-11 by running `--diagnose` from a CI runner (a dev sandbox
often cannot reach the site):

| Strategy | Result |
|---|---|
| static, lineup page | HTTP 200, 83.5 KB, **0 dates** — JS-rendered |
| static, reservations | HTTP 200, 104 KB, **0 dates** |
| static, homepage | 5 dates, furthest 2027-07-29 — unrelated, hence non-authoritative |
| `/wp-json/` probes | 3 × 404, 1 × 200 with no dates |
| headless Chromium | HTTP 200, **29 dates, through 2026-09-07** |
| lineup API, Aug 17 (ISO) | 9 shows, **46 comedians**, per-show `?showid=` links |
| lineup API, Sep 10/11/12 (ISO) | `No Comedians added yet!` — date listed, lineup pending |

An earlier note here claimed the API ignored ISO dates. That was wrong: the
control date used to test it was 3 days out, and lineups are only posted ~1–3
days ahead, so the control legitimately had no lineup and the encoding took the
blame. Driving the real date picker settled it.

So the Cellar lists roughly **four weeks** ahead and rolls the window forward
daily. The date-through is therefore a live canary: it should advance by one day
every day, and a target date is reachable a few days before the show once the
window covers it.

The **Diagnose live site** GitHub Actions workflow runs `--diagnose` against the
real site on demand and daily, which is also an early warning that the site
changed.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `TARGET_DATES` | `2026-09-10,2026-09-11,2026-09-12` | Comma-separated `YYYY-MM-DD` dates to watch |
| `CHECK_INTERVAL_SECONDS` | `300` | Seconds between checks (min 60) |
| `NTFY_TOPIC` | `comedycellar-alerts-pt-7g3k1x` | ntfy topic; `off` disables ntfy |
| `NTFY_SERVER` / `NTFY_TOKEN` | `https://ntfy.sh` / – | Self-hosted ntfy / auth token |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | – | Telegram channel |
| `DISCORD_WEBHOOK_URL` | – | Discord channel |
| `SLACK_WEBHOOK_URL` | – | Slack channel |
| `PUSHOVER_TOKEN` + `PUSHOVER_USER` | – | Pushover channel |
| `WEBHOOK_URL` | – | Generic webhook: POSTs `{title, message, level, ts}` |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `SMTP_FROM` / `SMTP_TO` | – | Email (Gmail: `smtp.gmail.com` + app password) |
| `ALERT_REPEATS` / `ALERT_REPEAT_MINUTES` | `3` / `30` | Reminders per found date |
| `HORIZON_UNKNOWN_HOURS` | `1` | Hours of no-dates-at-all before alarming |
| `HORIZON_STALL_DAYS` | `3` | Days of a frozen date-through before alarming |
| `FAILURE_ALERT_HOURS` | `2` | Hours of total unreachability before alarming |
| `HEARTBEAT_HOUR` | `9` | Local hour for the daily heartbeat; `-1` disables |
| `USE_BROWSER` / `USE_API` | `1` | Toggle the Chromium render / lineup API strategies |
| `USE_REST` | `0` | Enable the `/wp-json/` probes (all 404 today) |
| `CHROMIUM_PATH` | – | Explicit Chromium binary (auto-detected otherwise) |
| `TZ` | `America/New_York` | Timezone for heartbeat scheduling |
| `STATE_FILE` | `/data/state.json` | State location (mount a volume here) |
| `EXTRA_WATCH_URLS` | – | Additional comma-separated URLs to scan |
| `LOG_LEVEL` | `INFO` | `DEBUG` for verbose logs |

## Layout

| File | Role |
|---|---|
| `dateparse.py` | Date matching and harvesting — no network, exhaustively tested |
| `sources.py` | HTTP client and the four detection strategies |
| `checker.py` | Config, state, alerting, date-through guarantees, main loop, CLI |
| `test_checker.py` | 35 tests, including a real Chromium render of a JS-only page |

## Development

```bash
pip install -r requirements.txt && playwright install chromium
python -m unittest test_checker -v      # browser tests skip if no chromium
REQUIRE_BROWSER=1 python -m unittest test_checker -v   # or fail instead of skip
DRY_RUN=1 python checker.py --diagnose  # real site, notifications printed only
```
