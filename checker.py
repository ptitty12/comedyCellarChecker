#!/usr/bin/env python3
"""Comedy Cellar ticket watcher.

Polls comedycellar.com and sends notifications the moment shows for the
target dates (default: Sep 10/11/12, 2026) become visible anywhere on the
lineup/reservation pages or via the site's lineup AJAX API.

Designed to run forever inside a container:
  - two independent detection strategies (page scan + WordPress admin-ajax API)
  - notification fan-out to every configured channel with retries and a
    persistent queue (an alert is never dropped, it is retried until delivered)
  - repeat reminders per found date so one missed push can't lose the window
  - failure watchdog: if the site can't be checked for a while you get told,
    so silence never means "nothing happened"
  - daily heartbeat so you know it is alive
"""

import argparse
import html
import json
import logging
import os
import random
import re
import signal
import smtplib
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("cellar")

LINEUP_URL = "https://www.comedycellar.com/new-york-line-up/"
RESERVATIONS_URL = "https://www.comedycellar.com/reservations-new-york/"
AJAX_URL = "https://www.comedycellar.com/wp-admin/admin-ajax.php"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

MONTH_NAMES = {
    1: ["january", "jan"], 2: ["february", "feb"], 3: ["march", "mar"],
    4: ["april", "apr"], 5: ["may"], 6: ["june", "jun"],
    7: ["july", "jul"], 8: ["august", "aug"],
    9: ["september", "sept", "sep"],
    10: ["october", "oct"], 11: ["november", "nov"], 12: ["december", "dec"],
}

ALL_MONTHS_ALT = "|".join(
    sorted((n for names in MONTH_NAMES.values() for n in names), key=len, reverse=True)
)
ISO_ANY_RE = re.compile(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?!\d)")
MDY_ANY_RE = re.compile(
    rf"\b({ALL_MONTHS_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s*(20\d{{2}}))?\b",
    re.IGNORECASE,
)
SHOWTIME_RE = re.compile(r"\b\d{1,2}[:.]\d{2}\s*(?:pm|am)\b", re.IGNORECASE)
MONTH_BY_NAME = {n: num for num, names in MONTH_NAMES.items() for n in names}


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def _env(name, default):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


@dataclass
class Config:
    targets: list = field(default_factory=list)
    watch_urls: list = field(default_factory=lambda: [LINEUP_URL, RESERVATIONS_URL])
    check_interval: int = 300
    tick_seconds: int = 25
    state_file: str = "/data/state.json"
    tz: str = "America/New_York"

    # alert behaviour
    alert_repeats: int = 3            # reminders after the first alert, per date
    alert_repeat_minutes: int = 30
    failure_alert_hours: float = 2.0  # warn when the site is uncheckable this long
    failure_realert_hours: float = 12.0
    heartbeat_hour: int = 9           # local hour for the daily heartbeat, -1 = off
    startup_notify: bool = True

    # channels
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
    raw = _env("TARGET_DATES", "2026-09-10,2026-09-11,2026-09-12")
    for part in raw.split(","):
        part = part.strip()
        if part:
            cfg.targets.append(datetime.strptime(part, "%Y-%m-%d").date())
    if not cfg.targets:
        raise SystemExit("TARGET_DATES is empty")

    extra = _env("EXTRA_WATCH_URLS", "")
    for u in extra.split(","):
        if u.strip():
            cfg.watch_urls.append(u.strip())

    cfg.check_interval = max(60, int(_env("CHECK_INTERVAL_SECONDS", "300")))
    cfg.state_file = _env("STATE_FILE", cfg.state_file)
    cfg.tz = _env("TZ", cfg.tz)
    cfg.alert_repeats = int(_env("ALERT_REPEATS", "3"))
    cfg.alert_repeat_minutes = int(_env("ALERT_REPEAT_MINUTES", "30"))
    cfg.failure_alert_hours = float(_env("FAILURE_ALERT_HOURS", "2"))
    cfg.failure_realert_hours = float(_env("FAILURE_REALERT_HOURS", "12"))
    cfg.heartbeat_hour = int(_env("HEARTBEAT_HOUR", "9"))
    cfg.startup_notify = _env("STARTUP_NOTIFY", "1") not in ("0", "false", "no")

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
# HTTP client: curl_cffi (browser TLS fingerprint) when available, else requests
# --------------------------------------------------------------------------

