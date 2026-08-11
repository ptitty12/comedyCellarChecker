#!/usr/bin/env python3
"""Comedy Cellar ticket watcher.

Polls comedycellar.com and notifies the moment shows for the target dates
(default: Sep 10/11/12, 2026) become bookable.

Detection is layered (see sources.py): static HTML, WordPress REST, admin-ajax,
and headless Chromium — needed because the lineup is rendered client-side, so raw
HTML contains no dates at all.

The invariant this service is built around: it must always know the furthest date
the site lists ("date through"). If it can't work that out, that is an alarm in
its own right, because it means the watcher has gone blind without failing.
"""

import argparse
import json
import logging
import os
import random
import signal
import smtplib
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import requests

import sources
from dateparse import (clean_html, extract_all_dates, horizon_of,  # noqa: F401
                       patterns_for_date, text_mentions_date)
from sources import AJAX_URL, LINEUP_URL, RESERVATIONS_URL, HttpClient, discover

log = logging.getLogger("cellar")


def _env(name, default):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


@dataclass
class Config:
    targets: list = field(default_factory=list)
    watch_urls: list = field(default_factory=lambda: list(sources.DEFAULT_STATIC_URLS))
    check_interval: int = 300
    tick_seconds: int = 25
    state_file: str = "/data/state.json"
    tz: str = "America/New_York"

    alert_repeats: int = 3
    alert_repeat_minutes: int = 30
    failure_alert_hours: float = 2.0
    failure_realert_hours: float = 12.0
    horizon_unknown_hours: float = 1.0
    horizon_stall_days: float = 3.0
    heartbeat_hour: int = 9
    startup_notify: bool = True

    use_browser: bool = True
    use_rest: bool = True
    use_ajax: bool = True

    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_token: str = ""
    telegram_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook: str = ""
    slack_webhook: str = ""
    pushover_token: str = ""
    pushover_user: str = ""
    webhook_url: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_from: str = ""
    smtp_to: str = ""
    dry_run: bool = False


def load_config() -> Config:
    cfg = Config()
    for part in _env("TARGET_DATES", "2026-09-10,2026-09-11,2026-09-12").split(","):
        if part.strip():
            cfg.targets.append(datetime.strptime(part.strip(), "%Y-%m-%d").date())
    if not cfg.targets:
        raise SystemExit("TARGET_DATES is empty")

    for u in _env("EXTRA_WATCH_URLS", "").split(","):
        if u.strip():
            cfg.watch_urls.append(u.strip())

    cfg.check_interval = max(60, int(_env("CHECK_INTERVAL_SECONDS", "300")))
    cfg.state_file = _env("STATE_FILE", cfg.state_file)
    cfg.tz = _env("TZ", cfg.tz)
    cfg.alert_repeats = int(_env("ALERT_REPEATS", "3"))
    cfg.alert_repeat_minutes = int(_env("ALERT_REPEAT_MINUTES", "30"))
    cfg.failure_alert_hours = float(_env("FAILURE_ALERT_HOURS", "2"))
    cfg.failure_realert_hours = float(_env("FAILURE_REALERT_HOURS", "12"))
    cfg.horizon_unknown_hours = float(_env("HORIZON_UNKNOWN_HOURS", "1"))
    cfg.horizon_stall_days = float(_env("HORIZON_STALL_DAYS", "3"))
    cfg.heartbeat_hour = int(_env("HEARTBEAT_HOUR", "9"))
    cfg.startup_notify = _env("STARTUP_NOTIFY", "1") not in ("0", "false", "no")
    cfg.use_browser = _env("USE_BROWSER", "1") not in ("0", "false", "no")
    cfg.use_rest = _env("USE_REST", "1") not in ("0", "false", "no")
    cfg.use_ajax = _env("USE_AJAX", "1") not in ("0", "false", "no")

    cfg.ntfy_server = _env("NTFY_SERVER", cfg.ntfy_server).rstrip("/")
    cfg.ntfy_topic = _env("NTFY_TOPIC", "comedycellar-alerts-pt-7g3k1x")
    if cfg.ntfy_topic.lower() in ("off", "none", "disabled"):
        cfg.ntfy_topic = ""
    cfg.ntfy_token = _env("NTFY_TOKEN", "")
    cfg.telegram_token = _env("TELEGRAM_BOT_TOKEN", "")
    cfg.telegram_chat_id = _env("TELEGRAM_CHAT_ID", "")
    cfg.discord_webhook = _env("DISCORD_WEBHOOK_URL", "")
    cfg.slack_webhook = _env("SLACK_WEBHOOK_URL", "")
    cfg.pushover_token = _env("PUSHOVER_TOKEN", "")
    cfg.pushover_user = _env("PUSHOVER_USER", "")
    cfg.webhook_url = _env("WEBHOOK_URL", "")
    cfg.smtp_host = _env("SMTP_HOST", "")
    cfg.smtp_port = int(_env("SMTP_PORT", "587"))
    cfg.smtp_user = _env("SMTP_USER", "")
    cfg.smtp_pass = _env("SMTP_PASS", "")
    cfg.smtp_from = _env("SMTP_FROM", cfg.smtp_user)
    cfg.smtp_to = _env("SMTP_TO", "")
    cfg.dry_run = _env("DRY_RUN", "0") in ("1", "true", "yes")
    return cfg


