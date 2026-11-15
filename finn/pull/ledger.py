"""The promise ledger: what management guided, scored against what was later reported.

Extraction is a model's job; judgement is not. Each guidance item carries a metric, a target
period and (often) a number. This module looks up the reported figure for that metric and
period in the standardized financials and assigns one of:

    delivered   target period is complete and the number was met (or beaten)
    missed      complete, and it was not
    on_track    period still in progress; year-to-date is at or above the target
    behind      in progress and below — with the run-rate now required to get there
    open        nothing reported for the target period yet
    unmeasured  not checkable from headline financials (order book, store count, ...)

Only four headline metrics are scored — revenue growth, operating margin, PAT growth, PAT
margin — plus annual capex. Everything else is kept as an unscored watch item; a wrong
verdict is worse than none.
"""
from __future__ import annotations

from .. import periods
from ..periods import Period

GROWTH_TOLERANCE = 1.0   # percentage points: "12-14%" is met at 11.0
MARGIN_TOLERANCE = 0.5   # percentage points
SCORED = ("delivered", "missed", "on_track", "behind")


class Financials:
    """Reported quarterly and annual figures, addressable by period end."""

    def __init__(self, fundamentals: dict):
        self.fy_end_month: int = fundamentals.get("fy_end_month", 3)
        self.is_lender: bool = fundamentals.get("is_lender", False)
        q, a = fundamentals["series"]["quarterly"], fundamentals["series"]["annual"]
        self.q: dict[Period, dict] = {}
        for i, label in enumerate(q["labels"]):
            period = periods.parse_label(label)
            if period and q["revenue"][i] is not None:
                self.q[period] = {"revenue": q["revenue"][i], "op_profit": q["op_profit"][i], "pat": q["pat"][i]}
        self.a: dict[Period, dict] = {}
        for i, label in enumerate(a["labels"]):
            period = periods.parse_label(label)
            if period and a["revenue"][i] is not None:
                self.a[period] = {"revenue": a["revenue"][i], "op_profit": a["op_profit"][i], "pat": a["pat"][i], "capex": a["capex"][i]}
        self.latest_quarter: Period | None = max(self.q) if self.q else None

    # -- period arithmetic ---------------------------------------------------------
    def fy_end(self, fy: int) -> Period:
        return (fy, self.fy_end_month)

    def fy_quarters(self, fy: int) -> list[Period]:
        end = self.fy_end(fy)
        return [periods.shift(end, -9), periods.shift(end, -6), periods.shift(end, -3), end]

    # -- measurements --------------------------------------------------------------
    def _sum(self, quarters: list[Period], key: str) -> float | None:
        values = [self.q.get(p, {}).get(key) for p in quarters]
        return None if not values or any(v is None for v in values) else float(sum(values))

    def measure(self, metric: str, quarters: list[Period]) -> float | None:
        """Value of a headline metric over a set of reported quarters (growth is vs the same quarters a year earlier)."""
        prior = [periods.shift(p, -12) for p in quarters]
        if metric == "revenue_growth":
            now, then = self._sum(quarters, "revenue"), self._sum(prior, "revenue")
            return None if now is None or then is None or then <= 0 else (now / then - 1) * 100
        if metric == "pat_growth":
            now, then = self._sum(quarters, "pat"), self._sum(prior, "pat")
            return None if now is None or then is None or then <= 0 else (now / then - 1) * 100
        if metric == "operating_margin":
            op, rev = self._sum(quarters, "op_profit"), self._sum(quarters, "revenue")
            return None if op is None or not rev else op / rev * 100
        if metric == "pat_margin":
            pat, rev = self._sum(quarters, "pat"), self._sum(quarters, "revenue")
            return None if pat is None or not rev else pat / rev * 100
        return None

    def annual_measure(self, metric: str, fy: int) -> float | None:
        """Same metrics from the annual table (used once a year has been reported in full)."""
        end = self.fy_end(fy)
        cur, prev = self.a.get(end), self.a.get(periods.shift(end, -12))
        if not cur:
            return None
        if metric == "revenue_growth":
            return None if not prev or not prev["revenue"] or prev["revenue"] <= 0 else (cur["revenue"] / prev["revenue"] - 1) * 100
        if metric == "pat_growth":
            return None if not prev or prev["pat"] is None or prev["pat"] <= 0 or cur["pat"] is None else (cur["pat"] / prev["pat"] - 1) * 100
        if metric == "operating_margin":
            return None if cur["op_profit"] is None or not cur["revenue"] else cur["op_profit"] / cur["revenue"] * 100
        if metric == "pat_margin":
            return None if cur["pat"] is None or not cur["revenue"] else cur["pat"] / cur["revenue"] * 100
        if metric == "capex":
            return cur.get("capex")
        return None


