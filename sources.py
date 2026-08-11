"""Where dates come from.

Four independent strategies, tried cheapest-first. Any one of them producing a
date is enough; the browser strategy exists because comedycellar.com renders its
lineup client-side, so raw HTML contains no dates at all.

Every strategy returns a Sighting so the caller can report exactly which ones
worked — a checker that can't say where its data came from can't be trusted.
"""

import glob
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlparse

import requests

from dateparse import clean_html, extract_all_dates, text_mentions_date, SHOWTIME_RE

log = logging.getLogger("cellar.sources")

LINEUP_URL = "https://www.comedycellar.com/new-york-line-up/"
RESERVATIONS_URL = "https://www.comedycellar.com/reservations-newyork/"

# The site's own lineup endpoint, captured from a browser render on 2026-08-11:
#   POST /lineup/api/
#   action=cc_get_shows&json={"date":"today","venue":"newyork","type":"lineup"}
#   -> {"show": {"html": "<div>…6:00 pm show - …</div>"}}
# Note it is NOT wp-admin/admin-ajax.php, which answers but ignores this action.
LINEUP_API_URL = "https://www.comedycellar.com/lineup/api/"

# The response does not echo the date back, so "shows present" only means "shows
# for the date we asked about" if the endpoint actually honours the date. A date
# this far out cannot legitimately have a lineup, so it is queried on every check
# as a live trust test — see from_lineup_api.
SENTINEL_OFFSET_DAYS = 300

# The homepage is included because a target date may be announced there in prose;
# it is deliberately NOT authoritative for the date-through (see AUTHORITATIVE_URLS)
# because it carries unrelated far-future dates.
DEFAULT_STATIC_URLS = [
    LINEUP_URL,
    RESERVATIONS_URL,
    "https://www.comedycellar.com/",
]

# WordPress/plugin JSON endpoints that would expose show dates server-side.
REST_ENDPOINTS = [
    "https://www.comedycellar.com/wp-json/tribe/events/v1/events?per_page=50&start_date={start}",
    "https://www.comedycellar.com/wp-json/wp/v2/shows?per_page=50",
    "https://www.comedycellar.com/wp-json/wp/v2/lineup?per_page=50",
    "https://www.comedycellar.com/wp-json/wp/v2/types",
]

API_ACTION = "cc_get_shows"

# The captured request used the literal keyword "today", and a live probe showed
# an ISO date returns an empty lineup even for dates the site definitely lists.
# So the encoding the endpoint expects is unknown: these are tried against a
# control date until one demonstrably works.
DATE_ENCODINGS = [
    ("iso", lambda d: d.isoformat()),
    ("us-slash", lambda d: d.strftime("%m/%d/%Y")),
    ("us-slash-short", lambda d: f"{d.month}/{d.day}/{d.year}"),
    ("dash-us", lambda d: d.strftime("%m-%d-%Y")),
    ("compact", lambda d: d.strftime("%Y%m%d")),
    ("epoch", lambda d: str(int(time.mktime(d.timetuple())))),
]

# Offset used as the positive control: far enough out to be a real test, well
# inside the ~4-week window the site was measured to publish.
POSITIVE_PROBE_OFFSET_DAYS = 3

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Playwright's on-disk layout differs by version: "Chrome for Testing" builds use
# chrome-linux64/, older bundles use chrome-linux/. Both are matched so a version
# bump can't silently disable the only strategy that can read this site.
CHROMIUM_GLOBS = [
    "/ms-playwright/chromium-*/chrome-linux64/chrome",
    "/ms-playwright/chromium-*/chrome-linux/chrome",
    "/ms-playwright/chromium_headless_shell-*/chrome-linux64/chrome-headless-shell",
    "/ms-playwright/chromium_headless_shell-*/chrome-linux/headless_shell",
    "/opt/pw-browsers/chromium-*/chrome-linux64/chrome",
    "/opt/pw-browsers/chromium-*/chrome-linux/chrome",
]


# Pages whose dates represent the bookable show window. The homepage is scanned
# for target dates too, but a stray date in a news blurb there must never be
# mistaken for the lineup's date-through, nor suppress a browser render.
AUTHORITATIVE_URLS = {LINEUP_URL, RESERVATIONS_URL}


