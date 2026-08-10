#!/usr/bin/env python3
"""Tests: python3 -m unittest test_checker -v"""

import json
import os
import tempfile
import threading
import time
import unittest
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import checker
from checker import (
    CheckResult, Config, alert_body, clean_html, enqueue, extract_all_dates,
    flush_pending, handle_check_result, health, load_state, patterns_for_date,
    perform_check, probe_ajax, save_state, send_all, text_mentions_date,
)

SEP10 = date(2026, 9, 10)
SEP11 = date(2026, 9, 11)
SEP12 = date(2026, 9, 12)


def make_cfg(**kw):
    cfg = Config()
    cfg.targets = [SEP10, SEP11, SEP12]
    cfg.tz = "UTC"
    cfg.dry_run = True
    cfg.ntfy_topic = ""
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


class TestDateMatching(unittest.TestCase):
    def test_positive_formats(self):
        for text in [
            'value="2026-09-10"',
            "showtime 2026-09-10T19:00:00",
            "2026/09/10",
            "on 9/10/2026 at 7pm",
            "09-10-26",
            "Thursday, September 10",
            "Thursday September 10th, 2026",
            "SEPT. 10",
            "sep 10",
            "September&nbsp;10",       # entity, goes through clean_html
            '{"date":"2026-09-10","shows":[]}',
        ]:
            self.assertTrue(text_mentions_date(clean_html(text), SEP10), text)

    def test_negative_formats(self):
        for text in [
            "September 10, 2025",      # wrong year
            "2025-09-10",
            "September 100 jokes",
            "rated 9/10 by fans",      # bare numeric needs a year
            "September 11",            # different day
            "1026-09-10x2",
            "josep 10",
        ]:
            self.assertFalse(text_mentions_date(clean_html(text), SEP10), text)

    def test_range_text_matches_first_day(self):
        self.assertTrue(text_mentions_date("September 10-12 shows on sale", SEP10))

    def test_extract_all_dates(self):
        today = date(2026, 8, 10)
        text = clean_html(
            'options: <option value="2026-08-11">Tue</option> '
            '<option value="2026-08-24">Aug 24</option> '
            "blog from January 5, 2020 ... next show September 3"
        )
        got = extract_all_dates(text, today)
        self.assertIn(date(2026, 8, 11), got)
        self.assertIn(date(2026, 8, 24), got)
        self.assertIn(date(2026, 9, 3), got)      # yearless resolves forward
        self.assertNotIn(date(2020, 1, 5), got)   # far past is dropped


class FakeHttp:
    def __init__(self, pages=None, ajax=None):
        self.pages = pages or {}
        self.ajax = ajax or {}

    def get(self, url):
        return self.pages.get(url, (404, "nope"))

    def post_form(self, url, data, referer):
        target = json.loads(data["json"])["date"]
        return self.ajax.get(target, (200, "0"))

    def reset(self):
        pass


PAGE_WITH_SEP10 = "<html>" + "x" * 3000 + \
    '<select><option value="2026-08-24">Mon Aug 24</option>' \
    '<option value="2026-09-10">Thu Sep 10</option></select></html>'
PAGE_PLAIN = "<html>" + "x" * 3000 + "<p>shows through August 24</p></html>"


class TestSiteCheck(unittest.TestCase):
    def test_page_scan_finds_target(self):
        cfg = make_cfg()
        http = FakeHttp(pages={u: (200, PAGE_WITH_SEP10) for u in cfg.watch_urls})
        res = perform_check(cfg, http)
        self.assertIn(SEP10, res.found)
        self.assertNotIn(SEP11, res.found)
        self.assertTrue(res.ok)
        self.assertIn(date(2026, 8, 24), res.all_dates)

    def test_ajax_confirms(self):
        cfg = make_cfg()
        show_json = json.dumps({"show": {"date": "2026-09-11",
                                         "html": "early show 7:00 pm lineup"}})
        http = FakeHttp(pages={u: (200, PAGE_PLAIN) for u in cfg.watch_urls},
                        ajax={"2026-09-11": (200, show_json)})
        res = perform_check(cfg, http)
        self.assertIn(SEP11, res.found)
        self.assertIn(SEP11, res.confirmed)

    def test_probe_ajax_unsupported_action(self):
        http = FakeHttp()   # always answers "0"
        status, _ = probe_ajax(http, SEP10)
        self.assertEqual(status, "unavailable")

    def test_probe_ajax_negative(self):
        neg = json.dumps({"show": {"shows": [], "note": "no shows scheduled"}})
        http = FakeHttp(ajax={"2026-09-10": (200, neg)})
        status, _ = probe_ajax(http, SEP10)
        self.assertEqual(status, "negative")

    def test_all_down_is_not_ok(self):
        cfg = make_cfg()
        res = perform_check(cfg, FakeHttp())
        self.assertFalse(res.ok)
        self.assertTrue(res.errors)