def _range_text(item: dict) -> str:
    low, high, unit = item.get("low"), item.get("high"), {"pct": "%", "bps": " bps", "inr_cr": " cr", "x": "×", "count": ""}.get(item.get("unit") or "", "")
    if low is None:
        return ""
    prefix = "₹" if item.get("unit") == "inr_cr" else ""
    fmt = lambda v: f"{v:,.0f}" if float(v).is_integer() else f"{v:,.1f}"  # noqa: E731
    return f"{prefix}{fmt(low)}{unit}" if high is None or high == low else f"{prefix}{fmt(low)}–{fmt(high)}{unit}"


def _verdict(status: str, detail: str, **extra) -> dict:
    return {"status": status, "detail": detail, **{k: v for k, v in extra.items() if v is not None}}


def _compare(metric: str, actual: float, low: float, high: float | None, complete: bool) -> str:
    tolerance = MARGIN_TOLERANCE if metric.endswith("margin") else GROWTH_TOLERANCE
    met = actual >= low - tolerance
    if complete:
        return "delivered" if met else "missed"
    return "on_track" if met else "behind"


def _required_run_rate(fin: Financials, metric: str, fy: int, done: list[Period], low: float) -> float | None:
    """Growth the remaining quarters must deliver for the full year to reach `low` (revenue/PAT growth only)."""
    key = {"revenue_growth": "revenue", "pat_growth": "pat"}.get(metric)
    if not key:
        return None
    quarters = fin.fy_quarters(fy)
    remaining = [p for p in quarters if p not in done]
    prior_all = fin._sum([periods.shift(p, -12) for p in quarters], key)
    prior_remaining = fin._sum([periods.shift(p, -12) for p in remaining], key)
    done_actual = fin._sum(done, key)
    if not remaining or None in (prior_all, prior_remaining, done_actual) or prior_remaining <= 0:
        return None
    needed = prior_all * (1 + low / 100) - done_actual
    return (needed / prior_remaining - 1) * 100


