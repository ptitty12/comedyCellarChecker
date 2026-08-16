#!/usr/bin/env python3
"""Tests: python3 -m unittest test_checker -v

Covers date parsing, every discovery strategy (including a real headless-Chromium
render of a JS-rendered page), horizon tracking, alert flow and delivery.
"""

import json
import os
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import checker
import sources
from checker import (CheckResult, Config, alert_body, enqueue, flush_pending,
                     handle_check_result, handle_day_status, handle_horizon,
                     health, load_state, perform_check, save_state, send_all,
                     status_lines)
from dateparse import (clean_html, extract_all_dates, horizon_of,
                       text_mentions_date)
from sources import (Sighting, from_browser, from_lineup_api, from_rest,
                     from_static)

SEP10, SEP11, SEP12 = date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 12)
TARGETS = [SEP10, SEP11, SEP12]
TODAY = date(2026, 8, 11)


def make_cfg(**kw):
    cfg = Config()
    cfg.targets = list(TARGETS)
    cfg.tz, cfg.dry_run, cfg.ntfy_topic = "UTC", True, ""
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


class TestDateMatching(unittest.TestCase):
    def test_positive_formats(self):
        for text in ['value="2026-09-10"', "2026-9-10", "2026/09/10", "20260910",
                     "on 9/10/2026 at 7pm", "09-10-26", "Thursday, September 10",
                     "September 10th, 2026", "SEPT. 10", "sep 10",
                     "September&nbsp;10", '{"date":"2026-09-10"}',
                     '{"date":"2026-09-10"}'.replace("/", "\\/")]:
            self.assertTrue(text_mentions_date(clean_html(text), SEP10), text)

    def test_negative_formats(self):
        for text in ["September 10, 2025", "2025-09-10", "September 100 jokes",
                     "rated 9/10 by fans", "September 11", "1026-09-10x2", "josep 10"]:
            self.assertFalse(text_mentions_date(clean_html(text), SEP10), text)

    def test_extract_covers_many_shapes(self):
        text = clean_html(
            '<option value="2026-08-12">Wed</option> '
            '<a href="/x?date=2026-08-20">go</a> '
            '<time datetime="20260825">late</time> '
            'data-date="9/3" '
            'posted 8/30/2026 ... blog from January 5, 2020 ... next show September 5'
        )
        got = extract_all_dates(text, TODAY)
        for expected in [date(2026, 8, 12), date(2026, 8, 20), date(2026, 8, 25),
                         date(2026, 9, 3), date(2026, 8, 30), date(2026, 9, 5)]:
            self.assertIn(expected, got)
        self.assertNotIn(date(2020, 1, 5), got)

    def test_context_numeric_needs_context(self):
        """Bare 9/10 must not become a date, but data-date="9/10" must."""
        self.assertNotIn(SEP10, extract_all_dates("scored 9/10 overall", TODAY))
        self.assertIn(SEP10, extract_all_dates('data-date="9/10"', TODAY))

    def test_horizon_of(self):
        dates = {date(2026, 8, 1), date(2026, 8, 18), date(2026, 8, 12)}
        self.assertEqual(horizon_of(dates, TODAY), date(2026, 8, 18))
        self.assertIsNone(horizon_of({date(2026, 1, 1)}, TODAY))
        self.assertIsNone(horizon_of(set(), TODAY))



class FakeHttp:
    def __init__(self, pages=None, api=None, json_urls=None, api_default=None):
        self.pages, self.api, self.json_urls = pages or {}, api or {}, json_urls or {}
        # Unlisted dates answer the way the live endpoint does for a date whose
        # lineup is not posted yet, date echo included.
        self.api_default = api_default or (200, None)
        self.calls, self.last = 0, None

    def get(self, url):
        return self.pages.get(url, (404, "nope"))

    def get_json(self, url):
        return self.json_urls.get(url, (404, ""))

    def post_form(self, url, data, referer=None):
        """Keyed by the requested date, mimicking the real lineup endpoint."""
        self.calls += 1
        self.last = (url, data)
        key = json.loads(data["json"])["date"]
        if key in self.api:
            return self.api[key]
        status, body = self.api_default
        if body is None:
            body = json.dumps({"show": {"html": "", "date": key}, "date": key})
        return status, body

    def reset(self):
        pass