# --------------------------------------------------------------------------
# Checking
# --------------------------------------------------------------------------

@dataclass
class CheckResult:
    found: dict = field(default_factory=dict)
    all_dates: set = field(default_factory=set)
    horizon: object = None
    horizon_source: str = ""
    sightings: list = field(default_factory=list)

    @property
    def ok(self):
        return any(s.ok for s in self.sightings)

    @property
    def errors(self):
        return [s.detail for s in self.sightings if not s.ok]

    @property
    def working(self):
        return [s.strategy for s in self.sightings if s.ok and s.dates]

    @property
    def xhr_urls_sample(self):
        """Endpoints the rendered page actually called — recorded so a future
        version can poll them directly instead of launching a browser."""
        out = []
        for s in self.sightings:
            out += s.xhr_urls
        return out[:20]

    def add(self, d, evidence):
        self.found.setdefault(d, []).append(evidence)


def perform_check(cfg: Config, http, force_browser=False) -> CheckResult:
    today = datetime.now(ZoneInfo(cfg.tz)).date()
    sightings, dates, hits = discover(
        http, cfg.targets, today,
        static_urls=cfg.watch_urls,
        use_browser=cfg.use_browser,
        use_rest=cfg.use_rest,
        use_ajax=cfg.use_ajax,
    )
    if force_browser and not any(s.strategy == "browser" for s in sightings):
        extra = sources.from_browser(LINEUP_URL, cfg.targets, today)
        sightings += extra
        for s in extra:
            dates |= s.dates
            for d, ev in s.target_hits.items():
                hits.setdefault(d, []).append(ev)

    res = CheckResult(all_dates=dates, sightings=sightings)
    for d, evidence in hits.items():
        for ev in evidence:
            res.add(d, ev)
    res.horizon = horizon_of(dates, today)
    if res.horizon:
        best = max((s for s in sightings if res.horizon in s.dates),
                   key=lambda s: len(s.dates), default=None)
        res.horizon_source = best.strategy if best else "?"
    return res


# --------------------------------------------------------------------------
# Notification channels
# --------------------------------------------------------------------------

PRIORITY = {"alert": ("urgent", 1), "warn": ("high", 0), "info": ("default", -1)}


def send_ntfy(cfg, title, body, level):
    headers = {
        "Title": title.encode("ascii", "ignore").decode(),
        "Priority": PRIORITY[level][0],
        "Tags": "rotating_light,tickets" if level == "alert" else "robot",
    }
    if cfg.ntfy_token:
        headers["Authorization"] = f"Bearer {cfg.ntfy_token}"
    r = requests.post(f"{cfg.ntfy_server}/{cfg.ntfy_topic}",
                      data=body.encode(), headers=headers, timeout=15)
    return r.status_code == 200


def send_telegram(cfg, title, body, level):
    r = requests.post(
        f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage",
        json={"chat_id": cfg.telegram_chat_id, "text": f"{title}\n\n{body}"}, timeout=15)
    return r.status_code == 200 and r.json().get("ok") is True


def send_discord(cfg, title, body, level):
    r = requests.post(cfg.discord_webhook,
                      json={"content": f"**{title}**\n{body}"[:1990]}, timeout=15)
    return r.status_code in (200, 204)