class HttpClient:
    def __init__(self):
        self._curl_ok = False
        self._session = None
        try:
            from curl_cffi import requests as curl_requests  # noqa: F401
            self._curl_requests = curl_requests
            self._curl_ok = True
        except Exception:
            self._curl_requests = None
        self.reset()

    def reset(self):
        try:
            if self._curl_ok:
                self._session = self._curl_requests.Session(impersonate="chrome")
            else:
                self._session = requests.Session()
        except Exception as exc:
            log.warning("curl_cffi session failed (%s); falling back to requests", exc)
            self._curl_ok = False
            self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        })

    def _request(self, method, url, **kw):
        kw.setdefault("timeout", 30)
        last_exc = None
        for attempt in (1, 2):
            try:
                resp = self._session.request(method, url, **kw)
                return resp.status_code, resp.text or ""
            except Exception as exc:
                last_exc = exc
                if self._curl_ok:
                    # curl_cffi acting up -> permanently fall back to requests
                    log.warning("curl_cffi request failed (%s); switching to requests", exc)
                    self._curl_ok = False
                    self.reset()
                elif attempt == 1:
                    time.sleep(2)
                    self.reset()
        log.warning("%s %s failed: %s", method, url, last_exc)
        return 0, ""

    def get(self, url):
        return self._request("GET", url, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })

    def post_form(self, url, data, referer):
        return self._request("POST", url, data=data, headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://www.comedycellar.com",
            "Referer": referer,
        })


# --------------------------------------------------------------------------
# Date detection
# --------------------------------------------------------------------------

def patterns_for_date(d: date):
    """Regexes matching this exact date in the formats sites actually use."""
    name_alt = "|".join(sorted(MONTH_NAMES[d.month], key=len, reverse=True))
    yy = d.year % 100
    pats = [
        rf"(?<!\d){d.year}-{d.month:02d}-{d.day:02d}(?!\d)",
        rf"(?<!\d){d.year}/{d.month:02d}/{d.day:02d}(?!\d)",
        rf"(?<!\d)0?{d.month}[/\-.]0?{d.day}[/\-.](?:{d.year}|{yy:02d})(?!\d)",
        # "September 10" / "Sep 10th" / "September 10, 2026" — but not another year
        rf"\b(?:{name_alt})\.?\s+0?{d.day}(?:st|nd|rd|th)?\b(?!\s*,?\s*(?!{d.year})20\d\d)",
    ]
    return [re.compile(p, re.IGNORECASE) for p in pats]


def clean_html(text: str) -> str:
    text = html.unescape(text)
    return text.replace("\xa0", " ").replace("\\/", "/")


def text_mentions_date(text: str, d: date) -> bool:
    return any(p.search(text) for p in patterns_for_date(d))


def extract_all_dates(text: str, today: date):
    """Every plausible near-future date mentioned in the text (for the heartbeat)."""
    found = set()
    lo, hi = today - timedelta(days=2), today + timedelta(days=400)

    def keep(y, m, dd):
        try:
            val = date(y, m, dd)
        except ValueError:
            return
        if lo <= val <= hi:
            found.add(val)

    for y, m, dd in ISO_ANY_RE.findall(text):
        keep(int(y), int(m), int(dd))
    for name, dd, y in MDY_ANY_RE.findall(text):
        m = MONTH_BY_NAME.get(name.lower())
        if not m:
            continue
        if y:
            keep(int(y), m, int(dd))
        else:
            for year in (today.year, today.year + 1):
                try:
                    val = date(year, m, int(dd))
                except ValueError:
                    continue
                if lo <= val <= hi:
                    found.add(val)
                    break
    return found


# --------------------------------------------------------------------------
# Site checking
# --------------------------------------------------------------------------

@dataclass
class CheckResult:
    found: dict = field(default_factory=dict)   # date -> list of evidence strings
    confirmed: set = field(default_factory=set)  # dates confirmed via the AJAX API
    all_dates: set = field(default_factory=set)
    pages_ok: int = 0
    api_alive: bool = False
    errors: list = field(default_factory=list)

    @property
    def ok(self):
        return self.pages_ok > 0 or self.api_alive

    def add(self, d, evidence):
        self.found.setdefault(d, []).append(evidence)