@dataclass
class Sighting:
    strategy: str
    ok: bool = False
    detail: str = ""
    dates: set = field(default_factory=set)
    target_hits: dict = field(default_factory=dict)
    xhr_urls: list = field(default_factory=list)
    # Request/response samples from the site's own API, captured during a render
    # so the endpoint can be called directly instead of launching a browser.
    api_samples: list = field(default_factory=list)
    # Whether this source's dates may define the date-through.
    authoritative: bool = True

    def scan(self, text, targets, today):
        """Pull every date out of a blob, and note which targets it proves."""
        cleaned = clean_html(text)
        self.dates |= extract_all_dates(cleaned, today)
        for t in targets:
            if text_mentions_date(cleaned, t):
                self.target_hits.setdefault(t, self.detail or self.strategy)


class HttpClient:
    """requests, upgraded to a browser TLS fingerprint when curl_cffi is present."""

    def __init__(self):
        self._curl_ok = False
        try:
            from curl_cffi import requests as curl_requests
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
            log.warning("curl_cffi session failed (%s); using requests", exc)
            self._curl_ok = False
            self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        })

    def _request(self, method, url, **kw):
        kw.setdefault("timeout", 30)
        last = None
        for attempt in (1, 2):
            try:
                r = self._session.request(method, url, **kw)
                return r.status_code, r.text or ""
            except Exception as exc:
                last = exc
                if self._curl_ok:
                    log.warning("curl_cffi failed (%s); switching to requests", exc)
                    self._curl_ok = False
                    self.reset()
                elif attempt == 1:
                    time.sleep(2)
                    self.reset()
        log.warning("%s %s failed: %s", method, url, last)
        return 0, ""

    def get(self, url):
        return self._request("GET", url, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })

    def get_json(self, url):
        return self._request("GET", url, headers={"Accept": "application/json, */*"})

    def post_form(self, url, data, referer=LINEUP_URL):
        return self._request("POST", url, data=data, headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://www.comedycellar.com",
            "Referer": referer,
        })


# --------------------------------------------------------------------------
# Strategy 1: static HTML
# --------------------------------------------------------------------------

def from_static(http, urls, targets, today):
    out = []
    for url in urls:
        s = Sighting("static", detail=f"static {url}",
                     authoritative=url in AUTHORITATIVE_URLS)
        status, text = http.get(url)
        if status == 200 and len(text) > 3000:
            s.ok = True
            s.scan(text, targets, today)
            s.detail = f"static {url} ({len(text)}B, {len(s.dates)} dates)"
        else:
            s.detail = f"static {url} -> HTTP {status}, {len(text or '')}B"
        out.append(s)
    return out


# --------------------------------------------------------------------------
# Strategy 2: WordPress / plugin REST APIs
# --------------------------------------------------------------------------

def from_rest(http, targets, today):
    out = []
    start = today.isoformat()
    for tmpl in REST_ENDPOINTS:
        url = tmpl.format(start=start)
        s = Sighting("rest", detail=f"rest {url.split('/wp-json/')[-1][:48]}")
        status, text = http.get_json(url)
        if status == 200 and text.strip().startswith(("{", "[")):
            try:
                json.loads(text)
            except ValueError:
                s.detail += " -> non-JSON body"
                out.append(s)
                continue
            s.ok = True
            s.scan(text, targets, today)
            s.detail += f" -> 200, {len(s.dates)} dates"
        else:
            s.detail += f" -> HTTP {status}"
        out.append(s)
    return out


# --------------------------------------------------------------------------
# Strategy 3: admin-ajax date probes
# --------------------------------------------------------------------------

def query_lineup_api(http, when, venue="newyork"):
    """Ask the lineup endpoint about one date.

    `when` is an already-encoded string (see DATE_ENCODINGS) or a keyword the site
    accepts, e.g. "today". Returns (reachable, fragment, detail).
    """
    key = when if isinstance(when, str) else when.isoformat()
    payload = {"action": API_ACTION,
               "json": json.dumps({"date": key, "venue": venue, "type": "lineup"})}
    status, text = http.post_form(LINEUP_API_URL, payload)
    body = (text or "").strip()
    if status != 200 or not body:
        return False, "", f"HTTP {status}"
    try:
        doc = json.loads(body)
    except ValueError:
        return False, "", "non-JSON response"
    fragment = ""
    if isinstance(doc, dict):
        show = doc.get("show")
        if isinstance(show, dict):
            fragment = show.get("html") or ""
    if not fragment:
        fragment = json.dumps(doc)
    return True, clean_html(fragment), "ok"


