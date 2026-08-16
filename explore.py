#!/usr/bin/env python3
"""Live exploration: how does the lineup page expose per-date content?

Run from CI, where the site is reachable:
    python explore.py 2026-09-10 2026-09-11

Prints, for each date: how it was activated, the XHR it triggered (request body
included), and structural counts plus a text sample of the lineup that appeared.
Comparing a date that already has comedians against one that doesn't is what
tells us which markers distinguish "show scheduled" from "lineup announced".

This is a throwaway investigation tool, not part of the running service.
"""

import json
import sys
from datetime import date, datetime, timedelta

from playwright.sync_api import sync_playwright

import sources

# Elements whose attributes or text could name a date in the picker.
PICKER_JS = """
() => {
  const out = [];
  document.querySelectorAll('*').forEach(el => {
    if (el.children.length > 2) return;                 // leaf-ish only
    const attrs = {};
    for (const a of el.attributes || []) attrs[a.name] = a.value;
    const blob = JSON.stringify(attrs) + ' ' + (el.textContent || '').slice(0, 60);
    if (!/\\d{4}-\\d{2}-\\d{2}|\\d{1,2}\\/\\d{1,2}|(mon|tue|wed|thu|fri|sat|sun)/i.test(blob)) return;
    out.push({tag: el.tagName, attrs: attrs,
              text: (el.textContent || '').trim().slice(0, 40),
              html: el.outerHTML.slice(0, 200)});
  });
  return out.slice(0, 60);
}
"""

# What the lineup region currently shows.
SHAPE_JS = """
() => {
  const q = s => document.querySelectorAll(s).length;
  const names = [...document.querySelectorAll('.name')].map(e => e.textContent.trim());
  const heads = [...document.querySelectorAll('.set-header')].map(e =>
      e.textContent.replace(/\\s+/g, ' ').trim().slice(0, 90));
  const links = [...document.querySelectorAll('a')]
      .filter(a => /reserv|ticket|book|buy/i.test(a.textContent + ' ' + a.href))
      .map(a => (a.textContent.trim().slice(0, 40) + ' -> ' + a.href).slice(0, 140));
  return {
    counts: {
      setHeader: q('.set-header'), setContent: q('.set-content'),
      name: q('.name'), lineup: q('.lineup'), headshots: q('.set-content img'),
      lineupToggle: q('.lineup-toggle'),
    },
    headers: heads.slice(0, 8),
    names: names.slice(0, 25),
    ticketish: [...new Set(links)].slice(0, 6),
    bodySample: document.body.innerText.replace(/\\n{2,}/g, '\\n').slice(0, 1200),
  };
}
"""


def launch(p):
    opts = {"args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]}
    exe = sources.chromium_path()
    if exe:
        opts["executable_path"] = exe
    return p.chromium.launch(**opts)


def activate(page, target):
    """Try every plausible way to select `target` in the picker. Returns a note."""
    iso = target.isoformat()
    # 1. a <select> containing the date
    for sel in page.query_selector_all("select"):
        for opt in sel.query_selector_all("option"):
            if (opt.get_attribute("value") or "") == iso:
                sel.select_option(iso)
                return f"select_option({iso})"
    # 2. anything carrying the date in an attribute
    for attr in ("data-date", "data-day", "value", "href", "id"):
        el = page.query_selector(f'[{attr}*="{iso}"]')
        if el:
            try:
                el.click(timeout=5000)
                return f"clicked [{attr}*={iso}]"
            except Exception as exc:
                return f"found [{attr}*={iso}] but click failed: {exc}"
    # 3. by visible text, e.g. "Thu 10" / "Sep 10"
    for text in (target.strftime("%b %-d"), target.strftime("%-d"),
                 target.strftime("%a %-d")):
        try:
            el = page.get_by_text(text, exact=True).first
            el.click(timeout=4000)
            return f"clicked text {text!r}"
        except Exception:
            continue
    return "NO CONTROL FOUND"


def report(label, shape, xhr):
    print(f"\n=== {label} ===")
    print("  counts:", json.dumps(shape["counts"]))
    print("  headers:", json.dumps(shape["headers"], indent=None)[:400])
    print("  names:", json.dumps(shape["names"])[:400])
    print("  ticket-ish links:", json.dumps(shape["ticketish"])[:400])
    for r in xhr:
        print(f"  xhr {r['method']} {r['url']} [{r['status']}]")
        if r["post_data"]:
            print(f"      request: {r['post_data'][:300]}")
        print(f"      response head: {r['body'][:400]}")
    print("  body sample:")
    for line in shape["bodySample"].splitlines()[:22]:
        print("   |", line[:110])


def main(argv):
    today = datetime.now().date()
    targets = [datetime.strptime(a, "%Y-%m-%d").date() for a in argv[1:]] or [
        today + timedelta(days=25), today + timedelta(days=26)]
    # A near date is the control: it should already have comedians.
    controls = [today, today + timedelta(days=1), today + timedelta(days=3)]

    xhr = []

    def on_response(resp):
        try:
            if resp.request.resource_type not in ("xhr", "fetch"):
                return
            if "comedycellar.com" not in resp.url:
                return
            xhr.append({"method": resp.request.method, "url": resp.url,
                        "status": resp.status,
                        "post_data": resp.request.post_data or "",
                        "body": resp.text()[:1500]})
        except Exception:
            pass

    with sync_playwright() as p:
        browser = launch(p)
        page = browser.new_page(
            user_agent=sources.USER_AGENT, viewport={"width": 1400, "height": 1200})
        page.on("response", on_response)
        page.goto(sources.LINEUP_URL, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(4000)

        print("=" * 78)
        print(f"EXPLORATION {datetime.now():%Y-%m-%d %H:%M}  today={today}")
        print("=" * 78)
        print("\n--- date picker candidates (tag/attrs/text) ---")
        for c in page.evaluate(PICKER_JS)[:40]:
            print(f"  <{c['tag']}> {json.dumps(c['attrs'])[:150]} | {c['text']!r}")

        # Initial state = today's lineup, our "has comedians" reference.
        report("INITIAL PAGE LOAD (reference: should have comedians)",
               page.evaluate(SHAPE_JS), xhr[:])

        for d in controls[1:] + targets:
            xhr.clear()
            note = activate(page, d)
            page.wait_for_timeout(3500)
            report(f"{d} ({d:%a}) — activation: {note}", page.evaluate(SHAPE_JS), xhr[:])

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