PAGE_WITH_SEP10 = "<html>" + "x" * 3000 + \
    '<select><option value="2026-08-24">Aug 24</option>' \
    '<option value="2026-09-10">Thu Sep 10</option></select></html>'
PAGE_NO_DATES = "<html>" + "x" * 4000 + "<div id='app'>loading…</div></html>"



# Real fragments captured from the live API on 2026-08-16.
LINEUP_HTML = (
    '<div><div class="set-header"><span class="lineup-toggle" data-lineup-id="44040">+'
    '</span><div class="info"><h2><span class="bold">7:00 pm<span class="hide-mobile">'
    ' show</span></span><span class="divider">-</span><span class="title">Colin Quinn '
    'Returns to The CQ Room</span></h2></div></div><div class="lineup" '
    'data-set-content="44040"><div class="set-content"><div><img src="/x.jpg" '
    'alt="Nick Griffin&#039;s headshot"></div><div><p><span class="name">Nick Griffin'
    '</span> COMEDY CENTRAL</p></div></div><div class="set-content"><div><p>'
    '<span class="name">Colin Quinn</span> SNL</p></div></div>'
    '<a href="https://www.comedycellar.com/reservations-newyork/?showid=1787007600">'
    'Make A Reservation</a></div></div>'
)
NO_LINEUP_HTML = '<p class="no-shows">No Comedians added yet!</p>'


def api_doc(day, html):
    """Exactly the envelope the live endpoint returns, date echo included."""
    return json.dumps({"show": {"html": html, "date": day.strftime("%A %B %d, %Y")},
                       "date": day.isoformat()})


class TestClassifyLineup(unittest.TestCase):
    def test_lineup_html_yields_comedians_showtimes_and_booking_ids(self):
        info = sources.classify_lineup(LINEUP_HTML)
        self.assertEqual(info["status"], sources.LINEUP)
        self.assertEqual(info["comedians"], ["Nick Griffin", "Colin Quinn"])
        self.assertEqual(info["showtimes"], ["7:00 pm"])
        self.assertEqual(info["titles"], ["Colin Quinn Returns to The CQ Room"])
        self.assertEqual(info["show_ids"], ["1787007600"])

    def test_no_comedians_yet_is_its_own_state(self):
        info = sources.classify_lineup(NO_LINEUP_HTML)
        self.assertEqual(info["status"], sources.NO_LINEUP)
        self.assertEqual(info["comedians"], [])

    def test_empty_means_not_listed(self):
        self.assertEqual(sources.classify_lineup("")["status"], sources.NOT_LISTED)

    def test_showtimes_without_names(self):
        html = '<div class="set-header"><h2><span class="bold">9:30 pm show</span></h2></div>'
        self.assertEqual(sources.classify_lineup(html)["status"], sources.SHOWTIMES)

    def test_unrecognised_html_never_alerts(self):
        info = sources.classify_lineup("<div>site redesign</div>")
        self.assertEqual(info["status"], sources.UNKNOWN)
        self.assertLess(sources.RANK[info["status"]], sources.ALERT_RANK)

    def test_ranks_are_ordered(self):
        r = sources.RANK
        self.assertLess(r[sources.NOT_LISTED], r[sources.NO_LINEUP])
        self.assertLess(r[sources.NO_LINEUP], r[sources.SHOWTIMES])
        self.assertLess(r[sources.SHOWTIMES], r[sources.LINEUP])


