"""Reporting periods. A period is identified by its end month, e.g. (2026, 9) = quarter ended Sep 2026.

Using the period end as the key (rather than "Q2 FY27") lets filings, standardized tables and
concall dates join without caring about a company's fiscal calendar.
"""
from __future__ import annotations

import re
from datetime import date, datetime

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MONTH_IX = {m.lower(): i + 1 for i, m in enumerate(MONTHS)}
_LABEL_RE = re.compile(r"\b([A-Za-z]{3})[a-z]*\s+(\d{4})\b")

Period = tuple[int, int]  # (year, month of period end)


def parse_label(label: str) -> Period | None:
    """'Sep 2026' -> (2026, 9). Returns None for 'TTM' and anything unparseable."""
    m = _LABEL_RE.search(label or "")
    if not m:
        return None
    month = _MONTH_IX.get(m.group(1).lower())
    return (int(m.group(2)), month) if month else None


def label(p: Period) -> str:
    return f"{MONTHS[p[1] - 1]} {p[0]}"


def key(p: Period) -> str:
    return f"{p[0]:04d}-{p[1]:02d}"


def from_key(k: str) -> Period | None:
    m = re.fullmatch(r"(\d{4})-(\d{2})", k or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def shift(p: Period, months: int) -> Period:
    idx = p[0] * 12 + (p[1] - 1) + months
    return (idx // 12, idx % 12 + 1)


def quarter_end_on_or_before(d: date | datetime) -> Period:
    """The most recent calendar quarter end (Mar/Jun/Sep/Dec) on or before `d`'s month end."""
    month = d.month - (d.month % 3)
    return (d.year - 1, 12) if month == 0 else (d.year, month)


def quarter_discussed(call: Period) -> Period:
    """A results call held in month M discusses the last *completed* calendar quarter.

    A call in Oct, Nov or Dec discusses the Sep quarter; one in Jan-Mar the Dec quarter.
    A call held in the quarter-end month itself (rare) still refers to the previous quarter,
    because results cannot be out before the quarter closes.
    """
    year, month = call
    done = ((month - 1) // 3) * 3  # last completed quarter-end month in this calendar year
    return (year - 1, 12) if done == 0 else (year, done)


def fiscal_label(p: Period, fy_end_month: int = 3) -> str:
    """(2026, 9) with a March year end -> 'Q2 FY27'."""
    months_after_fy_end = (p[1] - fy_end_month) % 12
    quarter = 4 if months_after_fy_end == 0 else months_after_fy_end // 3
    fy = p[0] if p[1] <= fy_end_month else p[0] + 1
    return f"Q{quarter} FY{fy % 100:02d}"


def fiscal_year_label(p: Period, fy_end_month: int = 3) -> str:
    fy = p[0] if p[1] <= fy_end_month else p[0] + 1
    return f"FY{fy % 100:02d}"


def fiscal_year_end(p: Period, fy_end_month: int = 3) -> Period:
    """End of the fiscal year containing period `p`."""
    return (p[0], fy_end_month) if p[1] <= fy_end_month else (p[0] + 1, fy_end_month)
