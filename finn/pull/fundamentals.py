"""Deterministic fundamentals: every figure in the research view is computed here, not by a model.

Input is the standardized multi-year table set for one company. Output is:

* series for charts (quarterly and annual),
* a short list of FACTS — one-sentence observations with the numbers that back them, each with
  a stable id (f1, f2, ...). Facts are what the UI shows as observations, what the synthesis
  model is allowed to cite, and what its output is checked against,
* FLAGS — facts that crossed a threshold worth interrupting an investor for.

Lenders (banks, NBFCs) get their own treatment: operating margin, working capital and cash
conversion are not meaningful for a balance-sheet business, and saying so is better than
printing numbers that mislead.
"""
from __future__ import annotations

from .. import periods
from ..sources.screener import Company, Table

Number = float | None


# --- small numeric helpers ----------------------------------------------------------------

def _aligned(table: Table, labels: list[str], *names: str) -> list[Number]:
    row = table.row(*names)
    if row is None:
        return [None] * len(labels)
    by_label = dict(zip(table.columns, row))
    return [by_label.get(label) for label in labels]


def _sum(values: list[Number]) -> Number:
    return None if not values or any(v is None for v in values) else float(sum(values))  # type: ignore[arg-type]


def _div(a: Number, b: Number) -> Number:
    return None if a is None or b is None or b == 0 else a / b


def _pct(a: Number, b: Number) -> Number:
    """Growth of a over b in percent; None off a non-positive base."""
    return None if a is None or b is None or b <= 0 else round((a / b - 1) * 100, 1)


def _r(value: Number, digits: int = 1) -> Number:
    return None if value is None else round(value, digits)


def _last(values: list[Number]) -> Number:
    return next((v for v in reversed(values) if v is not None), None)


def _cagr(end: Number, start: Number, years: int) -> Number:
    if end is None or start is None or start <= 0 or end <= 0 or years <= 0:
        return None
    return round(((end / start) ** (1 / years) - 1) * 100, 1)


def cr(value: Number) -> str:
    """₹ crore, readable: 1812954 -> '₹18.1 lakh cr', 12345 -> '₹12,345 cr', 94.6 -> '₹94.6 cr'."""
    if value is None:
        return "n/a"
    sign, v = ("−" if value < 0 else ""), abs(value)
    if v >= 100_000:
        return f"{sign}₹{v / 100_000:.2f} lakh cr"
    if v >= 100:
        return f"{sign}₹{v:,.0f} cr"
    return f"{sign}₹{v:,.1f} cr"


def num(value: Number, digits: int = 0) -> str:
    """Plain number with a typographic minus, e.g. -132 -> '−132'."""
    if value is None:
        return "n/a"
    return f"{'−' if value < 0 else ''}{abs(value):.{digits}f}"


def signed(value: Number, suffix: str = "%", digits: int = 0) -> str:
    if value is None:
        return "n/a"
    return f"{'+' if value >= 0 else '−'}{abs(value):.{digits}f}{suffix}"


class Facts:
    """Collects observations and hands out ids."""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, section: str, text: str, tone: str = "neutral", **values) -> str:
        fact_id = f"f{len(self.items) + 1}"
        self.items.append({"id": fact_id, "section": section, "text": text, "tone": tone, "values": {k: v for k, v in values.items() if v is not None}})
        return fact_id


# --- series --------------------------------------------------------------------------------

def _quarterly(c: Company) -> dict:
    t = c.quarters
    labels = t.columns[-13:]
    rev = _aligned(t, labels, "Sales", "Revenue")
    op = _aligned(t, labels, "Operating Profit", "Financing Profit")
    pat = _aligned(t, labels, "Net Profit")
    out = {
        "labels": labels,
        "revenue": rev,
        "op_profit": op,
        "opm": [_r(_div(o, r) * 100) if _div(o, r) is not None else None for o, r in zip(op, rev)],
        "other_income": _aligned(t, labels, "Other Income"),
        "interest": _aligned(t, labels, "Interest"),
        "depreciation": _aligned(t, labels, "Depreciation"),
        "pbt": _aligned(t, labels, "Profit before tax"),
        "pat": pat,
        "gnpa": _aligned(t, labels, "Gross NPA %"),
        "nnpa": _aligned(t, labels, "Net NPA %"),
    }
    out["rev_yoy"] = [_pct(rev[i], rev[i - 4]) if i >= 4 else None for i in range(len(rev))]
    out["pat_yoy"] = [_pct(pat[i], pat[i - 4]) if i >= 4 else None for i in range(len(pat))]
    return out


