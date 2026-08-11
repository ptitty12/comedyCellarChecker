"""Date recognition: match specific target dates, and harvest every date a page mentions.

Kept separate from the fetching/notifying code so it can be tested exhaustively
without touching the network.
"""

import html
import re
from datetime import date, timedelta

MONTH_NAMES = {
    1: ["january", "jan"], 2: ["february", "feb"], 3: ["march", "mar"],
    4: ["april", "apr"], 5: ["may"], 6: ["june", "jun"],
    7: ["july", "jul"], 8: ["august", "aug"],
    9: ["september", "sept", "sep"],
    10: ["october", "oct"], 11: ["november", "nov"], 12: ["december", "dec"],
}
MONTH_BY_NAME = {n: num for num, names in MONTH_NAMES.items() for n in names}
ALL_MONTHS_ALT = "|".join(
    sorted((n for names in MONTH_NAMES.values() for n in names), key=len, reverse=True)
)

# Harvest patterns (any date, used for the "date through" horizon)
ISO_ANY_RE = re.compile(r"(?<!\d)(20\d{2})-(\d{1,2})-(\d{1,2})(?!\d)")
SLASH_ISO_ANY_RE = re.compile(r"(?<!\d)(20\d{2})/(\d{1,2})/(\d{1,2})(?!\d)")
COMPACT_ANY_RE = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")
MDY_ANY_RE = re.compile(
    rf"\b({ALL_MONTHS_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s*(20\d{{2}}))?\b",
    re.IGNORECASE,
)
NUM_MDY_ANY_RE = re.compile(r"(?<!\d)(\d{1,2})[/-](\d{1,2})[/-](20\d{2}|\d{2})(?!\d)")
# Numeric month/day with no year, but only in an unambiguous date-ish context
# (e.g. data-date="9/10", ?date=9-10) so "rated 9/10" can't sneak in.
CTX_MD_RE = re.compile(
    r"(?:date|day|showdate|show_date|d)\s*[=:\"']{1,3}\s*(\d{1,2})[/-](\d{1,2})(?!\d)",
    re.IGNORECASE,
)

SHOWTIME_RE = re.compile(r"\b\d{1,2}[:.]\d{2}\s*(?:pm|am)\b", re.IGNORECASE)


def clean_html(text: str) -> str:
    """Normalise entities/escapes so patterns see plain text."""
    text = html.unescape(html.unescape(text))
    return text.replace("\xa0", " ").replace("\\/", "/").replace("\\u002d", "-")


def patterns_for_date(d: date):
    """Regexes matching this exact date in the formats a site plausibly uses."""
    name_alt = "|".join(sorted(MONTH_NAMES[d.month], key=len, reverse=True))
    yy = d.year % 100
    return [re.compile(p, re.IGNORECASE) for p in (
        rf"(?<!\d){d.year}-0?{d.month}-0?{d.day}(?!\d)",
        rf"(?<!\d){d.year}/0?{d.month}/0?{d.day}(?!\d)",
        rf"(?<!\d){d.year}{d.month:02d}{d.day:02d}(?!\d)",
        rf"(?<!\d)0?{d.month}[/\-.]0?{d.day}[/\-.](?:{d.year}|{yy:02d})(?!\d)",
        # "September 10" / "Sep 10th" / "September 10, 2026" — but not another year
        rf"\b(?:{name_alt})\.?\s+0?{d.day}(?:st|nd|rd|th)?\b(?!\s*,?\s*(?!{d.year})20\d\d)",
    )]


def text_mentions_date(text: str, d: date) -> bool:
    return any(p.search(text) for p in patterns_for_date(d))


def extract_all_dates(text: str, today: date, horizon_days: int = 400):
    """Every plausible near-future date mentioned in the text.

    Used to compute how far ahead the site currently lists shows, so a page
    that silently stops containing dates is detectable.
    """
    found = set()
    lo, hi = today - timedelta(days=2), today + timedelta(days=horizon_days)

    def keep(y, m, dd):
        try:
            val = date(y, m, dd)
        except ValueError:
            return
        if lo <= val <= hi:
            found.add(val)

    def keep_yearless(m, dd):
        for year in (today.year, today.year + 1):
            try:
                val = date(year, m, dd)
            except ValueError:
                continue
            if lo <= val <= hi:
                found.add(val)
                return

    for rx in (ISO_ANY_RE, SLASH_ISO_ANY_RE, COMPACT_ANY_RE):
        for y, m, dd in rx.findall(text):
            keep(int(y), int(m), int(dd))

    for m, dd, y in NUM_MDY_ANY_RE.findall(text):
        year = int(y) if len(y) == 4 else 2000 + int(y)
        keep(year, int(m), int(dd))

    for name, dd, y in MDY_ANY_RE.findall(text):
        m = MONTH_BY_NAME.get(name.lower())
        if not m:
            continue
        if y:
            keep(int(y), m, int(dd))
        else:
            keep_yearless(m, int(dd))

    for m, dd in CTX_MD_RE.findall(text):
        keep_yearless(int(m), int(dd))

    return found


def horizon_of(dates, today: date):
    """Furthest-out date at or after today — the site's 'listing through' date."""
    future = [d for d in dates if d >= today]
    return max(future) if future else None