class TestAlertFlow(unittest.TestCase):
    def _found_result(self, *dates):
        res = CheckResult(pages_ok=2, api_alive=True)
        for d in dates:
            res.add(d, "seen on page")
        return res

    def test_new_date_queues_one_combined_alert(self):
        cfg, state = make_cfg(), checker.default_state()
        handle_check_result(cfg, state, self._found_result(SEP10, SEP11))
        self.assertEqual(len(state["pending"]), 1)
        self.assertIn("Sep 10", state["pending"][0]["title"])
        self.assertIn("Sep 11", state["pending"][0]["title"])
        self.assertEqual(state["pending"][0]["level"], "alert")
        # second sighting: no duplicate alert
        handle_check_result(cfg, state, self._found_result(SEP10, SEP11))
        self.assertEqual(len(state["pending"]), 1)

    def test_reminders_then_stop(self):
        cfg, state = make_cfg(alert_repeats=2, alert_repeat_minutes=30), checker.default_state()
        handle_check_result(cfg, state, self._found_result(SEP10))
        info = state["found"]["2026-09-10"]
        for expected in (2, 3):   # two reminders fire once overdue
            info["last_alert_ts"] -= 31 * 60
            handle_check_result(cfg, state, self._found_result(SEP10))
            self.assertEqual(info["alerts_sent"], expected)
        info["last_alert_ts"] -= 31 * 60
        handle_check_result(cfg, state, self._found_result(SEP10))
        self.assertEqual(info["alerts_sent"], 3)          # capped
        self.assertEqual(len(state["pending"]), 3)        # 1 alert + 2 reminders

    def test_watchdog_warns_and_recovers(self):
        cfg, state = make_cfg(failure_alert_hours=2), checker.default_state()
        bad = CheckResult()
        bad.errors = ["GET x -> HTTP 0"]
        handle_check_result(cfg, state, bad)
        self.assertEqual(state["pending"], [])            # too early to warn
        state["fail_since"] -= 3 * 3600
        handle_check_result(cfg, state, bad)
        self.assertEqual(len(state["pending"]), 1)
        self.assertIn("BLIND", state["pending"][0]["title"])
        handle_check_result(cfg, state, bad)              # no re-warn before 12h
        self.assertEqual(len(state["pending"]), 1)
        handle_check_result(cfg, state, self._found_result())   # site back
        self.assertEqual(len(state["pending"]), 2)
        self.assertIn("recovered", state["pending"][1]["title"])
        self.assertIsNone(state["fail_since"])

    def test_alert_body_mentions_booking_link(self):
        body = alert_body({SEP10: ["seen on page"]}, make_cfg())
        self.assertIn("reservations", body)
        self.assertIn("September 10, 2026", body)


class TestStateAndQueue(unittest.TestCase):
    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "state.json")
            state = checker.default_state()
            state["checks_total"] = 7
            enqueue(state, "t", "b", "info")
            save_state(path, state)
            loaded = load_state(path)
            self.assertEqual(loaded["checks_total"], 7)
            self.assertEqual(len(loaded["pending"]), 1)

    def test_corrupt_state_starts_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w") as fh:
                fh.write("{broken")
            self.assertEqual(load_state(path)["checks_total"], 0)

    def test_flush_retries_until_delivered(self):
        cfg, state = make_cfg(), checker.default_state()
        enqueue(state, "t", "b", "alert")
        with mock.patch.object(checker, "send_all", side_effect=[False, True]) as m:
            flush_pending(cfg, state)                     # fails -> backoff
            self.assertEqual(len(state["pending"]), 1)
            self.assertGreater(state["pending"][0]["next_try"], time.time())
            flush_pending(cfg, state)                     # not due yet
            self.assertEqual(m.call_count, 1)
            state["pending"][0]["next_try"] = 0
            flush_pending(cfg, state)                     # succeeds
            self.assertEqual(state["pending"], [])

    def test_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(state_file=os.path.join(tmp, "state.json"))
            self.assertEqual(health(cfg), 1)              # missing file
            save_state(cfg.state_file, checker.default_state())
            self.assertEqual(health(cfg), 0)
            old = time.time() - 7200
            os.utime(cfg.state_file, (old, old))
            self.assertEqual(health(cfg), 1)


class StubHandler(BaseHTTPRequestHandler):
    hits = []
    flaky_state = {"calls": 0}

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        StubHandler.hits.append((self.path, dict(self.headers), body))
        if self.path == "/flaky":
            StubHandler.flaky_state["calls"] += 1
            code = 500 if StubHandler.flaky_state["calls"] == 1 else 200
        elif self.path.startswith("/discord"):
            code = 204
        else:
            code = 200
        self.send_response(code)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


class TestRealChannels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_fanout_and_retry(self):
        StubHandler.hits, StubHandler.flaky_state = [], {"calls": 0}
        cfg = make_cfg(dry_run=False,
                       ntfy_server=self.base, ntfy_topic="mytopic",
                       discord_webhook=self.base + "/discord",
                       webhook_url=self.base + "/flaky")
        ok = send_all(cfg, "Test title", "hello body", "alert")
        self.assertTrue(ok)
        paths = [h[0] for h in StubHandler.hits]
        self.assertIn("/mytopic", paths)
        self.assertIn("/discord", paths)
        self.assertEqual(paths.count("/flaky"), 2)        # 500 then retried -> 200
        ntfy = next(h for h in StubHandler.hits if h[0] == "/mytopic")
        self.assertEqual(ntfy[1].get("Priority"), "urgent")
        self.assertEqual(ntfy[1].get("Title"), "Test title")
        self.assertIn(b"hello body", ntfy[2])

    def test_no_channels_fails_loud(self):
        cfg = make_cfg(dry_run=False)                     # every channel unset
        self.assertFalse(send_all(cfg, "t", "b", "info"))


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = checker.load_config()
        self.assertEqual(cfg.targets, [SEP10, SEP11, SEP12])
        self.assertTrue(cfg.ntfy_topic)
        self.assertEqual(cfg.check_interval, 300)

    def test_overrides(self):
        env = {"TARGET_DATES": "2026-12-31", "CHECK_INTERVAL_SECONDS": "30",
               "NTFY_TOPIC": "off"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = checker.load_config()
        self.assertEqual(cfg.targets, [date(2026, 12, 31)])
        self.assertEqual(cfg.check_interval, 60)          # clamped to minimum
        self.assertEqual(cfg.ntfy_topic, "")              # "off" disables


if __name__ == "__main__":
    unittest.main()