def _annual(c: Company) -> dict:
    labels = [l for l in c.pnl.columns if periods.parse_label(l)][-10:]
    rev = _aligned(c.pnl, labels, "Sales", "Revenue")
    op = _aligned(c.pnl, labels, "Operating Profit", "Financing Profit")
    equity_cap = _aligned(c.balance, labels, "Equity Capital")
    reserves = _aligned(c.balance, labels, "Reserves")
    cfo = _aligned(c.cashflow, labels, "Cash from Operating Activity")
    fcf = _aligned(c.cashflow, labels, "Free Cash Flow")
    return {
        "labels": labels,
        "revenue": rev,
        "op_profit": op,
        "opm": [_r(_div(o, r) * 100) if _div(o, r) is not None else None for o, r in zip(op, rev)],
        "pat": _aligned(c.pnl, labels, "Net Profit"),
        "interest": _aligned(c.pnl, labels, "Interest"),
        "other_income": _aligned(c.pnl, labels, "Other Income"),
        "payout": _aligned(c.pnl, labels, "Dividend Payout %"),
        "cfo": cfo,
        "fcf": fcf,
        "capex": [None if a is None or b is None else a - b for a, b in zip(cfo, fcf)],
        "borrowings": _aligned(c.balance, labels, "Borrowings", "Borrowing"),
        "deposits": _aligned(c.balance, labels, "Deposits"),
        "equity": [None if a is None or b is None else a + b for a, b in zip(equity_cap, reserves)],
        "cwip": _aligned(c.balance, labels, "CWIP"),
        "fixed_assets": _aligned(c.balance, labels, "Fixed Assets"),
        "total_assets": _aligned(c.balance, labels, "Total Assets"),
        "debtor_days": _aligned(c.ratios, labels, "Debtor Days"),
        "inventory_days": _aligned(c.ratios, labels, "Inventory Days"),
        "payable_days": _aligned(c.ratios, labels, "Days Payable"),
        "ccc": _aligned(c.ratios, labels, "Cash Conversion Cycle"),
        "roce": _aligned(c.ratios, labels, "ROCE %", "ROE %"),
    }


def _latest_balance(c: Company) -> dict:
    """Most recent balance-sheet column (often a half-year point newer than the last annual)."""
    t = c.balance
    if not t.columns:
        return {}
    label = t.columns[-1]
    get = lambda *n: t.value(n, label)  # noqa: E731
    equity = None if get("Equity Capital") is None or get("Reserves") is None else get("Equity Capital") + get("Reserves")
    return {
        "label": label,
        "borrowings": get("Borrowings", "Borrowing"),
        "deposits": get("Deposits"),
        "equity": equity,
        "cwip": get("CWIP"),
        "fixed_assets": get("Fixed Assets"),
        "total_assets": get("Total Assets"),
    }


# --- sections ------------------------------------------------------------------------------