def send_slack(cfg, title, body, level):
    r = requests.post(cfg.slack_webhook, json={"text": f"*{title}*\n{body}"}, timeout=15)
    return r.status_code == 200


def send_pushover(cfg, title, body, level):
    r = requests.post("https://api.pushover.net/1/messages.json", data={
        "token": cfg.pushover_token, "user": cfg.pushover_user,
        "title": title, "message": body, "priority": PRIORITY[level][1]}, timeout=15)
    return r.status_code == 200 and r.json().get("status") == 1


def send_webhook(cfg, title, body, level):
    r = requests.post(cfg.webhook_url, json={
        "source": "comedy-cellar-checker", "level": level,
        "title": title, "message": body, "ts": time.time()}, timeout=15)
    return 200 <= r.status_code < 300


def send_email(cfg, title, body, level):
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = title, cfg.smtp_from, cfg.smtp_to
    msg.set_content(body)
    if cfg.smtp_port == 465:
        server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30)
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
        server.starttls()
    with server:
        if cfg.smtp_user:
            server.login(cfg.smtp_user, cfg.smtp_pass)
        server.send_message(msg)
    return True


def enabled_channels(cfg):
    chans = []
    if cfg.ntfy_topic:
        chans.append((f"ntfy({cfg.ntfy_topic})", send_ntfy))
    if cfg.telegram_token and cfg.telegram_chat_id:
        chans.append(("telegram", send_telegram))
    if cfg.discord_webhook:
        chans.append(("discord", send_discord))
    if cfg.slack_webhook:
        chans.append(("slack", send_slack))
    if cfg.pushover_token and cfg.pushover_user:
        chans.append(("pushover", send_pushover))
    if cfg.webhook_url:
        chans.append(("webhook", send_webhook))
    if cfg.smtp_host and cfg.smtp_to:
        chans.append(("email", send_email))
    return chans


def send_all(cfg, title, body, level="alert", attempts=2):
    if cfg.dry_run:
        log.info("[dry-run] %s: %s | %s", level, title, body.replace("\n", " / "))
        return True
    results = {}
    for name, fn in enabled_channels(cfg):
        ok = False
        for attempt in range(1, attempts + 1):
            try:
                ok = fn(cfg, title, body, level)
            except Exception as exc:
                log.warning("channel %s attempt %d error: %s", name, attempt, exc)
                ok = False
            if ok:
                break
            time.sleep(min(2 * attempt, 5))
        results[name] = ok
    log.info("notify [%s] %r -> %s", level, title,
             ", ".join(f"{k}={'ok' if v else 'FAIL'}" for k, v in results.items())
             or "NO CHANNELS")
    return any(results.values())


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def default_state():
    return {
        "found": {},
        "pending": [],
        "fail_since": None,
        "fail_alerted_ts": None,
        "last_ok_check": None,
        "last_heartbeat": None,
        "checks_total": 0,
        "last_loop_ts": None,
        "horizon": None,
        "horizon_source": "",
        "horizon_first_seen_ts": None,
        "horizon_changed_ts": None,
        "horizon_unknown_since": None,
        "horizon_unknown_alerted_ts": None,
        "horizon_stall_alerted_ts": None,
        "xhr_urls": [],
    }


def load_state(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
        st = default_state()
        st.update(data)
        return st
    except FileNotFoundError:
        return default_state()
    except Exception as exc:
        log.warning("state file unreadable (%s); starting fresh", exc)
        return default_state()


def save_state(path, state):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh, indent=1, default=str)
        os.replace(tmp, path)
    except Exception as exc:
        log.error("could not write state file %s: %s", path, exc)


def enqueue(state, title, body, level):
    state["pending"].append({"title": title, "body": body, "level": level,
                             "created": time.time(), "attempts": 0, "next_try": 0})


def flush_pending(cfg, state):
    now, remaining = time.time(), []
    for item in state["pending"]:
        if now < item.get("next_try", 0):
            remaining.append(item)
            continue
        if send_all(cfg, item["title"], item["body"], item["level"]):
            continue
        item["attempts"] += 1
        backoff = min(60 * (2 ** min(item["attempts"], 5)), 1800)
        item["next_try"] = now + backoff
        log.warning("delivery failed (attempt %d), retry in %ds: %r",
                    item["attempts"], backoff, item["title"])
        remaining.append(item)
    state["pending"] = remaining