class TestLineupApi(unittest.TestCase):
    def test_status_per_target_date(self):
        http = FakeHttp(api={SEP10.isoformat(): (200, api_doc(SEP10, LINEUP_HTML)),
                             SEP11.isoformat(): (200, api_doc(SEP11, NO_LINEUP_HTML))})
        s = from_lineup_api(http, [SEP10, SEP11], TODAY)[0]
        self.assertTrue(s.ok)
        self.assertEqual(s.day_status[SEP10]["status"], sources.LINEUP)
        self.assertEqual(s.day_status[SEP11]["status"], sources.NO_LINEUP)
        self.assertIn(SEP10, s.target_hits)      # actionable
        self.assertNotIn(SEP11, s.target_hits)   # nothing to do yet

    def test_answer_about_the_wrong_date_is_discarded(self):
        """A stale or ignored date parameter must never page the user."""
        wrong = json.dumps({"show": {"html": LINEUP_HTML,
                                     "date": "Sunday August 16, 2026"},
                            "date": TODAY.isoformat()})
        s = from_lineup_api(FakeHttp(api={SEP10.isoformat(): (200, wrong)}),
                            [SEP10], TODAY)[0]
        self.assertEqual(s.day_status, {})
        self.assertEqual(s.target_hits, {})
        self.assertIn("not 2026-09-10", s.detail)

    def test_response_without_a_date_echo_is_not_trusted(self):
        http = FakeHttp(api={SEP10.isoformat():
                             (200, json.dumps({"show": {"html": LINEUP_HTML}}))})
        s = from_lineup_api(http, [SEP10], TODAY)[0]
        self.assertEqual(s.target_hits, {})
        self.assertIn("did not say which date", s.detail)

    def test_prose_date_echo_is_accepted(self):
        doc = json.dumps({"show": {"html": LINEUP_HTML,
                                   "date": "Thursday September 10, 2026"}})
        s = from_lineup_api(FakeHttp(api={SEP10.isoformat(): (200, doc)}),
                            [SEP10], TODAY)[0]
        self.assertEqual(s.day_status[SEP10]["status"], sources.LINEUP)

    def test_request_matches_the_captured_contract(self):
        http = FakeHttp()
        from_lineup_api(http, [SEP10], TODAY)
        url, data = http.last
        self.assertEqual(url, sources.LINEUP_API_URL)
        self.assertEqual(data["action"], "cc_get_shows")
        self.assertEqual(json.loads(data["json"]),
                         {"date": "2026-09-10", "venue": "newyork", "type": "lineup"})

    def test_unreachable_endpoint_is_unhealthy(self):
        s = from_lineup_api(FakeHttp(api_default=(0, "")), TARGETS, TODAY)[0]
        self.assertFalse(s.ok)

    def test_api_is_not_authoritative_for_the_date_through(self):
        """It is only asked about targets, so it must not define the horizon."""
        self.assertFalse(from_lineup_api(FakeHttp(), TARGETS, TODAY)[0].authoritative)



JS_PAGE = """<!doctype html><html><body><div id="app">loading...</div>
<script>
fetch('/api/offsets').then(function (r) { return r.json(); }).then(function (j) {
  var out = '<select id="d">';
  j.offsets.forEach(function (o) {
    var dt = new Date(Date.UTC(2026, 7, 11));
    dt.setUTCDate(dt.getUTCDate() + o);
    out += '<option value="' + dt.toISOString().slice(0, 10) + '">day</option>';
  });
  document.getElementById('app').innerHTML = out + '</select><p>Shows at 7:30 pm</p>';
  return fetch('/api/extra');
}).then(function (r) { return r.json(); }).then(function (j) { window._x = j; });
</script></body></html>"""


class BrowserStubHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/offsets"):
            body, ctype = json.dumps({"offsets": [7, 30]}).encode(), "application/json"
        elif self.path.startswith("/api/extra"):
            body = json.dumps({"shows": [{"date": "2026-09-12"}]}).encode()
            ctype = "application/json"
        elif self.path.startswith("/gone"):
            body = b"<html>" + b"filler " * 400 + b"</html>"
            self.send_response(404)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        else:
            body, ctype = JS_PAGE.encode(), "text/html"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@unittest.skipUnless(sources.chromium_path() or os.environ.get("REQUIRE_BROWSER"),
                     "no local chromium build available")