def probe_ajax(http: HttpClient, target: date):
    """Ask the site's own lineup API about one date.

    Returns (status, detail): status is 'found' | 'negative' | 'unavailable'.
    """
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        payload = {
            "action": "cc_get_shows",
            "json": json.dumps({
                "date": target.strftime(fmt),
                "venue": "newyork",
                "type": "lineup",
            }),
        }
        status, text = http.post_form(AJAX_URL, payload, LINEUP_URL)
        body = (text or "").strip()
        if status != 200 or not body or body == "0" or body == "-1":
            continue
        try:
            doc = json.loads(body)
        except ValueError:
            continue
        blob = clean_html(json.dumps(doc))
        has_date = text_mentions_date(blob, target)
        has_times = bool(SHOWTIME_RE.search(blob))
        if has_date and has_times:
            return "found", f"lineup API returned shows for {target.isoformat()}"
        lower = blob.lower()
        if len(blob) > 200 or any(k in lower for k in ("show", "lineup", "comedian")):
            return "negative", "api answered, no shows for this date"
    return "unavailable", "api did not answer usefully"


def perform_check(cfg: Config, http: HttpClient, skip_api_for=frozenset()) -> CheckResult:
    res = CheckResult()
    today = datetime.now(ZoneInfo(cfg.tz)).date()

    for url in cfg.watch_urls:
        status, text = http.get(url)
        if status == 200 and text and len(text) > 3000:
            res.pages_ok += 1
            cleaned = clean_html(text)
            res.all_dates |= extract_all_dates(cleaned, today)
            for target in cfg.targets:
                if text_mentions_date(cleaned, target):
                    res.add(target, f"seen on {url}")
        else:
            res.errors.append(f"GET {url} -> HTTP {status}, {len(text or '')} bytes")

    for target in cfg.targets:
        if target in skip_api_for:
            continue
        status, detail = probe_ajax(http, target)
        if status == "found":
            res.api_alive = True
            res.confirmed.add(target)
            res.add(target, detail)
        elif status == "negative":
            res.api_alive = True
        else:
            res.errors.append(f"api probe {target.isoformat()}: {detail}")

    return res


# --------------------------------------------------------------------------
# Notification channels — each returns True on success
# --------------------------------------------------------------------------

PRIORITY = {  # level -> (ntfy, pushover)
    "alert": ("urgent", 1),
    "warn": ("high", 0),
    "info": ("default", -1),
}


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
        json={"chat_id": cfg.telegram_chat_id, "text": f"{title}\n\n{body}"},
        timeout=15,
    )
    return r.status_code == 200 and r.json().get("ok") is True


def send_discord(cfg, title, body, level):
    r = requests.post(cfg.discord_webhook,
                      json={"content": f"**{title}**\n{body}"[:1990]}, timeout=15)
    return r.status_code in (200, 204)


def send_slack(cfg, title, body, level):
    r = requests.post(cfg.slack_webhook,
                      json={"text": f"*{title}*\n{body}"}, timeout=15)
    return r.status_code == 200


def send_pushover(cfg, title, body, level):
    r = requests.post("https://api.pushover.net/1/messages.json", data={
        "token": cfg.pushover_token, "user": cfg.pushover_user,
        "title": title, "message": body, "priority": PRIORITY[level][1],
    }, timeout=15)
    return r.status_code == 200 and r.json().get("status") == 1


def send_webhook(cfg, title, body, level):
    r = requests.post(cfg.webhook_url, json={
        "source": "comedy-cellar-checker", "level": level,
        "title": title, "message": body, "ts": time.time(),
    }, timeout=15)
    return 200 <= r.status_code < 300