# --------------------------------------------------------------------------
# Alerting
# --------------------------------------------------------------------------

def fmt_date(d: date) -> str:
    try:
        return d.strftime("%A, %B %-d, %Y")
    except ValueError:
        return d.strftime("%A, %B %d, %Y")


def short(d: date) -> str:
    try:
        return d.strftime("%b %-d")
    except ValueError:
        return d.strftime("%b %d")


def alert_body(dates_with_evidence, cfg, reminder=None):
    lines = ["Comedy Cellar just posted shows for:"]
    for d, evidence in sorted(dates_with_evidence.items()):
        lines.append(f"  - {fmt_date(d)}   ({'; '.join(evidence)})")
    lines += ["", f"Book NOW: {RESERVATIONS_URL}", f"Lineup:   {LINEUP_URL}"]
    if reminder:
        lines.append(f"\n(Reminder {reminder} — set ALERT_REPEATS=0 to silence repeats.)")
    elif cfg.alert_repeats > 0:
        lines.append(f"\n(You'll get {cfg.alert_repeats} reminders, every "
                     f"{cfg.alert_repeat_minutes} min, in case this one is missed.)")
    return "\n".join(lines)


def handle_check_result(cfg, state, res: CheckResult):
    now = time.time()

    new = {}
    for d, evidence in res.found.items():
        key = d.isoformat()
        if key not in state["found"]:
            new[d] = evidence
            state["found"][key] = {"first_seen": now, "alerts_sent": 1,
                                   "last_alert_ts": now}
    if new:
        names = ", ".join(short(d) for d in sorted(new))
        log.info("TARGET DATES FOUND: %s", names)
        enqueue(state, f"Comedy Cellar: {names} tickets are UP!",
                alert_body(new, cfg), "alert")

    for key, info in state["found"].items():
        if info["alerts_sent"] <= cfg.alert_repeats and \
                now - info["last_alert_ts"] >= cfg.alert_repeat_minutes * 60:
            d = date.fromisoformat(key)
            info["alerts_sent"] += 1
            info["last_alert_ts"] = now
            n = info["alerts_sent"] - 1
            enqueue(state, f"Reminder: Comedy Cellar {short(d)} tickets are up",
                    alert_body({d: ["reminder"]}, cfg,
                               reminder=f"{n}/{cfg.alert_repeats}"), "alert")

    if res.ok:
        state["last_ok_check"] = now
        if state.get("fail_alerted_ts"):
            enqueue(state, "Comedy Cellar checker recovered",
                    "The site is reachable again — monitoring resumed.", "info")
        state["fail_since"] = state["fail_alerted_ts"] = None
    else:
        state["fail_since"] = state.get("fail_since") or now
        failing_h = (now - state["fail_since"]) / 3600
        last = state.get("fail_alerted_ts")
        if failing_h >= cfg.failure_alert_hours and (
                not last or now - last >= cfg.failure_realert_hours * 3600):
            state["fail_alerted_ts"] = now
            enqueue(state, "Comedy Cellar checker is BLIND",
                    f"Every strategy has failed for {failing_h:.1f}h.\n"
                    f"{chr(10).join(res.errors[:5])}\n\n"
                    f"CHECK MANUALLY: {LINEUP_URL}", "warn")