class TestBrowserStrategy(unittest.TestCase):
    """Proves the browser strategy sees dates that raw HTML does not."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), BrowserStubHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/lineup"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_raw_html_has_no_dates_but_browser_does(self):
        # Preconditions: the static strategy is genuinely helpless on this page.
        self.assertEqual(extract_all_dates(clean_html(JS_PAGE), TODAY), set())

        s = from_browser(self.url, TARGETS, TODAY, settle_ms=2000)[0]
        self.assertTrue(s.ok, s.detail)
        self.assertIn(date(2026, 8, 18), s.dates)   # JS-computed, DOM only
        self.assertIn(SEP10, s.dates)               # JS-computed target date
        self.assertIn(SEP10, s.target_hits)
        self.assertIn(SEP12, s.dates)               # only ever in an XHR payload
        self.assertTrue(any("/api/offsets" in u for u in s.xhr_urls), s.xhr_urls)

    def test_horizon_from_browser_render(self):
        s = from_browser(self.url, TARGETS, TODAY, settle_ms=2000)[0]
        self.assertEqual(horizon_of(s.dates, TODAY), SEP12)

    def test_api_samples_capture_request_and_response(self):
        """The captured contract is what lets us drop the browser later."""
        s = from_browser(self.url, TARGETS, TODAY, settle_ms=2000)[0]
        offsets = [a for a in s.api_samples if "/api/offsets" in a["url"]]
        self.assertTrue(offsets, s.api_samples)
        self.assertEqual(offsets[0]["method"], "GET")
        self.assertEqual(offsets[0]["status"], 200)
        self.assertIn("offsets", offsets[0]["body_head"])

    def test_cross_origin_xhr_is_not_captured(self):
        """Ad/analytics traffic must not pollute the captured API contract."""
        s = from_browser(self.url, TARGETS, TODAY, settle_ms=2000)[0]
        hosts = {u.split("//")[1].split("/")[0].split(":")[0]
                 for u in s.xhr_urls if "//" in u}
        self.assertEqual(hosts, {"127.0.0.1"}, s.xhr_urls)

    def test_navigation_failure_is_not_counted_as_a_render(self):
        """Chromium's error page is big; it must never look like a successful read."""
        s = from_browser("http://127.0.0.1:9/nope", TARGETS, TODAY, timeout_s=8)[0]
        self.assertFalse(s.ok)
        self.assertEqual(s.dates, set())
        self.assertIn("navigation failed", s.detail)

    def test_http_404_page_is_not_counted_as_a_render(self):
        """A 404 body large enough to look like a page must still count as failure."""
        s = from_browser(self.url.replace("/lineup", "/gone"), TARGETS, TODAY,
                         settle_ms=200)[0]
        self.assertFalse(s.ok)
        self.assertIn("navigation failed", s.detail)


class TestDiscoveryOrchestration(unittest.TestCase):
    def test_browser_skipped_when_lineup_html_has_dates(self):
        cfg = make_cfg(watch_urls=[sources.LINEUP_URL])
        http = FakeHttp(pages={sources.LINEUP_URL: (200, PAGE_WITH_SEP10)})
        with mock.patch.object(sources, "from_browser") as browser:
            res = perform_check(cfg, http)
        browser.assert_not_called()
        self.assertEqual(res.horizon, SEP10)
        self.assertIn(SEP10, res.found)

    def test_browser_used_when_html_has_no_dates(self):
        cfg = make_cfg(watch_urls=[sources.LINEUP_URL])
        http = FakeHttp(pages={sources.LINEUP_URL: (200, PAGE_NO_DATES)})
        fake = Sighting("browser", ok=True, detail="browser stub",
                        dates={date(2026, 8, 18)})
        with mock.patch.object(sources, "from_browser", return_value=[fake]) as browser:
            res = perform_check(cfg, http)
        browser.assert_called_once()
        self.assertEqual(res.horizon, date(2026, 8, 18))
        self.assertEqual(res.horizon_source, "browser")

    def test_no_strategy_works_means_unknown_horizon(self):
        """Total blackout: pages 404 and the API is unreachable."""
        res = perform_check(make_cfg(watch_urls=["u"], use_browser=False),
                            FakeHttp(api_default=(0, "")))
        self.assertIsNone(res.horizon)
        self.assertFalse(res.ok)

    def test_reachable_api_with_no_shows_still_leaves_horizon_unknown(self):
        """A healthy API that reports no shows is not a date-through."""
        res = perform_check(make_cfg(watch_urls=["u"], use_browser=False), FakeHttp())
        self.assertTrue(res.ok)          # the endpoint answered
        self.assertIsNone(res.horizon)   # but nothing tells us how far it lists

    def test_stray_homepage_date_cannot_suppress_the_browser(self):
        """A date in homepage copy must not skip the render nor set the horizon."""
        homepage = "<html>" + "x" * 3000 + "<p>Our anniversary gala, December 1</p></html>"
        cfg = make_cfg(watch_urls=[sources.LINEUP_URL, "https://www.comedycellar.com/"])
        http = FakeHttp(pages={sources.LINEUP_URL: (200, PAGE_NO_DATES),
                               "https://www.comedycellar.com/": (200, homepage)})
        fake = Sighting("browser", ok=True, detail="browser stub",
                        dates={date(2026, 8, 18)})
        with mock.patch.object(sources, "from_browser", return_value=[fake]) as browser:
            res = perform_check(cfg, http)
        browser.assert_called_once()
        self.assertEqual(res.horizon, date(2026, 8, 18))   # not December 1

    def test_target_date_on_homepage_still_alerts(self):
        """Non-authoritative pages don't set the horizon but can still prove a date."""
        homepage = "<html>" + "x" * 3000 + "<p>Tickets for September 11 are live</p></html>"
        cfg = make_cfg(watch_urls=[sources.LINEUP_URL, "https://www.comedycellar.com/"])
        http = FakeHttp(pages={sources.LINEUP_URL: (200, PAGE_NO_DATES),
                               "https://www.comedycellar.com/": (200, homepage)})
        fake = Sighting("browser", ok=True, detail="stub", dates={date(2026, 8, 18)})
        with mock.patch.object(sources, "from_browser", return_value=[fake]):
            res = perform_check(cfg, http)
        self.assertIn(SEP11, res.found)
        self.assertEqual(res.horizon, date(2026, 8, 18))