def has_shows(fragment):
    return bool(SHOWTIME_RE.search(fragment))


_CALIBRATION = {}


def calibrate_lineup_api(http, today, cache=_CALIBRATION):
    """Find a date encoding the endpoint provably honours.

    Two controls, because either one alone can be fooled:

    * positive — a date a few days out, which the site definitely lists, must come
      back WITH showtimes. Without this an endpoint that silently answers "no
      shows" to every encoding looks perfectly healthy while detecting nothing.
      (Measured on the live site: ISO dates do exactly that.)
    * negative — a date ~10 months out, which cannot have a lineup, must come back
      WITHOUT showtimes, or the endpoint is echoing something unrelated and would
      report every target as live.

    Returns (encoder or None, detail). Cached per day, since the encoding a site
    accepts does not change between checks.
    """
    if cache.get("day") == today:
        return cache.get("encoder"), cache.get("detail")

    positive = today + timedelta(days=POSITIVE_PROBE_OFFSET_DAYS)
    sentinel = today + timedelta(days=SENTINEL_OFFSET_DAYS)
    encoder, detail = None, ""
    tried = []
    for name, enc in DATE_ENCODINGS:
        reachable, fragment, why = query_lineup_api(http, enc(positive))
        if not reachable:
            detail = f"unreachable ({why})"
            break
        if not has_shows(fragment):
            tried.append(name)
            continue
        reachable, fragment, _ = query_lineup_api(http, enc(sentinel))
        if reachable and has_shows(fragment):
            tried.append(f"{name}(echoes)")
            continue
        encoder, detail = enc, f"date encoding '{name}' verified against both controls"
        break
    else:
        detail = (f"no date encoding works (tried {', '.join(tried)}); endpoint only "
                  f"answers for its own keywords, so it cannot detect a future date")

    cache.update(day=today, encoder=encoder, detail=detail)
    return encoder, detail


def from_lineup_api(http, targets, today):
    """Query the site's own endpoint per target date, if it can be trusted to.

    Contributes nothing unless calibration proves the endpoint honours a date
    encoding — reporting that plainly rather than appearing to work.
    """
    s = Sighting("lineup-api")
    encoder, detail = calibrate_lineup_api(http, today)
    if encoder is None:
        s.ok = not detail.startswith("unreachable")
        s.detail = f"lineup-api contributes nothing: {detail}"
        return [s]

    results, reached = [], False
    for t in targets:
        reachable, fragment, why = query_lineup_api(http, encoder(t))
        if not reachable:
            results.append(f"{t}:{why}")
            continue
        reached = True
        if has_shows(fragment):
            s.dates.add(t)
            s.target_hits[t] = f"lineup API returned showtimes for {t}"
            results.append(f"{t}:SHOWS")
        else:
            results.append(f"{t}:none")

    # Health must reflect this check, not a calibration cached when things worked.
    # Claiming ok here would keep the "checker is BLIND" watchdog quiet during an
    # outage, which is the one time it needs to fire.
    s.ok = reached
    if not reached:
        _CALIBRATION.clear()   # re-verify once the endpoint answers again
    s.detail = f"lineup-api ({detail}) " + ", ".join(results)
    return [s]


# --------------------------------------------------------------------------
# Strategy 4: headless Chromium (renders the JS the other strategies can't see)
# --------------------------------------------------------------------------

HARVEST_JS = """els => els.map(e => [
    e.getAttribute('value'), e.getAttribute('data-date'), e.getAttribute('data-day'),
    e.getAttribute('datetime'), e.getAttribute('href'), e.textContent
].filter(Boolean).join(' '))"""

HARVEST_SELECTOR = (
    "option,[value],[data-date],[data-day],[datetime],time,a[href*='date'],"
    ".date,.day,.show-date,[class*='date'],[class*='day']"
)


def chromium_path():
    """An explicit Chromium binary, or None to let Playwright resolve its own.

    Globbed rather than pinned so a Playwright version bump doesn't silently
    disable the only strategy that can read this site.
    """
    override = os.environ.get("CHROMIUM_PATH", "")
    if override and os.path.exists(override):
        return override
    for pattern in CHROMIUM_GLOBS:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[-1]
    return None