def handle_horizon(cfg, state, res: CheckResult):
    """The core guarantee: we must always know the site's 'listing through' date.

    Not knowing it is an alarm — that is precisely the state in which target dates
    could appear and go unnoticed.
    """
    now = time.time()
    if res.xhr_urls_sample:
        state["xhr_urls"] = res.xhr_urls_sample

    if res.horizon is None:
        state["horizon_unknown_since"] = state.get("horizon_unknown_since") or now
        blind_h = (now - state["horizon_unknown_since"]) / 3600
        last = state.get("horizon_unknown_alerted_ts")
        if blind_h >= cfg.horizon_unknown_hours and (
                not last or now - last >= cfg.failure_realert_hours * 3600):
            state["horizon_unknown_alerted_ts"] = now
            enqueue(state, "Comedy Cellar watcher CANNOT read any dates",
                    f"No strategy has produced a single date for {blind_h:.1f}h, so the "
                    f"'date through' is unknown. Target dates could appear WITHOUT an "
                    f"alert — treat this as broken.\n\n"
                    f"Strategy results:\n" +
                    "\n".join(f"  - {s.detail}" for s in res.sightings[:8]) +
                    f"\n\nCHECK MANUALLY: {LINEUP_URL}", "warn")
        return

    prev = state.get("horizon")
    iso = res.horizon.isoformat()
    if prev != iso:
        state["horizon"] = iso
        state["horizon_source"] = res.horizon_source
        state["horizon_changed_ts"] = now
        state["horizon_first_seen_ts"] = state.get("horizon_first_seen_ts") or now
        state["horizon_stall_alerted_ts"] = None
    if state.get("horizon_unknown_since") or state.get("horizon_unknown_alerted_ts"):
        if state.get("horizon_unknown_alerted_ts"):
            enqueue(state, "Comedy Cellar watcher can read dates again",
                    f"Recovered — the site now lists shows through "
                    f"{fmt_date(res.horizon)} (via {res.horizon_source}).", "info")
        state["horizon_unknown_since"] = state["horizon_unknown_alerted_ts"] = None

    # A frozen horizon means the scraper reads *something* but isn't tracking the
    # site any more (cached page, stale endpoint) — also a silent failure.
    changed = state.get("horizon_changed_ts") or now
    stalled_days = (now - changed) / 86400
    last = state.get("horizon_stall_alerted_ts")
    if stalled_days >= cfg.horizon_stall_days and (
            not last or now - last >= cfg.failure_realert_hours * 3600):
        state["horizon_stall_alerted_ts"] = now
        enqueue(state, "Comedy Cellar watcher: date-through is STUCK",
                f"The furthest listed date has been {fmt_date(res.horizon)} for "
                f"{stalled_days:.1f} days without advancing. The Cellar normally "
                f"rolls its window forward daily, so this likely means we're reading "
                f"a stale source.\n\nCHECK MANUALLY: {LINEUP_URL}", "warn")


def status_lines(cfg, state, res: CheckResult):
    lines = []
    if res.horizon:
        lines.append(f"Site lists shows through: {fmt_date(res.horizon)} "
                     f"(via {res.horizon_source})")
    else:
        lines.append("Site lists shows through: UNKNOWN — no dates parsed (BROKEN)")
    ok = [s.strategy for s in res.sightings if s.ok]
    with_dates = res.working
    lines.append(f"Strategies reachable: {', '.join(sorted(set(ok))) or 'none'}; "
                 f"producing dates: {', '.join(sorted(set(with_dates))) or 'NONE'}")
    found = [fmt_date(t) for t in cfg.targets if t.isoformat() in state["found"]]
    missing = [fmt_date(t) for t in cfg.targets if t.isoformat() not in state["found"]]
    if found:
        lines.append("Already found: " + "; ".join(found))
    if missing:
        lines.append("Still waiting for: " + "; ".join(missing))
    else:
        lines.append("All target dates found — you can stop this service.")
    return lines


def maybe_heartbeat(cfg, state, res: CheckResult):
    if cfg.heartbeat_hour < 0:
        return
    now_local = datetime.now(ZoneInfo(cfg.tz))
    today = now_local.date().isoformat()
    if state.get("last_heartbeat") is None:
        state["last_heartbeat"] = today
        return
    if state["last_heartbeat"] == today or now_local.hour < cfg.heartbeat_hour:
        return
    state["last_heartbeat"] = today
    body = [f"Still watching. {state['checks_total']} checks so far."] + \
        status_lines(cfg, state, res)
    enqueue(state, "Comedy Cellar watcher: daily heartbeat", "\n".join(body), "info")


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True
    log.info("signal %s received, shutting down after current tick", signum)


def describe(cfg):
    chans = ", ".join(n for n, _ in enabled_channels(cfg)) or "NONE — configure a channel!"
    return (f"Watching for: {', '.join(t.isoformat() for t in cfg.targets)}\n"
            f"Every {cfg.check_interval}s | channels: {chans}")