def result(horizon=None, source="static", found=(), ok=True):
    sights = [Sighting("static", ok=ok, detail="static x",
                       dates={horizon} if horizon else set())]
    res = CheckResult(sightings=sights, horizon=horizon, horizon_source=source)
    for d in found:
        res.add(d, "seen")
    return res


class TestHorizonGuarantee(unittest.TestCase):
    def test_unknown_horizon_alerts_then_recovers(self):
        cfg, state = make_cfg(horizon_unknown_hours=1), checker.default_state()
        handle_horizon(cfg, state, result(None))
        self.assertEqual(state["pending"], [])                 # grace period
        state["horizon_unknown_since"] -= 2 * 3600
        handle_horizon(cfg, state, result(None))
        self.assertEqual(len(state["pending"]), 1)
        self.assertIn("CANNOT read any dates", state["pending"][0]["title"])
        handle_horizon(cfg, state, result(None))                # no spam
        self.assertEqual(len(state["pending"]), 1)
        handle_horizon(cfg, state, result(date(2026, 8, 18)))
        self.assertIn("can read dates again", state["pending"][1]["title"])
        self.assertIsNone(state["horizon_unknown_since"])
        self.assertEqual(state["horizon"], "2026-08-18")

    def test_stalled_horizon_alerts(self):
        cfg, state = make_cfg(horizon_stall_days=3), checker.default_state()
        handle_horizon(cfg, state, result(date(2026, 8, 18)))
        self.assertEqual(state["pending"], [])
        state["horizon_changed_ts"] -= 4 * 86400
        handle_horizon(cfg, state, result(date(2026, 8, 18)))
        self.assertEqual(len(state["pending"]), 1)
        self.assertIn("STUCK", state["pending"][0]["title"])

    def test_advancing_horizon_never_alerts(self):
        cfg, state = make_cfg(horizon_stall_days=3), checker.default_state()
        for day in range(18, 26):
            handle_horizon(cfg, state, result(date(2026, 8, day)))
            state["horizon_changed_ts"] -= 86400
        self.assertEqual(state["pending"], [])

    def test_status_lines_flag_broken_state(self):
        cfg, state = make_cfg(), checker.default_state()
        self.assertIn("UNKNOWN", "\n".join(status_lines(cfg, state, result(None))))
        good = "\n".join(status_lines(cfg, state, result(date(2026, 8, 18))))
        self.assertIn("August 18, 2026", good)


