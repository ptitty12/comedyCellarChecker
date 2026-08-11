# Comedy Cellar ticket watcher

Polls [the Comedy Cellar NYC lineup](https://www.comedycellar.com/new-york-line-up/)
around the clock and **notifies you the moment shows for September 10, 11 or 12, 2026
are announced** (dates are configurable). Built to run on a VPS via
[Dokploy](https://dokploy.com/) using the included `Dockerfile`.

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

1. **Static HTML** — fetches the lineup, both reservation slugs, and the
   homepage; matches target dates in every format a site plausibly writes
   (`2026-09-10`, `20260910`, `9/10/2026`, `September 10`, `Sept 10th`,
   `value="…"` picker attributes, embedded JSON). Wrong-year matches like
   "September 10, 2025" are excluded.
2. **WordPress / plugin REST** — tries The Events Calendar and custom
   post-type endpoints under `/wp-json/`, which would expose dates server-side.
3. **admin-ajax probes** — asks the theme's lineup endpoint about each target
   date across several plausible action names. A hit requires the response to
   both name the date *and* contain showtimes, so a generic `200` can't be
   mistaken for tickets going live.
4. **Headless Chromium** — renders the page like a real browser, then harvests
   dates from the live DOM *and* from every XHR/JSON payload the page fetched.
   This is the strategy that actually works on this site. It also records the
   endpoints the page really calls, so detection can be made cheaper later.

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
  | Date-through frozen ≥3 days (site rolls daily) | "date-through is STUCK" |
  | Every strategy unreachable for 2h | "checker is BLIND" |
  | Recovery from any of the above | recovery notice |
- **Daily heartbeat** (9am ET) reporting the date-through, which strategy
  produced it, and which strategies are alive.
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
python checker.py --diagnose      # what every strategy sees, incl. date-through
python checker.py --test-notify   # push a test message through every channel
python checker.py --once          # one full check, then exit
```

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
| `USE_BROWSER` / `USE_REST` / `USE_AJAX` | `1` | Toggle individual strategies |
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