def _snapshot(c: Company, q: dict, a: dict, bs: dict, facts: Facts) -> dict:
    rev_ttm, rev_prior = _sum(q["revenue"][-4:]), _sum(q["revenue"][-8:-4])
    pat_ttm, pat_prior = _sum(q["pat"][-4:]), _sum(q["pat"][-8:-4])
    op_ttm, op_prior = _sum(q["op_profit"][-4:]), _sum(q["op_profit"][-8:-4])
    opm_ttm = _r(_div(op_ttm, rev_ttm) * 100) if _div(op_ttm, rev_ttm) is not None else None
    opm_prior = _r(_div(op_prior, rev_prior) * 100) if _div(op_prior, rev_prior) is not None else None
    rev_g, pat_g = _pct(rev_ttm, rev_prior), _pct(pat_ttm, pat_prior)
    de = _r(_div(bs.get("borrowings"), bs.get("equity")), 2)
    rev_word = "Revenue"
    last_q = q["labels"][-1] if q["labels"] else None

    if rev_ttm is not None:
        facts.add("snapshot", f"Trailing-twelve-month revenue is {cr(rev_ttm)}, {signed(rev_g)} on the prior twelve months.",
                  "good" if (rev_g or 0) >= 10 else "bad" if (rev_g or 0) < 0 else "neutral", revenue_ttm=rev_ttm, growth_pct=rev_g)
    if pat_ttm is not None:
        facts.add("snapshot", f"Trailing-twelve-month PAT is {cr(pat_ttm)}, {signed(pat_g)} on the prior twelve months.",
                  "good" if (pat_g or 0) >= 10 else "bad" if (pat_g or 0) < 0 else "neutral", pat_ttm=pat_ttm, growth_pct=pat_g)
    if last_q and q["revenue"][-1] is not None:
        facts.add(
            "snapshot",
            f"{last_q} quarter: revenue {cr(q['revenue'][-1])} ({signed(q['rev_yoy'][-1])} YoY), "
            f"PAT {cr(q['pat'][-1])} ({signed(q['pat_yoy'][-1])} YoY)"
            + ("" if c.is_lender or q["opm"][-1] is None else f", operating margin {q['opm'][-1]:.1f}%") + ".",
            "neutral", revenue=q["revenue"][-1], pat=q["pat"][-1], rev_yoy=q["rev_yoy"][-1], pat_yoy=q["pat_yoy"][-1], opm=q["opm"][-1],
        )

    tiles = [
        {"key": "revenue", "label": f"{rev_word} · TTM", "value": rev_ttm, "fmt": "cr", "delta": rev_g, "delta_label": "YoY", "delta_fmt": "pct"},
        {"key": "pat", "label": "PAT · TTM", "value": pat_ttm, "fmt": "cr", "delta": pat_g, "delta_label": "YoY", "delta_fmt": "pct"},
    ]
    if c.is_lender:
        tiles.append({"key": "roe", "label": "ROE", "value": c.top.get("ROE"), "fmt": "pct"})
        nnpa = _last(q["nnpa"])
        if nnpa is not None:
            tiles.append({"key": "nnpa", "label": "Net NPA", "value": nnpa, "fmt": "pct2", "good_when": "low"})
    else:
        tiles.append({"key": "opm", "label": "Operating margin · TTM", "value": opm_ttm, "fmt": "pct",
                      "delta": None if opm_ttm is None or opm_prior is None else round(opm_ttm - opm_prior, 1), "delta_label": "YoY", "delta_fmt": "pp"})
        tiles.append({"key": "roce", "label": "ROCE", "value": c.top.get("ROCE"), "fmt": "pct"})
        tiles.append({"key": "de", "label": "Debt / equity", "value": de, "fmt": "x", "good_when": "low"})
    tiles.append({"key": "pe", "label": "P/E", "value": c.top.get("Stock P/E"), "fmt": "x1"})
    tiles.append({"key": "mcap", "label": "Market cap", "value": c.top.get("Market Cap"), "fmt": "cr"})
    return {
        "quarter": last_q, "basis": "Consolidated" if c.consolidated else "Standalone", "tiles": tiles,
        "ttm": {"revenue": rev_ttm, "pat": pat_ttm, "op_profit": op_ttm, "opm": opm_ttm, "revenue_growth": rev_g, "pat_growth": pat_g},
    }


def _trend(values: list[Number]) -> str | None:
    """'up' / 'down' if a short series moves consistently, else None."""
    pts = [v for v in values if v is not None]
    if len(pts) < 3:
        return None
    ups = sum(b > a for a, b in zip(pts, pts[1:]))
    downs = sum(b < a for a, b in zip(pts, pts[1:]))
    steps = len(pts) - 1
    if ups >= steps - 0 and pts[-1] - pts[0] >= 3:
        return "up"
    if downs >= steps - 0 and pts[0] - pts[-1] >= 3:
        return "down"
    if ups >= steps - 1 and pts[-1] - pts[0] >= 6:
        return "up"
    if downs >= steps - 1 and pts[0] - pts[-1] >= 6:
        return "down"
    return None