def score(item: dict, fin: Financials, call_period_end: Period) -> dict:
    """Verdict for one guidance item made on a call that discussed `call_period_end`."""
    metric = item["metric"]
    low, high = item.get("low"), item.get("high")
    fy, quarter = item.get("target_fy"), item.get("target_quarter")
    numeric = low is not None and (item.get("unit") in {"pct", "bps"} or (item.get("unit") == "inr_cr" and metric == "capex"))

    if fin.is_lender and metric in {"operating_margin"}:
        return _verdict("unmeasured", "Operating margin is not reported for lenders in the standardized financials.")
    if metric not in {"revenue_growth", "operating_margin", "pat_growth", "pat_margin", "capex"}:
        return _verdict("unmeasured", "Not checkable against headline financials; kept as a watch item.")

    # ---- which reported periods does the promise refer to? --------------------------------
    if fy is None:
        if item.get("direction") and metric.endswith("margin"):
            return _directional(item, fin, call_period_end, None)
        return _verdict("unmeasured", "No explicit target period, so there is nothing to hold it against yet.")

    if quarter:
        target = fin.fy_quarters(fy)[quarter - 1]
        if target <= call_period_end:
            return _verdict("unmeasured", "Refers to a period already reported when it was said.")
        if metric == "capex":
            return _verdict("unmeasured", "Quarterly capex is not in the standardized financials.")
        if target not in fin.q:
            return _verdict("open", f"{periods.fiscal_label(target, fin.fy_end_month)} has not been reported yet.")
        scope, quarters, complete = periods.fiscal_label(target, fin.fy_end_month), [target], True
    else:
        end = fin.fy_end(fy)
        if end <= call_period_end:
            return _verdict("unmeasured", "Refers to a year already reported when it was said.")
        all_quarters = fin.fy_quarters(fy)
        done = [p for p in all_quarters if p in fin.q]
        fy_name = periods.fiscal_year_label(end, fin.fy_end_month)
        if metric == "capex":
            actual = fin.annual_measure("capex", fy)
            if actual is None:
                return _verdict("open", f"{fy_name} cash flow is not reported yet (capex is annual).")
            if not numeric or item.get("unit") != "inr_cr":
                return _verdict("unmeasured", f"{fy_name} capex was about ₹{actual:,.0f} cr; the guidance gave no rupee figure to compare.", actual=round(actual), actual_label=f"{fy_name} capex")
            top = high if high is not None else low
            status = "delivered" if low * 0.85 <= actual <= top * 1.15 else "missed"
            side = "in line with" if status == "delivered" else ("above" if actual > top else "below")
            return _verdict(status, f"{fy_name} capex was about ₹{actual:,.0f} cr, {side} the {_range_text(item)} guided.", actual=round(actual), actual_label=f"{fy_name} capex", target=_range_text(item))
        if not done:
            return _verdict("open", f"No {fy_name} quarter has been reported yet.")
        complete = len(done) == 4
        quarters = done
        scope = fy_name if complete else f"{fy_name} so far ({len(done)} of 4 quarters)"

    # ---- rupee targets for revenue or PAT ("₹4,500 cr revenue in FY26") ---------------------
    if item.get("unit") == "inr_cr" and metric in {"revenue_growth", "pat_growth"} and low is not None:
        key = "revenue" if metric == "revenue_growth" else "pat"
        noun = "revenue" if key == "revenue" else "PAT"
        if quarter:
            actual = fin.q.get(quarters[0], {}).get(key)
        elif complete:
            actual = (fin.a.get(fin.fy_end(fy)) or {}).get(key) or fin._sum(quarters, key)
        else:
            return _verdict("open", f"{scope}: {noun} so far ₹{fin._sum(quarters, key) or 0:,.0f} cr; the ₹ target is for the full year.", target=_range_text(item))
        if actual is None:
            return _verdict("open", f"Not enough reported history to measure {scope}.")
        met = actual >= low * 0.98
        status = "delivered" if met else "missed"
        return _verdict(status, f"{scope}: {noun} ₹{actual:,.0f} cr against {_range_text(item)} guided.", actual=round(actual), actual_label=f"{scope} {noun}, ₹ cr", target=_range_text(item))

    # ---- directional promise without a number ---------------------------------------------
    if not numeric:
        if low is not None:
            return _verdict("unmeasured", "Guided in a unit the headline financials do not carry; kept as a watch item.")
        if item.get("direction") and metric.endswith("margin"):
            return _directional(item, fin, call_period_end, quarters)
        if item.get("direction"):
            return _verdict("unmeasured", "A direction for growth (e.g. 'outgrow the market') has no benchmark in the company's own numbers; kept as a watch item.")
        return _verdict("unmeasured", "No number or direction to hold it against.")

    # ---- numeric promise ------------------------------------------------------------------
    annual = fin.annual_measure(metric, fy) if (not quarter and complete) else None
    actual = annual if annual is not None else fin.measure(metric, quarters)
    if actual is None:
        return _verdict("open", f"Not enough reported history to measure {scope}.")

    if item.get("unit") == "bps":  # "margin up 50-100 bps": compare the change, not the level
        prior = fin.measure(metric, [periods.shift(p, -12) for p in quarters])
        if prior is None or not metric.endswith("margin"):
            return _verdict("unmeasured", "Guided as a change in basis points on a base that is not in the standardized financials.")
        change = (actual - prior) * 100
        status = _status_for_change(change, low, complete, item.get("direction"))
        return _verdict(status, f"{scope}: {metric_name(metric)} moved {change:+.0f} bps year on year ({prior:.1f}% → {actual:.1f}%) against {_range_text(item)} guided.",
                        actual=round(change), actual_label=f"{scope} change, bps", target=_range_text(item))

    if item.get("direction") == "down" and metric.endswith("margin"):
        met = actual <= (high if high is not None else low) + MARGIN_TOLERANCE
        status = ("delivered" if met else "missed") if complete else ("on_track" if met else "behind")
    else:
        status = _compare(metric, actual, low, high, complete)
    detail = f"{scope}: {metric_name(metric)} {_fmt_actual(metric, actual)} against {_range_text(item)} guided."
    if status == "delivered" and high is not None and actual > high + (MARGIN_TOLERANCE if metric.endswith("margin") else GROWTH_TOLERANCE):
        detail = detail[:-1] + " — ahead of the range."
    if status == "behind":
        needed = _required_run_rate(fin, metric, fy, quarters, low) if not quarter else None
        if needed is not None:
            left = 4 - len(quarters)
            detail += f" The remaining {left} quarter{'s' if left != 1 else ''} must grow {needed:+.0f}% to get there."
    return _verdict(status, detail, actual=round(actual, 1), actual_label=scope, target=_range_text(item))


def _status_for_change(change_bps: float, low: float, complete: bool, direction: str | None) -> str:
    met = change_bps <= -abs(low) + 20 if direction == "down" else change_bps >= low - 20
    return ("delivered" if met else "missed") if complete else ("on_track" if met else "behind")