def status_result(by_date=None):
    """CheckResult carrying per-date statuses, as the API strategy produces them."""
    res = CheckResult(sightings=[Sighting("lineup-api", ok=True, detail="stub",
                                          authoritative=False)])
    for d, status in (by_date or {}).items():
        info = sources.classify_lineup(
            LINEUP_HTML if status == sources.LINEUP else
            '<div class="set-header"><span class="bold">9:30 pm show</span></div>'
            if status == sources.SHOWTIMES else
            NO_LINEUP_HTML if status == sources.NO_LINEUP else "")
        res.day_status[d] = info
    return res


class TestDayStatusAlerts(unittest.TestCase):
    """What the user actually asked for: tell me when comedians are announced."""

    def test_no_lineup_baseline_is_silent(self):
        """Deploying while the dates say 'no comedians yet' must not spam."""
        cfg, state = make_cfg(), checker.default_state()
        handle_day_status(cfg, state, status_result({SEP10: sources.NO_LINEUP}))
        self.assertEqual(state["pending"], [])
        self.assertEqual(state["day_status"]["2026-09-10"]["status"], sources.NO_LINEUP)

    def test_lineup_announced_alerts_with_names(self):
        cfg, state = make_cfg(), checker.default_state()
        handle_day_status(cfg, state, status_result({SEP10: sources.NO_LINEUP}))
        handle_day_status(cfg, state, status_result({SEP10: sources.LINEUP}))
        self.assertEqual(len(state["pending"]), 1)
        alert = state["pending"][0]
        self.assertEqual(alert["level"], "alert")
        self.assertIn("LINEUP UP", alert["title"])
        self.assertIn("Nick Griffin", alert["title"])
        self.assertIn("Colin Quinn", alert["body"])
        self.assertIn("showid=1787007600", alert["body"])   # bookable link

    def test_showtimes_before_comedians_also_alerts(self):
        cfg, state = make_cfg(), checker.default_state()
        handle_day_status(cfg, state, status_result({SEP11: sources.NO_LINEUP}))
        handle_day_status(cfg, state, status_result({SEP11: sources.SHOWTIMES}))
        self.assertEqual(len(state["pending"]), 1)
        self.assertIn("showtimes posted", state["pending"][0]["title"])
        # ...and upgrading to a full lineup afterwards alerts again.
        handle_day_status(cfg, state, status_result({SEP11: sources.LINEUP}))
        self.assertEqual(len(state["pending"]), 2)
        self.assertIn("LINEUP UP", state["pending"][1]["title"])

    def test_steady_state_does_not_repeat(self):
        cfg, state = make_cfg(alert_repeats=0), checker.default_state()
        handle_day_status(cfg, state, status_result({SEP10: sources.NO_LINEUP}))
        handle_day_status(cfg, state, status_result({SEP10: sources.LINEUP}))
        for _ in range(5):
            handle_day_status(cfg, state, status_result({SEP10: sources.LINEUP}))
        self.assertEqual(len(state["pending"]), 1)

    def test_lineup_already_up_on_first_sight_still_alerts(self):
        """A lineup posted while the watcher was down must not be missed."""
        cfg, state = make_cfg(), checker.default_state()
        handle_day_status(cfg, state, status_result({SEP12: sources.LINEUP}))
        self.assertEqual(len(state["pending"]), 1)
        self.assertIn("already posted", state["pending"][0]["title"])

    def test_going_backwards_neither_alerts_nor_rearms(self):
        """A blip must not fire, and recovering from it must not re-fire."""
        cfg, state = make_cfg(alert_repeats=0), checker.default_state()
        handle_day_status(cfg, state, status_result({SEP10: sources.NO_LINEUP}))
        handle_day_status(cfg, state, status_result({SEP10: sources.LINEUP}))
        self.assertEqual(len(state["pending"]), 1)
        handle_day_status(cfg, state, status_result({SEP10: sources.NO_LINEUP}))
        self.assertEqual(len(state["pending"]), 1)                     # no alert
        self.assertEqual(state["day_status"]["2026-09-10"]["status"], sources.LINEUP)
        handle_day_status(cfg, state, status_result({SEP10: sources.LINEUP}))
        self.assertEqual(len(state["pending"]), 1)                     # no re-fire

    def test_unknown_status_never_alerts(self):
        cfg, state = make_cfg(), checker.default_state()
        res = CheckResult(sightings=[Sighting("lineup-api", ok=True)])
        res.day_status[SEP10] = sources.classify_lineup("<div>redesign</div>")
        handle_day_status(cfg, state, res)
        self.assertEqual(state["pending"], [])

    def test_reminders_repeat_then_stop(self):
        cfg, state = make_cfg(alert_repeats=2), checker.default_state()
        handle_day_status(cfg, state, status_result({SEP10: sources.LINEUP}))
        rec = state["day_status"]["2026-09-10"]
        for expected in (2, 3):
            rec["last_alert_ts"] -= 31 * 60
            handle_day_status(cfg, state, status_result({SEP10: sources.LINEUP}))
            self.assertEqual(rec["alerts_sent"], expected)
        rec["last_alert_ts"] -= 31 * 60
        handle_day_status(cfg, state, status_result({SEP10: sources.LINEUP}))
        self.assertEqual(rec["alerts_sent"], 3)
        self.assertEqual(len(state["pending"]), 3)

    def test_heartbeat_reports_each_target_status(self):
        cfg, state = make_cfg(), checker.default_state()
        handle_day_status(cfg, state, status_result({SEP10: sources.LINEUP,
                                                       SEP11: sources.NO_LINEUP}))
        text = "\n".join(status_lines(cfg, state, status_result()))
        self.assertIn("Sep 10", text)
        self.assertIn("LINEUP ANNOUNCED", text)
        self.assertIn("Nick Griffin", text)
        self.assertIn("no comedians yet", text)

    def test_old_state_file_without_day_status_upgrades(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w") as fh:
                json.dump({"found": {"2026-09-10": {"first_seen": 1, "alerts_sent": 1,
                                                    "last_alert_ts": 1}},
                           "pending": [], "checks_total": 42}, fh)
            state = load_state(path)
            self.assertEqual(state["day_status"], {})
            handle_day_status(make_cfg(), state,
                              status_result({SEP10: sources.NO_LINEUP}))
            self.assertEqual(state["pending"], [])   # no duplicate of the old alert


class TestAlertFlow(unittest.TestCase):
    def test_new_dates_queue_one_alert(self):
        cfg, state = make_cfg(), checker.default_state()
        handle_check_result(cfg, state, result(SEP11, found=[SEP10, SEP11]))
        self.assertEqual(len(state["pending"]), 1)
        self.assertIn("Sep 10", state["pending"][0]["title"])
        self.assertEqual(state["pending"][0]["level"], "alert")
        handle_check_result(cfg, state, result(SEP11, found=[SEP10, SEP11]))
        self.assertEqual(len(state["pending"]), 1)

    def test_reminders_then_stop(self):
        cfg, state = make_cfg(alert_repeats=2), checker.default_state()
        handle_check_result(cfg, state, result(SEP10, found=[SEP10]))
        info = state["found"]["2026-09-10"]
        for expected in (2, 3):
            info["last_alert_ts"] -= 31 * 60
            handle_check_result(cfg, state, result(SEP10, found=[SEP10]))
            self.assertEqual(info["alerts_sent"], expected)
        info["last_alert_ts"] -= 31 * 60
        handle_check_result(cfg, state, result(SEP10, found=[SEP10]))
        self.assertEqual(info["alerts_sent"], 3)
        self.assertEqual(len(state["pending"]), 3)

    def test_watchdog_warns_and_recovers(self):
        cfg, state = make_cfg(failure_alert_hours=2), checker.default_state()
        handle_check_result(cfg, state, result(ok=False))
        self.assertEqual(state["pending"], [])
        state["fail_since"] -= 3 * 3600
        handle_check_result(cfg, state, result(ok=False))
        self.assertIn("BLIND", state["pending"][0]["title"])
        handle_check_result(cfg, state, result(date(2026, 8, 18)))
        self.assertIn("recovered", state["pending"][1]["title"])

    def test_alert_body_has_booking_link(self):
        body = alert_body({SEP10: ["seen"]}, make_cfg())
        self.assertIn("reservations-newyork", body)
        self.assertIn("September 10, 2026", body)


class TestStateAndQueue(unittest.TestCase):
    def test_roundtrip_and_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "state.json")
            st = checker.default_state()
            st["checks_total"] = 7
            enqueue(st, "t", "b", "info")
            save_state(path, st)
            self.assertEqual(load_state(path)["checks_total"], 7)
            self.assertEqual(len(load_state(path)["pending"]), 1)
            with open(path, "w") as fh:
                fh.write("{broken")
            self.assertEqual(load_state(path)["checks_total"], 0)

    def test_old_state_file_gains_new_keys(self):
        """A volume written by the previous version must not crash the new one."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w") as fh:
                json.dump({"found": {}, "pending": [], "checks_total": 187}, fh)
            st = load_state(path)
            self.assertEqual(st["checks_total"], 187)
            self.assertIn("horizon", st)
            handle_horizon(make_cfg(), st, result(date(2026, 8, 18)))
            self.assertEqual(st["horizon"], "2026-08-18")

    def test_flush_retries_until_delivered(self):
        cfg, state = make_cfg(), checker.default_state()
        enqueue(state, "t", "b", "alert")
        with mock.patch.object(checker, "send_all", side_effect=[False, True]) as m:
            flush_pending(cfg, state)
            self.assertGreater(state["pending"][0]["next_try"], time.time())
            flush_pending(cfg, state)
            self.assertEqual(m.call_count, 1)
            state["pending"][0]["next_try"] = 0
            flush_pending(cfg, state)
            self.assertEqual(state["pending"], [])

    def test_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(state_file=os.path.join(tmp, "state.json"))
            self.assertEqual(health(cfg), 1)
            save_state(cfg.state_file, checker.default_state())
            self.assertEqual(health(cfg), 0)
            old = time.time() - 7200
            os.utime(cfg.state_file, (old, old))
            self.assertEqual(health(cfg), 1)


class NotifyStub(BaseHTTPRequestHandler):
    hits, flaky = [], {"calls": 0}

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        NotifyStub.hits.append((self.path, dict(self.headers), self.rfile.read(n)))
        if self.path == "/flaky":
            NotifyStub.flaky["calls"] += 1
            code = 500 if NotifyStub.flaky["calls"] == 1 else 200
        else:
            code = 204 if self.path.startswith("/discord") else 200
        self.send_response(code)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


class TestRealChannels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), NotifyStub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_fanout_and_retry(self):
        NotifyStub.hits, NotifyStub.flaky = [], {"calls": 0}
        cfg = make_cfg(dry_run=False, ntfy_server=self.base, ntfy_topic="mytopic",
                       discord_webhook=self.base + "/discord",
                       webhook_url=self.base + "/flaky")
        self.assertTrue(send_all(cfg, "Test title", "hello body", "alert"))
        paths = [h[0] for h in NotifyStub.hits]
        self.assertIn("/mytopic", paths)
        self.assertIn("/discord", paths)
        self.assertEqual(paths.count("/flaky"), 2)
        ntfy = next(h for h in NotifyStub.hits if h[0] == "/mytopic")
        self.assertEqual(ntfy[1].get("Priority"), "urgent")
        self.assertIn(b"hello body", ntfy[2])

    def test_no_channels_fails_loud(self):
        self.assertFalse(send_all(make_cfg(dry_run=False), "t", "b", "info"))


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = checker.load_config()
        self.assertEqual(cfg.targets, TARGETS)
        self.assertTrue(cfg.use_browser)
        self.assertIn(sources.RESERVATIONS_URL, cfg.watch_urls)

    def test_overrides(self):
        env = {"TARGET_DATES": "2026-12-31", "CHECK_INTERVAL_SECONDS": "30",
               "NTFY_TOPIC": "off", "USE_BROWSER": "0"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = checker.load_config()
        self.assertEqual(cfg.targets, [date(2026, 12, 31)])
        self.assertEqual(cfg.check_interval, 60)
        self.assertEqual(cfg.ntfy_topic, "")
        self.assertFalse(cfg.use_browser)


if __name__ == "__main__":
    unittest.main()