def _trajectory(c: Company, q: dict, a: dict, snap: dict, facts: Facts) -> dict:
    sales_cagr = c.growth.get("Compounded Sales Growth", {})
    profit_cagr = c.growth.get("Compounded Profit Growth", {})
    ids = []

    s10, s5, s3 = sales_cagr.get("10 Years"), sales_cagr.get("5 Years"), sales_cagr.get("3 Years")
    ttm_g = snap["ttm"]["revenue_growth"]
    if s3 is not None and s5 is not None:
        horizon = f"{s10:.0f}% over 10 years, " if s10 is not None else ""
        if ttm_g is not None and ttm_g - s3 >= 5:
            tail, tone = f"; the last twelve months ({signed(ttm_g)}) are running ahead of the 3-year pace", "good"
        elif ttm_g is not None and s3 - ttm_g >= 5:
            tail, tone = f"; the last twelve months ({signed(ttm_g)}) are running behind the 3-year pace", "bad"
        else:
            tail, tone = "", "neutral"
        ids.append(facts.add("trajectory", f"Revenue has compounded at {horizon}{s5:.0f}% over 5 and {s3:.0f}% over 3{tail}.", tone,
                             cagr_10y=s10, cagr_5y=s5, cagr_3y=s3, ttm=ttm_g))

    p5, p3 = profit_cagr.get("5 Years"), profit_cagr.get("3 Years")
    if p3 is not None and s3 is not None:
        gap = p3 - s3
        if gap >= 4:
            text, tone = f"Profit has compounded faster than revenue over 3 years ({p3:.0f}% vs {s3:.0f}%): operating leverage is working.", "good"
        elif gap <= -4:
            cost = "" if c.is_lender else ": growth is costing margin"
            text, tone = f"Profit has compounded slower than revenue over 3 years ({p3:.0f}% vs {s3:.0f}%){cost}.", "bad"
        else:
            text, tone = f"Profit and revenue have compounded at a similar pace over 3 years ({p3:.0f}% vs {s3:.0f}%).", "neutral"
        ids.append(facts.add("trajectory", text, tone, profit_cagr_3y=p3, sales_cagr_3y=s3, profit_cagr_5y=p5))

    recent = q["rev_yoy"][-4:]
    direction = _trend(recent)
    if direction and all(v is not None for v in recent):
        path = " → ".join(signed(v) for v in recent)
        word = "accelerated" if direction == "up" else "decelerated"
        ids.append(facts.add("trajectory", f"Quarterly revenue growth has {word} over the last four quarters: {path} YoY.",
                             "good" if direction == "up" else "bad", path=recent))

    if not c.is_lender:
        opm_now, opm_then = q["opm"][-1], (q["opm"][-5] if len(q["opm"]) >= 5 else None)
        if opm_now is not None and opm_then is not None:
            delta = round(opm_now - opm_then, 1)
            if abs(delta) >= 1.5:
                word = "expanded" if delta > 0 else "compressed"
                ids.append(facts.add("trajectory", f"Operating margin has {word} to {opm_now:.1f}% from {opm_then:.1f}% a year ago ({signed(delta, ' pp', 1)}).",
                                     "good" if delta > 0 else "bad", opm_now=opm_now, opm_year_ago=opm_then, delta_pp=delta))
            else:
                ids.append(facts.add("trajectory", f"Operating margin is steady at {opm_now:.1f}% ({opm_then:.1f}% a year ago).", "neutral",
                                     opm_now=opm_now, opm_year_ago=opm_then))
        opm_series = [v for v in a["opm"][-5:] if v is not None]
        if len(opm_series) >= 4:
            hi, lo, cur = max(opm_series), min(opm_series), opm_series[-1]
            if hi - lo >= 3:
                where = "at the top of" if cur == hi else "at the bottom of" if cur == lo else "inside"
                ids.append(facts.add("trajectory", f"Annual operating margin has ranged from {num(lo)}% to {num(hi)}% over five years; the latest year ({num(cur)}%) sits {where} that range.",
                                     "good" if cur == hi else "bad" if cur == lo else "neutral", low=lo, high=hi, latest=cur))

    return {"cagr": {"revenue": sales_cagr, "profit": profit_cagr}, "facts": ids}


