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
                     handle_check_result, handle_horizon, health, load_state,
                     perform_check, save_state, send_all, status_lines)
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


EMPTY_API = json.dumps({"show": {"html": ""}})


def shows_api(html='<div class="set-header"><h2>7:00 pm show</h2></div>'):
    """Shaped like the real response captured from the live site."""
    return json.dumps({"show": {"html": html}})


class FakeHttp:
    def __init__(self, pages=None, api=None, json_urls=None, api_default=None):
        self.pages, self.api, self.json_urls = pages or {}, api or {}, json_urls or {}
        # Unlisted dates answer like the real endpoint does for a date with no
        # lineup yet — including the sentinel the trust guard queries.
        self.api_default = api_default or (200, EMPTY_API)
        self.calls, self.last = 0, None

    def get(self, url):
        return self.pages.get(url, (404, "nope"))

    def get_json(self, url):
        return self.json_urls.get(url, (404, ""))

    def post_form(self, url, data, referer=None):
        """Keyed by the requested date, mimicking the real lineup endpoint."""
        self.calls += 1
        self.last = (url, data)
        return self.api.get(json.loads(data["json"])["date"], self.api_default)

    def reset(self):
        pass


PAGE_WITH_SEP10 = "<html>" + "x" * 3000 + \
    '<select><option value="2026-08-24">Aug 24</option>' \
    '<option value="2026-09-10">Thu Sep 10</option></select></html>'
PAGE_NO_DATES = "<html>" + "x" * 4000 + "<div id='app'>loading…</div></html>"


class TestStrategies(unittest.TestCase):
    def test_static_finds_target_and_dates(self):
        s = from_static(FakeHttp(pages={"u": (200, PAGE_WITH_SEP10)}),
                        ["u"], TARGETS, TODAY)[0]
        self.assertTrue(s.ok)
        self.assertIn(SEP10, s.target_hits)
        self.assertIn(date(2026, 8, 24), s.dates)

    def test_static_reports_404(self):
        s = from_static(FakeHttp(), ["u"], TARGETS, TODAY)[0]
        self.assertFalse(s.ok)
        self.assertIn("404", s.detail)

    def test_static_js_page_yields_nothing(self):
        """The real failure we hit: page loads fine, contains zero dates."""
        s = from_static(FakeHttp(pages={"u": (200, PAGE_NO_DATES)}),
                        ["u"], TARGETS, TODAY)[0]
        self.assertTrue(s.ok)
        self.assertEqual(s.dates, set())

    def test_rest_parses_events_json(self):
        url = sources.REST_ENDPOINTS[0].format(start=TODAY.isoformat())
        body = json.dumps({"events": [{"start_date": "2026-09-11 19:00:00",
                                       "title": "Late Show"}]})
        out = from_rest(FakeHttp(json_urls={url: (200, body)}), TARGETS, TODAY)
        hit = [s for s in out if s.ok]
        self.assertTrue(hit)
        self.assertIn(SEP11, hit[0].target_hits)

    def test_rest_ignores_html_error_page(self):
        url = sources.REST_ENDPOINTS[0].format(start=TODAY.isoformat())
        out = from_rest(FakeHttp(json_urls={url: (200, "<html>nope</html>")}),
                        TARGETS, TODAY)
        self.assertFalse(any(s.ok for s in out))

class TestLineupApi(unittest.TestCase):
    """The endpoint never echoes the date, so both controls are load-bearing."""

    def setUp(self):
        sources._CALIBRATION.clear()

    def _http(self, **kw):
        """An endpoint that honours ISO dates for anything within `window` days."""
        window = kw.pop("window", 30)
        api = {}
        for off in range(-1, window + 1):
            api[(TODAY + timedelta(days=off)).isoformat()] = (200, shows_api())
        api.update(kw.pop("api", {}))
        return FakeHttp(api=api, **kw)

    def test_calibration_accepts_a_working_encoding(self):
        encoder, detail = sources.calibrate_lineup_api(self._http(), TODAY)
        self.assertIsNotNone(encoder)
        self.assertEqual(encoder(SEP10), "2026-09-10")
        self.assertIn("verified", detail)

    def test_shows_for_a_target_is_a_hit(self):
        # Window wide enough to include the targets (31 days out).
        s = from_lineup_api(self._http(window=40), TARGETS, TODAY)[0]
        self.assertTrue(s.ok)
        self.assertEqual(set(s.target_hits), set(TARGETS))

    def test_targets_outside_the_window_are_not_hits(self):
        s = from_lineup_api(self._http(window=20), TARGETS, TODAY)[0]
        self.assertTrue(s.ok)
        self.assertEqual(s.target_hits, {})

    def test_endpoint_that_never_says_yes_declares_itself_useless(self):
        """The live failure: ISO dates always answer 'no shows'.

        The negative control alone passes here, so without a positive control
        this would look healthy while being incapable of ever detecting a date.
        """
        s = from_lineup_api(FakeHttp(api_default=(200, EMPTY_API)), TARGETS, TODAY)[0]
        self.assertTrue(s.ok)
        self.assertEqual(s.target_hits, {})
        self.assertIn("contributes nothing", s.detail)
        self.assertIn("no date encoding works", s.detail)

    def test_endpoint_that_ignores_the_date_is_rejected(self):
        """Says yes to everything, including a date 300 days out."""
        s = from_lineup_api(FakeHttp(api_default=(200, shows_api())), TARGETS, TODAY)[0]
        self.assertTrue(s.ok)
        self.assertEqual(s.target_hits, {})     # would otherwise be 3 false alarms
        self.assertEqual(s.dates, set())
        self.assertIn("echoes", s.detail)

    def test_unreachable_endpoint_is_reported(self):
        s = from_lineup_api(FakeHttp(api_default=(0, "")), TARGETS, TODAY)[0]
        self.assertFalse(s.ok)
        self.assertIn("unreachable", s.detail)

    def test_outage_after_good_calibration_reports_unhealthy(self):
        """A cached calibration must not mask a dead endpoint from the watchdog."""
        sources.calibrate_lineup_api(self._http(), TODAY)      # calibrate while up
        s = from_lineup_api(FakeHttp(api_default=(0, "")), TARGETS, TODAY)[0]
        self.assertFalse(s.ok)                                 # not "fine, no shows"
        self.assertEqual(sources._CALIBRATION, {})             # will re-verify

    def test_calibration_is_cached_per_day(self):
        http = self._http()
        sources.calibrate_lineup_api(http, TODAY)
        before = http.calls
        sources.calibrate_lineup_api(http, TODAY)
        self.assertEqual(http.calls, before)          # no repeat probing
        sources.calibrate_lineup_api(http, TODAY + timedelta(days=1))
        self.assertGreater(http.calls, before)        # new day, re-verified

    def test_request_matches_the_captured_contract(self):
        http = self._http()
        from_lineup_api(http, [SEP10], TODAY)
        url, data = http.last
        self.assertEqual(url, sources.LINEUP_API_URL)
        self.assertEqual(data["action"], "cc_get_shows")
        self.assertEqual(json.loads(data["json"]),
                         {"date": SEP10.isoformat(), "venue": "newyork",
                          "type": "lineup"})

    def test_query_accepts_today_keyword(self):
        http = FakeHttp(api={"today": (200, shows_api())})
        reachable, fragment, _ = sources.query_lineup_api(http, "today")
        self.assertTrue(reachable)
        self.assertTrue(sources.has_shows(fragment))


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
    def setUp(self):
        sources._CALIBRATION.clear()   # process-global by design; isolate tests

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