def from_browser(url, targets, today, settle_ms=3500, timeout_s=60):
    """Render the page, then harvest the DOM *and* every JSON/XHR payload it fetched.

    The XHR bodies are the real prize: they are the show data itself, and their
    URLs tell us the endpoint the site actually uses.
    """
    s = Sighting("browser", detail=f"browser {url}")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        s.detail = f"browser unavailable: playwright not importable ({exc})"
        return [s]

    bodies, xhr_urls, api_samples = [], [], []
    page_host = urlparse(url).hostname

    def on_response(resp):
        try:
            if resp.request.resource_type not in ("xhr", "fetch"):
                return
            # Same-origin only: third-party ad/analytics traffic is pure noise.
            if urlparse(resp.url).hostname != page_host:
                return
            ct = (resp.headers or {}).get("content-type", "")
            if not any(k in ct for k in ("json", "javascript", "text/html", "text/plain")):
                return
            xhr_urls.append(f"{resp.request.method} {resp.url} [{resp.status}]")
            body = resp.text()
            if body and len(body) < 2_000_000:
                bodies.append(body)
                api_samples.append({
                    "method": resp.request.method,
                    "url": resp.url,
                    "post_data": (resp.request.post_data or "")[:1000],
                    "content_type": ct,
                    "status": resp.status,
                    "body_head": body[:1200],
                })
        except Exception:
            pass  # a body that can't be read is not worth failing the render over

    nav_status, nav_error = None, ""
    try:
        with sync_playwright() as p:
            launch = {"args": ["--no-sandbox", "--disable-dev-shm-usage",
                               "--disable-gpu"]}
            exe = chromium_path()
            if exe:
                launch["executable_path"] = exe
            browser = p.chromium.launch(**launch)
            try:
                page = browser.new_page(user_agent=USER_AGENT,
                                        viewport={"width": 1400, "height": 1000})
                page.on("response", on_response)
                try:
                    resp = page.goto(url, wait_until="domcontentloaded",
                                     timeout=timeout_s * 1000)
                    nav_status = resp.status if resp else None
                except Exception as exc:
                    nav_error = f"{type(exc).__name__}: {exc}".splitlines()[0]
                    log.warning("browser goto failed: %s", nav_error)
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass
                page.wait_for_timeout(settle_ms)

                html_text = page.content()
                try:
                    extras = page.eval_on_selector_all(HARVEST_SELECTOR, HARVEST_JS)
                except Exception:
                    extras = []
                try:
                    body_text = page.inner_text("body")[:400_000]
                except Exception:
                    body_text = ""
            finally:
                browser.close()
    except Exception as exc:
        s.detail = f"browser failed: {type(exc).__name__}: {exc}"
        return [s]

    # A failed navigation still yields a big DOM — Chromium's own error page — so
    # success is judged on the navigation response, never on document size.
    if nav_status is None or nav_status >= 400:
        s.detail = (f"browser {url} -> navigation failed "
                    f"({nav_error or f'HTTP {nav_status}'})")
        return [s]

    blob = "\n".join([html_text, body_text, "\n".join(extras), "\n".join(bodies)])
    s.ok = len(html_text) > 500
    s.xhr_urls = xhr_urls
    s.api_samples = api_samples
    s.scan(blob, targets, today)
    s.detail = (f"browser {url} (HTTP {nav_status}, dom {len(html_text)}B, "
                f"{len(extras)} nodes, {len(bodies)} xhr payloads, {len(s.dates)} dates)")
    return [s]


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def discover(http, targets, today, static_urls=None, use_browser=True,
             browser_url=LINEUP_URL, use_rest=False, use_api=True):
    """Run the strategies cheapest-first; the browser only if nothing else found a date.

    Returns (sightings, dates, target_hits).
    """
    sightings = list(from_static(http, static_urls or DEFAULT_STATIC_URLS, targets, today))
    if use_rest:
        sightings += from_rest(http, targets, today)
    if use_api:
        sightings += from_lineup_api(http, targets, today)

    # Render only when no *authoritative* source produced a date. Gating on "any
    # date anywhere" would let one stray homepage date skip the render, which is
    # the only strategy that can actually read this site.
    if use_browser and not any(s.dates for s in sightings if s.authoritative):
        sightings += from_browser(browser_url, targets, today)

    dates, hits = set(), {}
    for s in sightings:
        if s.authoritative:
            dates |= s.dates
        for d, ev in s.target_hits.items():
            hits.setdefault(d, []).append(ev)
    return sightings, dates, hits