def _balance_sheet(c: Company, q: dict, a: dict, bs: dict, snap: dict, facts: Facts) -> dict:
    ids, tiles = [], []
    debt, equity = bs.get("borrowings"), bs.get("equity")

    if c.is_lender:
        deposits, assets = bs.get("deposits"), bs.get("total_assets")
        if deposits is not None:
            same_point = bool(a["labels"]) and bs.get("label") == a["labels"][-1]
            pool = a["deposits"][:-1] if same_point else a["deposits"]
            base_label = (a["labels"][:-1] if same_point else a["labels"])[-1] if pool else None
            base = pool[-1] if pool else None
            growth = _pct(deposits, base)
            ids.append(facts.add("balance_sheet", f"Deposits stand at {cr(deposits)}" + (f", {signed(growth)} since {base_label}." if growth is not None else "."),
                                 "neutral", deposits=deposits, growth_pct=growth))
            tiles.append({"key": "deposits", "label": "Deposits", "value": deposits, "fmt": "cr"})
        if assets is not None and equity:
            lev = round(assets / equity, 1)
            ids.append(facts.add("balance_sheet", f"Assets are {lev:.1f}× shareholders' equity ({cr(equity)}); leverage is the business model for a lender, so watch asset quality rather than debt.",
                                 "neutral", leverage_x=lev, equity=equity))
            tiles.append({"key": "leverage", "label": "Assets / equity", "value": lev, "fmt": "x1"})
        gnpa, nnpa = _last(q["gnpa"]), _last(q["nnpa"])
        if gnpa is not None:
            ids.append(facts.add("balance_sheet", f"Gross NPA is {gnpa:.2f}% and net NPA {nnpa:.2f}% at the latest reported quarter." if nnpa is not None else f"Gross NPA is {gnpa:.2f}%.",
                                 "neutral", gnpa=gnpa, nnpa=nnpa))
            tiles.append({"key": "gnpa", "label": "Gross NPA", "value": gnpa, "fmt": "pct2", "good_when": "low"})
    else:
        de = _div(debt, equity)
        idx3 = -4 if len(a["labels"]) >= 4 else 0
        debt_then, eq_then = a["borrowings"][idx3] if a["borrowings"] else None, a["equity"][idx3] if a["equity"] else None
        de_then = _div(debt_then, eq_then)
        if de is not None:
            tone = "good" if de < 0.3 else "bad" if de > 1.0 else "neutral"
            then = f" (was {de_then:.2f}× in {a['labels'][idx3]})" if de_then is not None else ""
            ids.append(facts.add("balance_sheet", f"Borrowings are {cr(debt)} against equity of {cr(equity)}: debt/equity {de:.2f}×{then}.", tone,
                                 borrowings=debt, equity=equity, de=round(de, 2), de_then=_r(de_then, 2)))
            tiles.append({"key": "de", "label": "Debt / equity", "value": round(de, 2), "fmt": "x", "good_when": "low"})
        op_ttm = snap["ttm"]["op_profit"]
        if debt is not None and op_ttm and op_ttm > 0:
            ratio = round(debt / op_ttm, 1)
            tiles.append({"key": "debt_ebitda", "label": "Debt / EBITDA", "value": ratio, "fmt": "x1", "good_when": "low"})
            if ratio >= 3:
                ids.append(facts.add("balance_sheet", f"Gross debt is {ratio:.1f}× trailing operating profit — more than three years of EBITDA.", "bad", debt_to_ebitda=ratio))
        interest = _sum(q["interest"][-4:])
        other = _sum(q["other_income"][-4:]) or 0.0
        if interest and interest > 0 and op_ttm is not None:
            cover = round((op_ttm + other) / interest, 1)
            tone = "good" if cover >= 8 else "bad" if cover < 3 else "neutral"
            ids.append(facts.add("balance_sheet", f"Operating profit plus other income covers interest {cover:.1f}× over the last twelve months.", tone, interest_cover=cover))
            tiles.append({"key": "cover", "label": "Interest cover", "value": cover, "fmt": "x1"})
        cwip, fixed = bs.get("cwip"), bs.get("fixed_assets")
        if cwip and fixed and fixed > 0 and cwip / fixed >= 0.15:
            share = round(cwip / fixed * 100)
            ids.append(facts.add("balance_sheet", f"Capital work-in-progress is {cr(cwip)}, {share}% of fixed assets — a large build-out not yet earning.", "neutral", cwip=cwip, cwip_share_pct=share))

        ccc_now, ccc_then = _last(a["ccc"]), (a["ccc"][idx3] if a["ccc"] else None)
        if ccc_now is not None:
            tiles.append({"key": "ccc", "label": "Cash conversion cycle", "value": ccc_now, "fmt": "days", "good_when": "low"})
            if ccc_then is not None:
                delta = ccc_now - ccc_then
                parts = []
                for key, name, sign in (("debtor_days", "debtor days", 1), ("inventory_days", "inventory days", 1), ("payable_days", "payable days", -1)):
                    now, then = _last(a[key]), a[key][idx3]
                    if now is not None and then is not None and abs(now - then) >= 5:
                        parts.append((abs(now - then), f"{name} {then:.0f} → {now:.0f}"))
                driver = f" ({', '.join(p for _, p in sorted(parts, reverse=True)[:2])})" if parts else ""
                if delta >= 15:
                    ids.append(facts.add("balance_sheet", f"The cash conversion cycle has lengthened to {num(ccc_now)} days from {num(ccc_then)} in {a['labels'][idx3]}{driver}: more cash is tied up in operations.",
                                         "bad", ccc_now=ccc_now, ccc_then=ccc_then))
                elif delta <= -15:
                    ids.append(facts.add("balance_sheet", f"The cash conversion cycle has shortened to {num(ccc_now)} days from {num(ccc_then)} in {a['labels'][idx3]}{driver}.",
                                         "good", ccc_now=ccc_now, ccc_then=ccc_then))
                else:
                    ids.append(facts.add("balance_sheet", f"The cash conversion cycle is {num(ccc_now)} days, little changed from {num(ccc_then)} in {a['labels'][idx3]}.", "neutral", ccc_now=ccc_now, ccc_then=ccc_then))

    sh = c.shareholding
    promoters = sh.row("Promoters")
    if promoters and len([v for v in promoters if v is not None]) >= 5:
        now, then = promoters[-1], promoters[-5]
        if now is not None and then is not None:
            delta = round(now - then, 2)
            tone = "bad" if delta <= -2 else "neutral"
            move = "unchanged over a year" if abs(delta) < 0.1 else f"{signed(delta, ' pp', 1)} over a year"
            ids.append(facts.add("balance_sheet", f"Promoters hold {now:.1f}%, {move}.", tone, promoter_pct=now, change_pp=delta))
            tiles.append({"key": "promoter", "label": "Promoter holding", "value": now, "fmt": "pct", "delta": delta if abs(delta) >= 0.1 else None, "delta_label": "1y", "delta_fmt": "pp"})
    fii = sh.row("FIIs")
    if fii and len(fii) >= 5 and fii[-1] is not None and fii[-5] is not None and abs(fii[-1] - fii[-5]) >= 2:
        delta = round(fii[-1] - fii[-5], 1)
        ids.append(facts.add("balance_sheet", f"Foreign institutions hold {fii[-1]:.1f}%, {signed(delta, ' pp', 1)} over a year.", "bad" if delta < 0 else "good", fii_pct=fii[-1], change_pp=delta))

    return {"as_of": bs.get("label"), "tiles": tiles, "facts": ids}