def _directional(item: dict, fin: Financials, base: Period, quarters: list[Period] | None) -> dict:
    """'Margins will improve' — check the direction over the quarters reported since it was said."""
    metric, direction = item["metric"], item["direction"]
    later = sorted(p for p in (quarters or fin.q) if p > base and p in fin.q)[:4]
    if not later:
        return _verdict("open", "No quarter has been reported since this was said.")
    if base not in fin.q:
        return _verdict("unmeasured", "The quarter it was said in is outside the reported history.")
    before = fin.measure(metric, [base])
    after = fin.measure(metric, later)
    if before is None or after is None:
        return _verdict("unmeasured", "Not enough reported history to measure the direction.")
    delta = after - before
    band = 0.3 if metric.endswith("margin") else 1.0
    moved = "up" if delta > band else "down" if delta < -band else "flat"
    status = "delivered" if moved == direction else "missed"
    span = f"the {len(later)} quarter{'s' if len(later) != 1 else ''} since"
    unit = "%" if metric.endswith("margin") else "% YoY"
    return _verdict(status, f"Said {metric_name(metric)} would go {direction}; over {span} it went from {before:.1f}{unit} to {after:.1f}{unit}.",
                    actual=round(after, 1), actual_label=span, target=f"direction: {direction}")


def metric_name(metric: str) -> str:
    return {"revenue_growth": "revenue growth", "operating_margin": "operating margin", "pat_growth": "PAT growth", "pat_margin": "PAT margin", "capex": "capex"}.get(metric, metric.replace("_", " "))


def _fmt_actual(metric: str, value: float) -> str:
    return f"{value:.1f}%" if metric.endswith("margin") else f"{value:+.1f}%"


# --- guidance drift ------------------------------------------------------------------------

def walks(guidance: list[dict]) -> list[dict]:
    """How a number for the same target moved from call to call: raised, cut or held."""
    groups: dict[tuple, list[dict]] = {}
    for g in guidance:
        if g.get("low") is None or g.get("target_fy") is None or g.get("target_quarter") or g["metric"] == "other":
            continue
        groups.setdefault((g["metric"], g["target_fy"], g.get("unit")), []).append(g)
    out = []
    for (metric, fy, unit), items in groups.items():
        items = sorted(items, key=lambda g: g["call"])
        steps, seen = [], set()
        for g in items:
            if g["call"] in seen:
                continue
            seen.add(g["call"])
            steps.append({"id": g["id"], "call": g["call"], "quarter": g["quarter"], "low": g["low"], "high": g["high"], "text": _range_text(g)})
        if len(steps) < 2:
            continue
        mid = lambda s: (s["low"] + (s["high"] if s["high"] is not None else s["low"])) / 2  # noqa: E731
        first, last = mid(steps[0]), mid(steps[-1])
        band = 0.25 if unit in {"pct", "x"} else max(abs(first) * 0.03, 1e-9)
        drift = "raised" if last - first > band else "cut" if first - last > band else "held"
        out.append({"metric": metric, "label": f"FY{fy % 100:02d} {metric_name(metric)}", "target_fy": fy, "steps": steps, "drift": drift})
    return sorted(out, key=lambda w: (w["drift"] == "held", -w["target_fy"]))


def scorecard(guidance: list[dict]) -> dict:
    counts = {s: 0 for s in ("delivered", "missed", "on_track", "behind", "open", "unmeasured")}
    for g in guidance:
        counts[g["verdict"]["status"]] = counts.get(g["verdict"]["status"], 0) + 1
    settled = counts["delivered"] + counts["missed"]
    return {
        **counts,
        "total": len(guidance),
        "scored": sum(counts[s] for s in SCORED),
        "hit_rate_pct": round(counts["delivered"] / settled * 100) if settled else None,
    }


def build(calls: list[dict], fundamentals: dict) -> dict:
    """Attach a verdict to every guidance item across calls; return the flat list plus summary."""
    fin = Financials(fundamentals)
    guidance: list[dict] = []
    for call in sorted(calls, key=lambda c: c["call"], reverse=True):
        base = periods.from_key(call["period_end"])
        for item in call["guidance"]:
            entry = {"id": f"g{len(guidance) + 1}", "call": call["call"], "call_label": call["call_label"], "quarter": call["quarter"], "url": call["url"], **item}
            entry["target_text"] = _range_text(item) or None
            entry["verdict"] = score(item, fin, base) if base else _verdict("unmeasured", "Call date could not be placed.")
            guidance.append(entry)
    return {"guidance": guidance, "scorecard": scorecard(guidance), "walks": walks(guidance)}