def run(cfg: Config, once=False):
    http = HttpClient()
    state = load_state(cfg.state_file)
    log.info("starting\n%s", describe(cfg))

    last_check, announced = 0.0, not (cfg.startup_notify and not once)
    while True:
        loop_start = time.time()
        try:
            if loop_start - last_check >= cfg.check_interval or once:
                last_check = loop_start + random.uniform(-0.1, 0.1) * cfg.check_interval
                res = perform_check(cfg, http)
                state["checks_total"] += 1
                log.info("check #%d: horizon=%s via %s | found=%s | %s",
                         state["checks_total"],
                         res.horizon.isoformat() if res.horizon else "UNKNOWN",
                         res.horizon_source or "-",
                         [d.isoformat() for d in res.found] or "none",
                         "; ".join(s.detail for s in res.sightings))
                handle_check_result(cfg, state, res)
                handle_horizon(cfg, state, res)
                maybe_heartbeat(cfg, state, res)
                if not announced:
                    announced = True
                    enqueue(state, "Comedy Cellar watcher started",
                            describe(cfg) + "\n\n" +
                            "\n".join(status_lines(cfg, state, res)), "info")
                if not res.ok:
                    http.reset()
            flush_pending(cfg, state)
        except Exception:
            log.exception("tick failed (loop continues)")
        state["last_loop_ts"] = time.time()
        save_state(cfg.state_file, state)

        if once:
            return 0 if not state["pending"] else 1
        if _shutdown:
            return 0
        time.sleep(max(1.0, cfg.tick_seconds - (time.time() - loop_start)))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def health(cfg) -> int:
    try:
        age = time.time() - os.path.getmtime(cfg.state_file)
    except OSError:
        return 1
    return 0 if age < max(3 * cfg.tick_seconds + 60, 600) else 1


def test_notify(cfg) -> int:
    ok = send_all(cfg, "Comedy Cellar watcher: test notification",
                  "If you can read this, alerts will reach you.\n" + describe(cfg),
                  "info", attempts=3)
    print("test notification:", "DELIVERED" if ok else "ALL CHANNELS FAILED")
    return 0 if ok else 1


def diagnose(cfg) -> int:
    """Full transparency dump: what every strategy saw. Run this on the VPS."""
    http = HttpClient()
    today = datetime.now(ZoneInfo(cfg.tz)).date()
    res = perform_check(cfg, http, force_browser=True)
    print("=" * 72)
    print(f"DIAGNOSIS  {datetime.now(ZoneInfo(cfg.tz)):%Y-%m-%d %H:%M %Z}  (today={today})")
    print("=" * 72)
    for s in res.sightings:
        mark = "ok " if s.ok else "FAIL"
        print(f"[{mark}] {s.strategy:8s} {s.detail}")
        if s.dates:
            ds = sorted(s.dates)
            print(f"         dates: {', '.join(d.isoformat() for d in ds[:12])}"
                  f"{' …' if len(ds) > 12 else ''}  (max {max(ds)})")
        for u in s.xhr_urls[:15]:
            print(f"         xhr: {u}")
        for sample in s.api_samples[:4]:
            print(f"         --- API SAMPLE {sample['method']} {sample['url']} "
                  f"[{sample['status']}] {sample['content_type']}")
            if sample["post_data"]:
                print(f"             request body: {sample['post_data']}")
            print(f"             response head: {sample['body_head']}")
    print("-" * 72)
    print("DATE THROUGH:",
          f"{res.horizon} (via {res.horizon_source})" if res.horizon
          else "UNKNOWN — no strategy produced a date")
    print("TARGET HITS:", {d.isoformat(): v for d, v in res.found.items()} or "none")
    print("=" * 72)
    return 0 if res.horizon else 1


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--once", action="store_true", help="single check, then exit")
    p.add_argument("--test-notify", action="store_true", help="test every channel")
    p.add_argument("--diagnose", action="store_true",
                   help="dump what every detection strategy sees")
    p.add_argument("--health", action="store_true", help="container healthcheck")
    args = p.parse_args()

    cfg = load_config()
    if args.health:
        sys.exit(health(cfg))
    if args.test_notify:
        sys.exit(test_notify(cfg))
    if args.diagnose:
        sys.exit(diagnose(cfg))

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        sys.exit(run(cfg, once=args.once))
    except SystemExit:
        raise
    except Exception:
        log.exception("fatal error")
        try:
            send_all(cfg, "Comedy Cellar checker crashed",
                     "Fatal error; Docker will restart it. Check logs if this repeats.",
                     "warn", attempts=1)
        finally:
            sys.exit(1)


if __name__ == "__main__":
    main()