def _cash(c: Company, a: dict, facts: Facts) -> dict:
    ids, tiles = [], []
    if c.is_lender:
        ids.append(facts.add("cash", "Operating cash flow is not a useful quality test for a lender — deposits and loans flow through it. Judge earnings quality on asset quality and provisioning instead.", "neutral"))
        return {"tiles": tiles, "facts": ids, "applicable": False}

    def window(key: str, n: int) -> list[Number]:
        return a[key][-n:] if len(a[key]) >= n else []

    for years in (5, 3):
        cfo, op, pat = _sum(window("cfo", years)), _sum(window("op_profit", years)), _sum(window("pat", years))
        if cfo is None or op is None or op <= 0:
            continue
        conv = round(cfo / op * 100)
        tone = "good" if conv >= 80 else "bad" if conv < 55 else "neutral"
        if cfo <= 0:
            text = f"Over {years} years the business reported {cr(op)} of operating profit but operating cash flow was {cr(cfo)}: profits have not turned into cash."
        else:
            pat_note = f"; against PAT of {cr(pat)} that is {cfo / pat:.1f}×" if pat and pat > 0 else ""
            text = f"Over {years} years, {cr(cfo)} of operating cash came from {cr(op)} of operating profit: {conv}% conversion{pat_note}."
        ids.append(facts.add("cash", text, tone, years=years, cfo=cfo, op_profit=op, conversion_pct=conv, pat=pat))
        tiles.append({"key": "conversion", "label": f"CFO / EBITDA · {years}y", "value": conv, "fmt": "pct0"})
        break

    fcf5 = [v for v in a["fcf"][-5:] if v is not None]
    if len(fcf5) >= 3:
        negative = sum(v < 0 for v in fcf5)
        total = sum(fcf5)
        tone = "bad" if negative >= 3 or total < 0 else "good" if negative == 0 else "neutral"
        ids.append(facts.add("cash", f"Free cash flow was negative in {negative} of the last {len(fcf5)} years; cumulative {cr(total)}, latest year {cr(fcf5[-1])}.",
                             tone, negative_years=negative, years=len(fcf5), cumulative_fcf=total, latest_fcf=fcf5[-1]))
        tiles.append({"key": "fcf", "label": f"FCF · {len(fcf5)}y total", "value": total, "fmt": "cr"})

    capex, rev = a["capex"], a["revenue"]
    if capex and capex[-1] is not None and rev[-1]:
        now = round(capex[-1] / rev[-1] * 100, 1)
        prior = [c_ / r for c_, r in zip(capex[-4:-1], rev[-4:-1]) if c_ is not None and r]
        prior_avg = round(sum(prior) / len(prior) * 100, 1) if prior else None
        if now >= 3:
            vs = f" (averaged {prior_avg:.0f}% over the three years before)" if prior_avg is not None else ""
            ids.append(facts.add("cash", f"Capex ran at {now:.0f}% of revenue in {a['labels'][-1]}, about {cr(capex[-1])}{vs}.", "neutral", capex=capex[-1], capex_pct=now, prior_avg_pct=prior_avg))
        tiles.append({"key": "capex", "label": "Capex / revenue", "value": now, "fmt": "pct"})

    payout = _last(a["payout"])
    if payout is not None:
        tiles.append({"key": "payout", "label": "Dividend payout", "value": payout, "fmt": "pct0"})
    return {"tiles": tiles, "facts": ids, "applicable": True}


