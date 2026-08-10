# Comedy Cellar ticket watcher

Polls [the Comedy Cellar NYC lineup](https://www.comedycellar.com/new-york-line-up/)
around the clock and **notifies you the moment shows for September 10, 11 or 12, 2026
are announced** (dates are configurable). Built to run on a VPS via
[Dokploy](https://dokploy.com/) using the included `Dockerfile`.

## How it detects new dates

Two independent strategies run on every check (default: every 5 minutes):

1. **Page scan** — fetches the lineup and reservations pages and looks for the
   target dates in every format the site could plausibly use
   (`2026-09-10`, `9/10/2026`, `September 10`, `Sept 10th`, date-picker
   `value=` attributes, JSON embedded in the page, …). Wrong-year mentions
   ("September 10, 2025") are ignored.
2. **Lineup API probe** — asks the site's own WordPress AJAX endpoint
   (`admin-ajax.php`, the call the date picker makes) for each target date and
   flags the date as *confirmed* if the response contains showtimes.

Either one firing triggers the alert. If **both** strategies stop working
(site down, layout change, bot-blocking), you get a *"checker is BLIND —
check manually"* warning after 2 hours and every 12 hours until it recovers —
so silence can never mean you missed the on-sale.

## How it notifies (the bulletproof part)

- **Every configured channel fires for every alert** — configure as many as you like.
- Failed deliveries go into a **persistent retry queue** (exponential backoff,
  forever) — an alert is never dropped, even if ntfy/Telegram/your network is
  down for an hour.
- When a date is found you get the alert plus **3 repeat reminders 30 minutes
  apart**, in case one push gets swallowed.
- **Daily heartbeat** (9am ET by default) proving it's alive, with the furthest
  date the site currently lists.
- Startup/crash/recovery notices, container `HEALTHCHECK`, and a state file on
  a volume so restarts don't re-alert or lose the queue.

### Channel 1 (default, zero signup): ntfy

1. Install the **ntfy** app ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)
   / [iOS](https://apps.apple.com/us/app/ntfy/id1625396347)) or open
   [ntfy.sh](https://ntfy.sh) in a browser.
2. Subscribe to your topic — default: **`comedycellar-alerts-pt-7g3k1x`**
   (i.e. open <https://ntfy.sh/comedycellar-alerts-pt-7g3k1x>).
3. That's it. Anyone who knows the topic name can see/send messages on the
   public ntfy.sh server, so optionally set `NTFY_TOPIC` to your own random
   string — just subscribe to the same name in the app.

In the ntfy app, give the topic **"instant delivery" / disable battery
optimization** so alerts arrive immediately.

### Optional extra channels

Set any of these env vars and the channel joins the fan-out — see the table
below: Telegram bot, Discord webhook, Slack webhook,
[Pushover](https://pushover.net), plain email (SMTP), or a generic JSON
webhook.

## Deploy on Dokploy

1. Push this repo to GitHub (already done if you're reading it there).
2. Dokploy → **Create Application** → provider **GitHub**, pick this repo/branch.
3. Build type: **Dockerfile** (it's auto-detected at the repo root).
4. *(Recommended)* **Advanced → Volumes / Mounts**: add a **volume mount** with
   mount path `/data` — keeps state across redeploys (no duplicate alerts,
   no lost queued notifications).
5. *(Optional)* **Environment** tab: set `NTFY_TOPIC` and/or other channels.
6. **Deploy.** You should get a *"watcher started"* notification within a
   minute — that alone proves the whole pipeline works.

Verify anytime from Dokploy's container terminal:

```bash
python checker.py --test-notify   # pushes a test message through every channel
python checker.py --once         # one full site check, prints what it saw
```

Or locally with plain Docker:

```bash
docker compose up -d --build
docker compose logs -f
```

## Configuration

Everything is optional — the defaults already watch Sep 10–12, 2026 and push to ntfy.

| Env var | Default | Meaning |
|---|---|---|
| `TARGET_DATES` | `2026-09-10,2026-09-11,2026-09-12` | Comma-separated `YYYY-MM-DD` dates to watch |
| `CHECK_INTERVAL_SECONDS` | `300` | Seconds between site checks (min 60) |
| `NTFY_TOPIC` | `comedycellar-alerts-pt-7g3k1x` | ntfy topic; `off` disables ntfy |
| `NTFY_SERVER` | `https://ntfy.sh` | Self-hosted ntfy if you have one |
| `NTFY_TOKEN` | – | ntfy auth token (protected topics) |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | – | Telegram bot channel |
| `DISCORD_WEBHOOK_URL` | – | Discord webhook channel |
| `SLACK_WEBHOOK_URL` | – | Slack webhook channel |
| `PUSHOVER_TOKEN` / `PUSHOVER_USER` | – | Pushover channel |
| `WEBHOOK_URL` | – | Generic webhook: POSTs `{title, message, level, ts}` JSON |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `SMTP_FROM` / `SMTP_TO` | – | Email channel (Gmail: `smtp.gmail.com` + app password) |
| `ALERT_REPEATS` | `3` | Extra reminders per found date (`0` = just one alert) |
| `ALERT_REPEAT_MINUTES` | `30` | Minutes between reminders |
| `HEARTBEAT_HOUR` | `9` | Local hour for the daily "still alive" message; `-1` disables |
| `FAILURE_ALERT_HOURS` | `2` | Warn after this many hours of failed checks |
| `TZ` | `America/New_York` | Timezone for heartbeat scheduling |
| `STATE_FILE` | `/data/state.json` | Where state lives (mount a volume here) |
| `EXTRA_WATCH_URLS` | – | Additional comma-separated URLs to scan |
| `LOG_LEVEL` | `INFO` | `DEBUG` for verbose logs |

## Development

```bash
pip install -r requirements.txt
python -m unittest test_checker -v   # full test suite, no network needed
DRY_RUN=1 python checker.py --once   # real site check, notifications printed only
```
