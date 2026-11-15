"""Quarterly results: from figures a model transcribed to a card an investor can trust.

The model only copies line items out of the filing. Everything else happens here:
unit conversion, EBITDA, margins, QoQ/YoY, internal consistency checks, and a reconciliation
against the standardized quarterly table when that already carries the quarter.
"""
from __future__ import annotations

from .. import periods
from ..sources.screener import Company

UNIT_TO_CRORE = {"crore": 1.0, "crores": 1.0, "lakh": 0.01, "lakhs": 0.01, "million": 0.1, "millions": 0.1, "billion": 100.0, "thousand": 0.0001}
COLUMNS = ("current", "previous_quarter", "year_ago_quarter")
RECONCILE_TOLERANCE = 0.015  # 1.5% — rounding plus reclassification noise


def _num(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        text = str(value).replace(",", "").strip()
        negative = text.startswith("(") and text.endswith(")")
        number = float(text.strip("()"))
        return -number if negative else number
    except ValueError:
        return None


def pct_change(current: float | None, base: float | None) -> float | None:
    """Percentage change; None when the base is zero or negative (a change off a loss is not a growth rate)."""
    if current is None or base is None or base <= 0:
        return None
    return round((current / base - 1) * 100, 1)


def ebitda(col: dict, is_lender: bool) -> float | None:
    """Operating profit before interest, depreciation and other income.

    Lenders: pre-provision operating profit as printed (EBITDA is not meaningful for a bank).
    Others: revenue - (total expenses - finance costs - depreciation). If the expenses line is
    missing, fall back to PBT before exceptionals + finance costs + depreciation - other income.
    """
    if is_lender:
        return _num(col.get("operating_profit"))
    revenue, expenses = _num(col.get("revenue")), _num(col.get("total_expenses"))
    finance, dep = _num(col.get("finance_costs")) or 0.0, _num(col.get("depreciation")) or 0.0
    if revenue is not None and expenses is not None:
        return revenue - (expenses - finance - dep)
    pbt = _num(col.get("pbt_before_exceptional"))
    if pbt is None:
        pbt = _num(col.get("pbt"))
    if pbt is None:
        return None
    return pbt + finance + dep - (_num(col.get("other_income")) or 0.0)


def _column(col: dict, factor: float, is_lender: bool) -> dict:
    scale = lambda v: None if v is None else round(v * factor, 2)  # noqa: E731
    return {
        "revenue": scale(_num(col.get("revenue"))),
        "ebitda": scale(ebitda(col, is_lender)),
        "pbt": scale(_num(col.get("pbt")) if _num(col.get("pbt")) is not None else _num(col.get("pbt_before_exceptional"))),
        "pat": scale(_num(col.get("pat"))),
    }


def _margin(numerator: float | None, revenue: float | None) -> float | None:
    return round(numerator / revenue * 100, 1) if numerator is not None and revenue else None


def _self_checks(raw: dict) -> list[str]:
    """Arithmetic the printed statement must satisfy. A failure means a mis-read column."""
    problems = []
    for name in COLUMNS:
        col = raw.get(name) or {}
        pbt, tax, pat = _num(col.get("pbt")), _num(col.get("tax")), _num(col.get("pat"))
        if None not in (pbt, tax, pat) and abs((pbt - tax) - pat) > max(0.02 * abs(pbt), 1.0):
            problems.append(f"{name}: PBT - tax != PAT")
        if _num(col.get("revenue")) is None or _num(col.get("pat")) is None:
            problems.append(f"{name}: missing revenue or PAT")
    return problems


def _reconcile(card: dict, company: Company | None) -> dict:
    """Compare filing-derived numbers with the standardized quarterly table, if it has the quarter."""
    if company is None or not card.get("period_end"):
        return {"status": "unavailable"}
    period = periods.from_key(card["period_end"])
    column = periods.label(period) if period else None
    table = company.quarters
    if column not in table.columns:
        return {"status": "not_yet", "note": "Standardized data has not picked up this quarter yet"}
    pairs = {
        "revenue": ("Sales", "Revenue"),
        "pbt": ("Profit before tax",),
        "pat": ("Net Profit",),
    }
    diffs, worst = {}, 0.0
    for key, rows in pairs.items():
        ours = card["current"].get(key)
        theirs = table.value(rows, column)
        if ours is None or theirs is None or theirs == 0:
            continue
        diff = abs(ours - theirs) / abs(theirs)
        diffs[key] = {"filing": ours, "standardized": theirs, "diff_pct": round(diff * 100, 2)}
        worst = max(worst, diff)
    if not diffs:
        return {"status": "unavailable"}
    return {"status": "matched" if worst <= RECONCILE_TOLERANCE else "differs", "basis_matches": company.consolidated == (card["basis"] == "consolidated"), "diffs": diffs}


def build_card(raw: dict, company: Company | None = None) -> dict | None:
    """Turn the model's transcription into a results card. Returns None if it is unusable."""
    if not isinstance(raw, dict) or not isinstance(raw.get("current"), dict):
        return None
    factor = UNIT_TO_CRORE.get(str(raw.get("unit", "")).lower())
    if factor is None:
        return None
    is_lender = bool(raw.get("is_lender"))
    cols = {name: _column(raw.get(name) or {}, factor, is_lender) for name in COLUMNS}
    current, prev, yago = (cols[name] for name in COLUMNS)
    if current["revenue"] is None or current["pat"] is None:
        return None

    labels = {"revenue": "Total income" if is_lender else "Revenue", "ebitda": "PPOP" if is_lender else "EBITDA", "pbt": "PBT", "pat": "PAT"}
    metrics = []
    for key in ("revenue", "ebitda", "pbt", "pat"):
        entry = {
            "key": key,
            "label": labels[key],
            "value": current[key],
            "prev": prev[key],
            "year_ago": yago[key],
            "qoq_pct": pct_change(current[key], prev[key]),
            "yoy_pct": pct_change(current[key], yago[key]),
        }
        if key in ("ebitda", "pat"):
            entry["margin_pct"] = _margin(current[key], current["revenue"])
            entry["margin_year_ago_pct"] = _margin(yago[key], yago["revenue"])
        metrics.append(entry)

    period = periods.from_key(str(raw.get("period_end", "")))
    card = {
        "type": "results",
        "source": "filing",
        "basis": raw.get("basis", "consolidated"),
        "is_lender": is_lender,
        "period_end": periods.key(period) if period else None,
        "period_label": periods.label(period) if period else None,
        "quarter": periods.fiscal_label(period, company.fy_end_month if company else 3) if period else None,
        "unit": "₹ cr",
        "page": raw.get("page"),
        "current": current,
        "metrics": metrics,
        "flags": [str(f)[:120] for f in (raw.get("flags") or [])][:3],
        "checks": _self_checks(raw),
    }
    card["reconciliation"] = _reconcile(card, company)
    return card


def card_from_standardized(company: Company) -> dict | None:
    """Fallback when a filing cannot be read: the same four numbers from the standardized table."""
    table = company.quarters
    if len(table.columns) < 5:
        return None

    def at(index: int) -> dict:
        get = lambda *names: (table.row(*names) or [None] * len(table.columns))[index]  # noqa: E731
        return {
            "revenue": get("Sales", "Revenue"),
            "ebitda": get("Operating Profit", "Financing Profit"),
            "pbt": get("Profit before tax"),
            "pat": get("Net Profit"),
        }

    current, prev, yago = at(-1), at(-2), at(-5)
    if current["revenue"] is None or current["pat"] is None:
        return None
    labels = {"revenue": "Revenue", "ebitda": "Financing profit" if company.is_lender else "EBITDA", "pbt": "PBT", "pat": "PAT"}
    metrics = []
    for key in ("revenue", "ebitda", "pbt", "pat"):
        entry = {
            "key": key, "label": labels[key], "value": current[key], "prev": prev[key], "year_ago": yago[key],
            "qoq_pct": pct_change(current[key], prev[key]), "yoy_pct": pct_change(current[key], yago[key]),
        }
        if key in ("ebitda", "pat"):
            entry["margin_pct"] = _margin(current[key], current["revenue"])
            entry["margin_year_ago_pct"] = _margin(yago[key], yago["revenue"])
        metrics.append(entry)
    period = periods.parse_label(table.columns[-1])
    return {
        "type": "results",
        "source": "standardized",
        "basis": "consolidated" if company.consolidated else "standalone",
        "is_lender": company.is_lender,
        "period_end": periods.key(period) if period else None,
        "period_label": table.columns[-1],
        "quarter": periods.fiscal_label(period, company.fy_end_month) if period else None,
        "unit": "₹ cr",
        "page": None,
        "current": current,
        "metrics": metrics,
        "flags": [],
        "checks": [],
        "reconciliation": {"status": "unavailable"},
    }


def headline(card: dict) -> str:
    """One deterministic line: 'Revenue ₹952 cr, +4% YoY · PAT ₹40 cr, −66% YoY'."""
    by_key = {m["key"]: m for m in card["metrics"]}
    parts = []
    for key in ("revenue", "pat"):
        metric = by_key.get(key)
        if not metric or metric["value"] is None:
            continue
        text = f"{metric['label']} ₹{fmt_cr(metric['value'])} cr"
        if metric["yoy_pct"] is not None:
            text += f", {'+' if metric['yoy_pct'] >= 0 else '−'}{abs(metric['yoy_pct']):.0f}% YoY"
        elif metric["year_ago"] is not None and metric["year_ago"] <= 0 < metric["value"]:
            text += ", from a loss a year ago"
        elif metric["value"] < 0:
            text += " (loss)"
        parts.append(text)
    return " · ".join(parts)


def fmt_cr(value: float) -> str:
    """Indian-style grouping for crore figures: 123456.7 -> '1,23,457'; small values keep a decimal."""
    if abs(value) < 100:
        return f"{value:,.1f}"
    whole = f"{int(abs(value) + 0.5):d}"  # half-up, not banker's rounding
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return ("-" if value < 0 else "") + whole