def _flags(c: Company, q: dict, facts: Facts) -> list[dict]:
    """Promote facts with a clear tone to flags, and add a few cross-cutting checks."""
    other, pbt = _sum(q["other_income"][-4:]), _sum(q["pbt"][-4:])
    if not c.is_lender and other is not None and pbt and pbt > 0 and other / pbt >= 0.3:
        share = round(other / pbt * 100)
        facts.add("cash", f"Other income is {share}% of pre-tax profit over the last twelve months — earnings lean on non-operating items.", "bad", other_income_share_pct=share)
    tax = c.quarters.row("Tax %")
    if tax and tax[-1] is not None and 0 <= tax[-1] < 15 and (q["pbt"][-1] or 0) > 0:
        facts.add("snapshot", f"The effective tax rate was only {tax[-1]:.0f}% in {q['labels'][-1]}, which flatters PAT.", "bad", tax_rate=tax[-1])
    return [{"id": f["id"], "section": f["section"], "tone": f["tone"], "text": f["text"]} for f in facts.items if f["tone"] in {"bad", "good"}]


def build(c: Company) -> dict:
    """All deterministic research for one company."""
    q, a, bs = _quarterly(c), _annual(c), _latest_balance(c)
    facts = Facts()
    snapshot = _snapshot(c, q, a, bs, facts)
    trajectory = _trajectory(c, q, a, snapshot, facts)
    balance = _balance_sheet(c, q, a, bs, snapshot, facts)
    cash = _cash(c, a, facts)
    flags = _flags(c, q, facts)
    return {
        "is_lender": c.is_lender,
        "fy_end_month": c.fy_end_month,
        "snapshot": snapshot,
        "trajectory": trajectory,
        "balance_sheet": balance,
        "cash": cash,
        "flags": flags,
        "facts": facts.items,
        "series": {"quarterly": q, "annual": a},
    }
