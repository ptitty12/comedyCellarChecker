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
import re
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
#   action=cc_get_shows&json={"date":"2026-09-10","venue":"newyork","type":"lineup"}
#   -> {"show": {"html": "…", "date": "Thursday September 10, 2026"},
#       "date": "2026-09-10"}
# It accepts ISO dates and echoes back the date it answered about, which is what
# makes a per-date status trustworthy. It is NOT wp-admin/admin-ajax.php, which
# answers but ignores this action.
LINEUP_API_URL = "https://www.comedycellar.com/lineup/api/"

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
    # date -> {status, comedians, showtimes, titles, show_ids}
    day_status: dict = field(default_factory=dict)
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
# Strategy 2: the site's own lineup API (per-date status)
# --------------------------------------------------------------------------

# How far a date has progressed, in order. Each step is a thing worth being told
# about, and the ordering is what makes "did anything improve?" a simple compare.
NOT_LISTED = "not_listed"      # empty response: date is beyond the published window
NO_LINEUP = "no_lineup"        # "No Comedians added yet!" — date exists, nothing booked
SHOWTIMES = "showtimes"        # show times posted, comedians not named yet
LINEUP = "lineup"              # comedians announced
UNKNOWN = "unknown"            # response we don't recognise; never alerts

RANK = {UNKNOWN: -1, NOT_LISTED: 0, NO_LINEUP: 1, SHOWTIMES: 2, LINEUP: 3}
# At or above this, the user wants a push: there is something to act on.
ALERT_RANK = RANK[SHOWTIMES]

# Markers taken from live responses on 2026-08-16. A day with a lineup returns
# set-header blocks with show times and <span class="name"> per comedian; a day
# without returns exactly '<p class="no-shows">No Comedians added yet!</p>'.
NO_LINEUP_RE = re.compile(r'class=["\']no-shows|no\s+comedians\s+added', re.I)
NAME_RE = re.compile(r'<span[^>]*class=["\'][^"\']*\bname\b[^"\']*["\'][^>]*>(.*?)</span>',
                     re.I | re.S)
TITLE_RE = re.compile(r'<span[^>]*class=["\'][^"\']*\btitle\b[^"\']*["\'][^>]*>(.*?)</span>',
                      re.I | re.S)
SHOWID_RE = re.compile(r'reservations-[a-z]*newyork/?\?showid=(\d+)', re.I)
TAG_RE = re.compile(r"<[^>]+>")


def _plain(fragment):
    return clean_html(TAG_RE.sub(" ", fragment)).strip()


def classify_lineup(html):
    """Turn a lineup HTML fragment into a status plus what it says.

    Comedian names decide 'lineup'; show times alone mean the slots exist but
    nobody is announced yet. Both are things the user asked to hear about.
    """
    html = html or ""
    comedians, seen = [], set()
    for raw in NAME_RE.findall(html):
        name = _plain(raw)
        if name and name.lower() not in seen:
            seen.add(name.lower())
            comedians.append(name)
    showtimes = [m.strip() for m in SHOWTIME_RE.findall(_plain(html))]
    # SHOWTIME_RE has no groups, so findall returns whole matches.
    titles, seen_t = [], set()
    for raw in TITLE_RE.findall(html):
        title = _plain(raw)
        if title and title.lower() not in seen_t:
            seen_t.add(title.lower())
            titles.append(title)
    show_ids = list(dict.fromkeys(SHOWID_RE.findall(html)))

    if not html.strip():
        status = NOT_LISTED
    elif comedians:
        status = LINEUP
    elif showtimes:
        status = SHOWTIMES
    elif NO_LINEUP_RE.search(html):
        status = NO_LINEUP
    else:
        status = UNKNOWN
    return {"status": status, "comedians": comedians, "showtimes": showtimes,
            "titles": titles, "show_ids": show_ids}


def query_lineup_api(http, when, venue="newyork"):
    """Ask the lineup endpoint about one date.

    `when` is a date, or a keyword the site accepts such as "today".
    Returns (doc or None, detail). The endpoint echoes back the date it answered
    about, which is checked by the callers below.
    """
    key = when if isinstance(when, str) else when.isoformat()
    payload = {"action": API_ACTION,
               "json": json.dumps({"date": key, "venue": venue, "type": "lineup"})}
    status, text = http.post_form(LINEUP_API_URL, payload)
    body = (text or "").strip()
    if status != 200 or not body:
        return None, f"HTTP {status}"
    try:
        doc = json.loads(body)
    except ValueError:
        return None, "non-JSON response"
    if not isinstance(doc, dict):
        return None, "unexpected JSON shape"
    return doc, "ok"


def lineup_html(doc):
    show = doc.get("show")
    return (show or {}).get("html", "") if isinstance(show, dict) else ""


def has_shows(fragment):
    return bool(SHOWTIME_RE.search(fragment or ""))


def fetch_day(http, day):
    """Status of one date, or None if the answer can't be trusted.

    The endpoint returns the date it is describing, both as an ISO string and in
    prose ("Thursday September 10, 2026"). Requiring that to match the date we
    asked for is what makes a "lineup posted!" alert trustworthy: a stale or
    ignored date parameter is caught here rather than paged to the user.
    """
    doc, detail = query_lineup_api(http, day)
    if doc is None:
        return None, detail
    show = doc.get("show") if isinstance(doc.get("show"), dict) else {}
    echoed = str(doc.get("date") or show.get("date") or "")
    if echoed and not text_mentions_date(clean_html(echoed), day):
        return None, f"answered about {echoed!r}, not {day}"
    if not echoed:
        return None, "response did not say which date it describes"
    info = classify_lineup(lineup_html(doc))
    info["date"] = day
    return info, "ok"


def from_lineup_api(http, targets, today):
    """Per-target-date status straight from the endpoint the date picker uses.

    Not authoritative for the date-through: it is only asked about the target
    dates, so its dates say nothing about how far ahead the site publishes. That
    stays the browser's job, which keeps the date-through canary honest.
    """
    s = Sighting("lineup-api", authoritative=False)
    results, reached = [], False
    for t in targets:
        info, detail = fetch_day(http, t)
        if info is None:
            results.append(f"{t}:{detail}")
            continue
        reached = True
        s.day_status[t] = info
        results.append(f"{t}:{info['status']}"
                       + (f"({len(info['comedians'])} comedians)"
                          if info["comedians"] else ""))
        if RANK[info["status"]] >= RANK[NO_LINEUP]:
            s.dates.add(t)          # the date exists on the site
        if RANK[info["status"]] >= ALERT_RANK:
            s.target_hits[t] = (f"lineup API: {info['status']}"
                                + (f" — {', '.join(info['comedians'][:4])}"
                                   if info["comedians"] else ""))
    s.ok = reached
    s.detail = "lineup-api " + ", ".join(results)
    return [s]

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


def day_status_of(sightings):
    """Merge per-date status across strategies, best-informed wins."""
    merged = {}
    for s in sightings:
        for d, info in s.day_status.items():
            if RANK[info["status"]] > RANK.get(
                    merged.get(d, {}).get("status", UNKNOWN), -1):
                merged[d] = info
    return merged