def send_email(cfg, title, body, level):
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = cfg.smtp_from
    msg["To"] = cfg.smtp_to
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
    """Fan out to every channel. True if at least one delivery succeeded."""
    if cfg.dry_run:
        log.info("[dry-run] %s: %s | %s", level, title, body.replace("\n", " ⏎ "))
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
             ", ".join(f"{k}={'ok' if v else 'FAIL'}" for k, v in results.items()) or "NO CHANNELS")
    return any(results.values())


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def default_state():
    return {
        "found": {},          # iso date -> {first_seen, alerts_sent, last_alert_ts, confirmed}
        "pending": [],        # queued notifications awaiting successful delivery
        "fail_since": None,   # ts when the current site-unreachable streak began
        "fail_alerted_ts": None,
        "last_ok_check": None,
        "last_heartbeat": None,   # local iso date of last heartbeat
        "checks_total": 0,
        "last_loop_ts": None,
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
    state["pending"].append({
        "title": title, "body": body, "level": level,
        "created": time.time(), "attempts": 0, "next_try": 0,
    })


def flush_pending(cfg, state):
    now = time.time()
    remaining = []
    for item in state["pending"]:
        if now < item.get("next_try", 0):
            remaining.append(item)
            continue
        if send_all(cfg, item["title"], item["body"], item["level"]):
            continue
        item["attempts"] += 1
        backoff = min(60 * (2 ** min(item["attempts"], 5)), 1800)
        item["next_try"] = now + backoff
        log.warning("delivery failed (attempt %d), retrying in %ds: %r",
                    item["attempts"], backoff, item["title"])
        remaining.append(item)
    state["pending"] = remaining


# --------------------------------------------------------------------------
# Alert / heartbeat / watchdog logic
# --------------------------------------------------------------------------

def fmt_date(d: date) -> str:
    return d.strftime("%A, %B %-d, %Y") if os.name != "nt" else d.strftime("%A, %B %d, %Y")


def alert_body(dates_with_evidence, cfg, reminder=None):
    lines = ["Comedy Cellar just posted shows for:"]
    for d, evidence in sorted(dates_with_evidence.items()):
        lines.append(f"  - {fmt_date(d)}   ({'; '.join(evidence)})")
    lines += [
        "",
        f"Book NOW: {RESERVATIONS_URL}",
        f"Lineup:   {LINEUP_URL}",
    ]
    if reminder:
        lines.append(f"\n(Reminder {reminder} — set ALERT_REPEATS=0 to silence repeats.)")
    elif cfg.alert_repeats > 0:
        lines.append(f"\n(You'll get {cfg.alert_repeats} reminders, "
                     f"every {cfg.alert_repeat_minutes} min, in case this one is missed.)")
    return "\n".join(lines)


def handle_check_result(cfg, state, res: CheckResult):
    now = time.time()

    # --- new dates found -> one combined alert
    new = {}
    for d, evidence in res.found.items():
        key = d.isoformat()
        if key not in state["found"]:
            new[d] = evidence
            state["found"][key] = {
                "first_seen": now, "alerts_sent": 1, "last_alert_ts": now,
                "confirmed": d in res.confirmed,
            }
        else:
            state["found"][key]["confirmed"] = (
                state["found"][key].get("confirmed") or d in res.confirmed)
    if new:
        names = ", ".join(d.strftime("%b %-d" if os.name != "nt" else "%b %d")
                          for d in sorted(new))
        log.info("TARGET DATES FOUND: %s", names)
        enqueue(state, f"Comedy Cellar: {names} tickets are UP!",
                alert_body(new, cfg), "alert")

    # --- reminders for already-found dates
    for key, info in state["found"].items():
        if info["alerts_sent"] <= cfg.alert_repeats and \
                now - info["last_alert_ts"] >= cfg.alert_repeat_minutes * 60:
            d = date.fromisoformat(key)
            info["alerts_sent"] += 1
            info["last_alert_ts"] = now
            n = info["alerts_sent"] - 1
            enqueue(state, f"Reminder: Comedy Cellar {d.strftime('%b %d')} tickets are up",
                    alert_body({d: ["reminder"]}, cfg,
                               reminder=f"{n}/{cfg.alert_repeats}"), "alert")

    # --- watchdog: site uncheckable vs recovered
    if res.ok:
        state["last_ok_check"] = now
        if state.get("fail_alerted_ts"):
            enqueue(state, "Comedy Cellar checker recovered",
                    "The site is reachable again — monitoring resumed normally.", "info")
        state["fail_since"] = None
        state["fail_alerted_ts"] = None
    else:
        state["fail_since"] = state.get("fail_since") or now
        failing_h = (now - state["fail_since"]) / 3600
        last_alert = state.get("fail_alerted_ts")
        if failing_h >= cfg.failure_alert_hours and (
                not last_alert or now - last_alert >= cfg.failure_realert_hours * 3600):
            state["fail_alerted_ts"] = now
            enqueue(state, "Comedy Cellar checker is BLIND",
                    f"Every check has failed for {failing_h:.1f}h "
                    f"(site down, blocking us, or page changed).\n"
                    f"Errors: {'; '.join(res.errors[:4]) or 'unknown'}\n\n"
                    f"Until this recovers, CHECK MANUALLY: {LINEUP_URL}",
                    "warn")


def maybe_heartbeat(cfg, state, res: CheckResult):
    if cfg.heartbeat_hour < 0:
        return
    now_local = datetime.now(ZoneInfo(cfg.tz))
    today = now_local.date().isoformat()
    if state.get("last_heartbeat") is None:
        state["last_heartbeat"] = today  # skip on first ever run; startup notice covers it
        return
    if state["last_heartbeat"] == today or now_local.hour < cfg.heartbeat_hour:
        return
    state["last_heartbeat"] = today

    future = sorted(d for d in res.all_dates if d >= now_local.date())
    horizon = fmt_date(future[-1]) if future else "unknown (no dates parsed!)"
    missing = [fmt_date(t) for t in cfg.targets
               if t.isoformat() not in state["found"]]
    found = [fmt_date(t) for t in cfg.targets if t.isoformat() in state["found"]]
    lines = [
        f"Still watching. {state['checks_total']} checks so far.",
        f"Site currently lists shows through: {horizon}",
        f"Strategies: page scan {'OK' if res.pages_ok else 'FAILING'} "
        f"({res.pages_ok}/{len(cfg.watch_urls)} pages), "
        f"lineup API {'OK' if res.api_alive else 'FAILING'}",
    ]
    if found:
        lines.append("Already found: " + "; ".join(found))
    if missing:
        lines.append("Still waiting for: " + "; ".join(missing))
    else:
        lines.append("All target dates found — you can stop this service.")
    enqueue(state, "Comedy Cellar watcher: daily heartbeat", "\n".join(lines), "info")


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True
    log.info("signal %s received, shutting down after current tick", signum)


def describe(cfg):
    chans = ", ".join(name for name, _ in enabled_channels(cfg)) or "NONE — configure a channel!"
    targets = ", ".join(t.isoformat() for t in cfg.targets)
    return (f"Watching for: {targets}\nEvery {cfg.check_interval}s | "
            f"channels: {chans}\nState: {cfg.state_file}")


def run(cfg: Config, once=False):
    http = HttpClient()
    state = load_state(cfg.state_file)
    log.info("starting\n%s", describe(cfg))

    if cfg.startup_notify and not once:
        enqueue(state, "Comedy Cellar watcher started", describe(cfg), "info")

    last_check = 0.0
    while True:
        loop_start = time.time()
        try:
            if loop_start - last_check >= cfg.check_interval or once:
                last_check = loop_start + random.uniform(-0.1, 0.1) * cfg.check_interval
                already = {date.fromisoformat(k) for k, v in state["found"].items()
                           if v.get("confirmed")}
                res = perform_check(cfg, http, skip_api_for=already)
                state["checks_total"] += 1
                log.info("check #%d: pages_ok=%d api_alive=%s found=%s horizon=%s%s",
                         state["checks_total"], res.pages_ok, res.api_alive,
                         [d.isoformat() for d in res.found] or "none",
                         max(res.all_dates).isoformat() if res.all_dates else "?",
                         (" errors=" + "; ".join(res.errors)) if res.errors else "")
                handle_check_result(cfg, state, res)
                maybe_heartbeat(cfg, state, res)
                if not res.ok:
                    http.reset()  # fresh session/cookies for the next attempt
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
    """0 = healthy (loop wrote state recently), 1 = wedged."""
    try:
        age = time.time() - os.path.getmtime(cfg.state_file)
    except OSError:
        return 1
    return 0 if age < max(3 * cfg.tick_seconds + 60, 600) else 1


def test_notify(cfg) -> int:
    ok = send_all(cfg, "Comedy Cellar watcher: test notification",
                  "If you can read this, alerts will reach you.\n" + describe(cfg),
                  "info", attempts=3)
    print("test notification:", "DELIVERED (at least one channel)" if ok else "ALL CHANNELS FAILED")
    return 0 if ok else 1


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="single check, then exit")
    parser.add_argument("--test-notify", action="store_true",
                        help="send a test message through every configured channel")
    parser.add_argument("--health", action="store_true", help="container healthcheck")
    args = parser.parse_args()

    cfg = load_config()
    if args.health:
        sys.exit(health(cfg))
    if args.test_notify:
        sys.exit(test_notify(cfg))

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        sys.exit(run(cfg, once=args.once))
    except SystemExit:
        raise
    except Exception:
        log.exception("fatal error")
        try:  # best effort: tell the user the process is bouncing
            send_all(cfg, "Comedy Cellar checker crashed",
                     "The watcher hit a fatal error and will be restarted by Docker. "
                     "Check container logs if this repeats.", "warn", attempts=1)
        finally:
            sys.exit(1)


if __name__ == "__main__":
    main()
