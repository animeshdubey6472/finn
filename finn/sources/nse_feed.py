"""NSE's official announcements RSS feed and listed-securities file.

The feed is a rolling window of the most recent filings across the whole market, so it is the
"live wire": anything filed in the last few hours shows up here before aggregator pages pick
it up. It carries no history; the pipeline accumulates it run over run.
"""
from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from ..net import Fetcher, FetchError, fetcher

FEED_URL = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
SYMBOLS_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
IST = timezone(timedelta(hours=5, minutes=30))


def _norm(name: str) -> str:
    name = re.sub(r"\b(limited|ltd|the)\b\.?", " ", name.lower())
    return re.sub(r"[^a-z0-9]+", " ", name).strip()


def listed_symbols(net: Fetcher | None = None) -> list[tuple[str, str]]:
    """[(symbol, company name)] for every NSE-listed equity. Cached for a week."""
    net = net or fetcher()
    try:
        text = net.get(SYMBOLS_URL, max_age=7 * 86400).text
    except FetchError:
        return []
    rows = csv.reader(io.StringIO(text))
    next(rows, None)
    return [(r[0].strip(), r[1].strip()) for r in rows if len(r) >= 2 and r[0].strip()]


def fetch_recent(symbols: set[str], net: Fetcher | None = None) -> list[dict]:
    """Recent filings for `symbols` from the official feed. Empty list if the feed is unreachable."""
    net = net or fetcher()
    try:
        xml_text = net.get(FEED_URL, max_age=300).text
    except FetchError:
        return []
    by_name = {_norm(name): symbol for symbol, name in listed_symbols(net)}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for node in root.iter("item"):
        company = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        description = (node.findtext("description") or "").strip()
        symbol = by_name.get(_norm(company))
        if symbol is None:  # fall back to the symbol prefix of the attachment file name
            prefix = link.rsplit("/", 1)[-1].split("_", 1)[0].upper()
            symbol = prefix if prefix in symbols else None
        if symbol not in symbols or not link.lower().endswith(".pdf"):
            continue  # XBRL duplicates of the same filing are skipped
        body, _, subject = description.partition("|SUBJECT:")
        try:
            ts = datetime.strptime((node.findtext("pubDate") or "").strip(), "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST)
        except ValueError:
            continue
        out.append({"symbol": symbol, "title": subject.strip() or "Announcement", "summary": body.strip(), "ts": ts.isoformat(), "url": link, "source": "NSE"})
    return out
