"""screener.in company page -> structured fundamentals and document links.

One public page per company carries standardized multi-year financials plus links to the
primary documents (annual reports, concall transcripts, exchange filings). Nothing here is
specific to any ticker: the parser keys on section ids and row labels, and tolerates rows
that only exist for some sectors (banks have no "Operating Profit", for instance).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from .. import periods
from ..net import Fetcher, FetchError, fetcher

BASE = "https://www.screener.in"
PAGE_TTL = 3 * 3600  # listing pages change intraday during results season

SECTIONS = {
    "quarters": "quarters",
    "pnl": "profit-loss",
    "balance": "balance-sheet",
    "cashflow": "cash-flow",
    "ratios": "ratios",
    "shareholding": "shareholding",
}


class UnknownSymbol(LookupError):
    pass


@dataclass
class Table:
    columns: list[str] = field(default_factory=list)
    rows: dict[str, list[float | None]] = field(default_factory=dict)

    def row(self, *names: str) -> list[float | None] | None:
        """First row whose label matches any of `names` (case-insensitive)."""
        lowered = {k.lower(): v for k, v in self.rows.items()}
        for name in names:
            if name.lower() in lowered:
                return lowered[name.lower()]
        return None

    def col(self, label: str) -> int | None:
        return self.columns.index(label) if label in self.columns else None

    def value(self, row_names: tuple[str, ...] | str, column: str) -> float | None:
        names = (row_names,) if isinstance(row_names, str) else row_names
        row, idx = self.row(*names), self.col(column)
        return row[idx] if row is not None and idx is not None and idx < len(row) else None

    def series(self, *names: str) -> list[tuple[str, float]]:
        """(column, value) pairs for a row, skipping blanks."""
        row = self.row(*names) or []
        return [(c, v) for c, v in zip(self.columns, row) if v is not None]


@dataclass
class Company:
    symbol: str
    name: str = ""
    url: str = ""
    consolidated: bool = True
    bse_code: str | None = None
    nse_symbol: str | None = None
    sector_path: list[str] = field(default_factory=list)
    top: dict[str, float | list[float] | None] = field(default_factory=dict)
    quarters: Table = field(default_factory=Table)
    pnl: Table = field(default_factory=Table)
    balance: Table = field(default_factory=Table)
    cashflow: Table = field(default_factory=Table)
    ratios: Table = field(default_factory=Table)
    shareholding: Table = field(default_factory=Table)
    growth: dict[str, dict[str, float | None]] = field(default_factory=dict)
    announcements: list[dict] = field(default_factory=list)
    annual_reports: list[dict] = field(default_factory=list)
    concalls: list[dict] = field(default_factory=list)
    credit_ratings: list[dict] = field(default_factory=list)
    fetched_at: float = 0.0

    @property
    def has_financials(self) -> bool:
        sales = self.pnl.row("Sales", "Revenue") or []
        return sum(v is not None for v in sales) >= 2

    @property
    def is_lender(self) -> bool:
        """Banks and NBFCs report 'Financing Profit' instead of operating profit."""
        return self.pnl.row("Financing Profit") is not None

    @property
    def is_insurer(self) -> bool:
        """Insurers report premiums as 'sales'; operating margin and cash conversion mislead for them."""
        return any("insurance" in part.lower() for part in self.sector_path)

    @property
    def is_financial(self) -> bool:
        return self.is_lender or self.is_insurer

    @property
    def fy_end_month(self) -> int:
        """Fiscal year-end month, read off the annual P&L column labels (March for most)."""
        ends = [periods.parse_label(c) for c in self.pnl.columns]
        months = [p[1] for p in ends if p]
        return max(set(months), key=months.count) if months else 3

    def to_dict(self) -> dict:
        return asdict(self)


# --- parsing helpers ------------------------------------------------------------------

_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def parse_number(text: str) -> float | None:
    """'1,23,456' -> 123456.0, '+12%' -> 12.0, '-2,066' -> -2066.0, '' -> None."""
    cleaned = (text or "").replace("\xa0", " ").strip()
    if not cleaned or cleaned in {"-", "--"}:
        return None
    m = _NUM_RE.search(cleaned.replace("+", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _clean(text: str) -> str:
    return " ".join((text or "").replace("\xa0", " ").split())


def _row_label(text: str) -> str:
    return _clean(text).rstrip("+").strip()


def _parse_table(section) -> Table:
    table = section.find("table") if section else None
    if table is None or table.find("thead") is None:
        return Table()
    columns = [_clean(th.get_text(" ")) for th in table.find("thead").find_all("th")][1:]
    rows: dict[str, list[float | None]] = {}
    body = table.find("tbody")
    for tr in body.find_all("tr") if body else []:
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        # First text node only: some labels carry an inline tooltip ("CFO/EBITDA Share of ...").
        name = _row_label(next(cells[0].stripped_strings, ""))
        if not name or name == "Raw PDF":
            continue
        rows[name] = [parse_number(c.get_text(" ")) for c in cells[1:]]
    return Table(columns, rows)


def _parse_top(soup) -> dict:
    out: dict[str, float | list[float] | None] = {}
    box = soup.find(id="top-ratios")
    for li in box.find_all("li") if box else []:
        name_el = li.find(class_="name")
        if not name_el:
            continue
        nums = [parse_number(n.get_text()) for n in li.find_all(class_="number")]
        nums = [n for n in nums if n is not None]
        out[_clean(name_el.get_text())] = nums[0] if len(nums) == 1 else (nums or None)
    return out


def _parse_growth(soup) -> dict:
    out: dict[str, dict[str, float | None]] = {}
    for table in soup.find_all("table", class_="ranges-table"):
        head = table.find("th")
        if not head:
            continue
        ranges = {}
        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) == 2:
                ranges[_clean(cells[0].get_text()).rstrip(":")] = parse_number(cells[1].get_text())
        out[_clean(head.get_text())] = ranges
    return out


def _parse_announcements(box) -> list[dict]:
    items = []
    for li in box.find_all("li") if box else []:
        a = li.find("a", href=True)
        if not a:
            continue
        title = _clean("".join(s for s in a.find_all(string=True, recursive=False)))
        # Fresh filings carry only a <span><time>; older ones a <div> with a short summary.
        time_el = a.find("time")
        detail = time_el.parent if time_el else a.find("div")
        summary = ""
        if detail:
            summary = _clean(detail.get_text(" "))
            if time_el:
                summary = summary.replace(_clean(time_el.get_text()), "", 1)
            summary = summary.lstrip(" -–").strip()
        items.append(
            {
                "title": title or _clean(a.get_text(" "))[:160],
                "summary": summary,
                "ts": time_el.get("datetime") if time_el and time_el.get("datetime") else None,
                "date_text": _clean(time_el.get_text()) if time_el else None,
                "url": urljoin(BASE + "/", a["href"]),
            }
        )
    return items


def _parse_annual_reports(box) -> list[dict]:
    out = []
    for li in box.find_all("li") if box else []:
        a = li.find("a", href=True)
        if not a:
            continue
        text = _clean(a.get_text(" "))
        year = re.search(r"(20\d{2})", text)
        out.append({"title": text, "year": int(year.group(1)) if year else None, "url": a["href"]})
    return out


def _parse_concalls(box) -> list[dict]:
    out = []
    for li in box.find_all("li") if box else []:
        first = li.find("div")
        period = periods.parse_label(_clean(first.get_text())) if first else None
        entry: dict = {"call": periods.key(period) if period else None, "label": _clean(first.get_text()) if first else ""}
        for a in li.find_all("a", href=True):
            kind = _clean(a.get_text()).lower()
            if kind.startswith("transcript"):
                entry["transcript"] = a["href"]
            elif kind == "ppt":
                entry["ppt"] = a["href"]
            elif kind in {"rec", "recording"}:
                entry["recording"] = a["href"]
            elif kind == "notes":
                entry["notes"] = a["href"]
        if period:
            out.append(entry)
    # screener sometimes lists the same call twice (once per document); merge on call month
    merged: dict[str, dict] = {}
    for e in out:
        merged.setdefault(e["call"], {}).update({k: v for k, v in e.items() if v})
    return sorted(merged.values(), key=lambda e: e["call"], reverse=True)


def _parse_ratings(box) -> list[dict]:
    out = []
    for li in box.find_all("li") if box else []:
        a = li.find("a", href=True)
        if a:
            out.append({"title": _clean(a.get_text(" ")), "url": a["href"]})
    return out


def parse_company(html: str, symbol: str, url: str = "") -> Company:
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    company = Company(symbol=symbol.upper(), name=_clean(h1.get_text()) if h1 else symbol, url=url)
    info = soup.find(id="company-info")
    if info is not None and info.get("data-consolidated") is not None:
        company.consolidated = info.get("data-consolidated") == "true"

    top = soup.find(id="top")
    for a in top.find_all("a", href=True) if top else []:
        text = _clean(a.get_text(" "))
        if text.startswith("BSE:"):
            company.bse_code = text.split(":", 1)[1].strip()
        elif text.startswith("NSE:"):
            company.nse_symbol = text.split(":", 1)[1].strip()

    peers = soup.find("section", id="peers")
    if peers:
        company.sector_path = [
            _clean(a.get_text()) for a in peers.find_all("a", href=True) if a["href"].startswith("/market/")
        ]

    company.top = _parse_top(soup)
    for attr, section_id in SECTIONS.items():
        setattr(company, attr, _parse_table(soup.find("section", id=section_id)))
    company.growth = _parse_growth(soup)

    docs = soup.find("section", id="documents")
    if docs:
        boxes = docs.find_all("div", class_="documents")
        by_class = lambda cls: next((b for b in boxes if cls in (b.get("class") or [])), None)  # noqa: E731
        plain = next(
            (b for b in boxes if not {"annual-reports", "concalls", "credit-ratings"} & set(b.get("class") or [])), None
        )
        company.announcements = _parse_announcements(plain)
        company.annual_reports = _parse_annual_reports(by_class("annual-reports"))
        company.concalls = _parse_concalls(by_class("concalls"))
        company.credit_ratings = _parse_ratings(by_class("credit-ratings"))
    return company


def company_url(symbol: str, consolidated: bool = True) -> str:
    path = f"/company/{quote(symbol.upper(), safe='')}/"
    return BASE + path + ("consolidated/" if consolidated else "")


def fetch_company(symbol: str, *, net: Fetcher | None = None, max_age: float = PAGE_TTL) -> Company:
    """Fetch and parse a company. Prefers consolidated numbers; falls back to standalone
    when a company publishes no consolidated statements."""
    net = net or fetcher()
    symbol = symbol.strip().upper()
    last: Company | None = None
    for consolidated in (True, False):
        url = company_url(symbol, consolidated)
        try:
            page = net.get(url, max_age=max_age)
        except FetchError as exc:
            if exc.status == 404:
                raise UnknownSymbol(symbol) from exc
            raise
        company = parse_company(page.text, symbol, url)
        company.fetched_at = page.fetched_at
        if company.has_financials:
            company.consolidated = consolidated
            return company
        last = company
    if last is None:
        raise UnknownSymbol(symbol)
    return last


def announcement_time(item: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(item["ts"]) if item.get("ts") else None
    except ValueError:
        return None
