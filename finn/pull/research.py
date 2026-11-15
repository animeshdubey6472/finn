"""Pull: one company, end to end. Works for any symbol the data sources know.

    fundamentals   deterministic, from standardized financials            (no model)
    transcripts    guidance / claims / analyst pressure, quoted + paged   (fast tier, 1 call each)
    ledger         promises scored against reported numbers               (no model)
    annual report  business, segments, audit and notes flags              (fast tier, 1 call)
    synthesis      narrative vs numbers, bull vs bear                     (smart tier, 1 call)
    verification   every figure and reference in the synthesis is checked (no model)

Each stage degrades on its own: no transcripts means no ledger but the numbers still stand;
no model key means a numbers-only view. `coverage` records what was and was not available.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Callable

from .. import config, periods, prompts
from ..llm import LLM, LLMUnavailable
from ..net import Fetcher, fetcher
from ..sources.screener import Company, fetch_company
from . import annual_report, fundamentals, ledger, transcripts
from .grounding import allowed_numbers, ungrounded_numbers

Progress = Callable[[str, str], None]


def _noop(stage: str, detail: str) -> None:  # noqa: ARG001
    return None


# --- fact pack -----------------------------------------------------------------------------

def _quarter_table(series: dict, is_lender: bool) -> str:
    q = series["quarterly"]
    lines = ["quarter | revenue ₹cr | YoY % | " + ("" if is_lender else "op margin % | ") + "PAT ₹cr | YoY %"]
    start = max(0, len(q["labels"]) - 8)
    for i in range(start, len(q["labels"])):
        cell = lambda v, d=0: "n/a" if v is None else f"{v:.{d}f}"  # noqa: E731
        margin = "" if is_lender else f"{cell(q['opm'][i], 1)} | "
        lines.append(f"{q['labels'][i]} | {cell(q['revenue'][i])} | {cell(q['rev_yoy'][i], 1)} | {margin}{cell(q['pat'][i])} | {cell(q['pat_yoy'][i], 1)}")
    return "\n".join(lines)


def fact_pack(company: Company, fund: dict, book: dict, claims: list[dict], pressure: list[dict], business: dict | None) -> str:
    """Everything the synthesis model may use, as plain text with ids."""
    parts = [f"COMPANY: {company.name} ({company.symbol}) — {' › '.join(company.sector_path[-2:]) or 'sector n/a'}"]
    if business and business.get("what"):
        segs = ", ".join(
            f"{s['name']}{' ' + format(s['share_pct'], 'g') + '%' if s.get('share_pct') else ''}" for s in business.get("segments", [])
        )
        parts.append(f"BUSINESS (company's own description, {business.get('source', 'filings')}): {business['what']}" + (f" Segments: {segs}." if segs else ""))
    parts.append("FACTS (computed from reported financials):\n" + "\n".join(f"{f['id']}: {f['text']}" for f in fund["facts"]))
    parts.append("LAST EIGHT QUARTERS:\n" + _quarter_table(fund["series"], fund["is_lender"]))

    if book["guidance"]:
        lines = []
        for g in book["guidance"]:
            target = f"; target {g['target_text']}" if g.get("target_text") else ""
            lines.append(f"{g['id']} [{g['quarter']} call · {g['label']}{target}] {g['statement']} — LEDGER: {g['verdict']['status']}. {g['verdict']['detail']}")
        parts.append("GUIDANCE given on earnings calls, with the ledger verdict computed by code:\n" + "\n".join(lines))
        sc = book["scorecard"]
        parts.append(
            f"LEDGER SCORECARD: {sc['delivered']} delivered, {sc['missed']} missed, {sc['on_track']} on track, {sc['behind']} behind, "
            f"{sc['open']} open, {sc['unmeasured']} unmeasured, of {sc['total']} guidance items."
        )
        if book["walks"]:
            parts.append("GUIDANCE DRIFT (same target across calls):\n" + "\n".join(
                f"{w['label']}: " + " → ".join(f"{s['text']} ({s['quarter']} call)" for s in w["steps"]) + f" = {w['drift']}" for w in book["walks"]
            ))
    if claims:
        parts.append("CLAIMS management made about the present:\n" + "\n".join(f"{c['id']} [{c['quarter']} call · {c['topic']}] {c['claim']}" for c in claims))
    if pressure:
        parts.append("WHAT ANALYSTS PRESSED ON:\n" + "\n".join(f"[{p['quarter']} call] {p['topic']}: {p['question']} (answer: {p['response']})" for p in pressure))
    return "\n\n".join(parts)


# --- synthesis + verification --------------------------------------------------------------

def _verify(raw: dict, pack: str, known_ids: set[str]) -> tuple[dict, dict]:
    """Drop synthesized statements that cite a figure absent from the fact pack; strip unknown refs."""
    allowed = allowed_numbers(pack)
    report = {"checked": 0, "dropped": 0, "dropped_items": []}

    def clean_refs(refs) -> list[str]:
        return [r for r in (refs or []) if isinstance(r, str) and r in known_ids][:6]

    def ok(*texts: str) -> bool:
        report["checked"] += 1
        missing = [n for t in texts for n in ungrounded_numbers(t or "", allowed)]
        if missing:
            report["dropped"] += 1
            report["dropped_items"].append({"text": " / ".join(t for t in texts if t)[:200], "unmatched": missing[:4]})
            return False
        return True

    out: dict = {"one_liner": None, "narrative_vs_numbers": [], "credibility": None, "bull": [], "bear": [], "watch": []}
    one = str(raw.get("one_liner") or "").strip()
    if one and ok(one):
        out["one_liner"] = one[:220]
    for item in raw.get("narrative_vs_numbers") or []:
        if not isinstance(item, dict):
            continue
        says, shows = str(item.get("says") or "").strip(), str(item.get("shows") or "").strip()
        if says and shows and ok(says, shows):
            out["narrative_vs_numbers"].append({
                "theme": str(item.get("theme") or "")[:40], "says": says[:260], "shows": shows[:260],
                "verdict": item.get("verdict") if item.get("verdict") in {"aligned", "stretched", "diverging"} else "stretched",
                "refs": clean_refs(item.get("refs")),
            })
    cred = str(raw.get("credibility") or "").strip()
    if cred and ok(cred):
        out["credibility"] = cred[:360]
    for side in ("bull", "bear"):
        for item in (raw.get(side) or [])[:3]:
            if not isinstance(item, dict):
                continue
            point, evidence = str(item.get("point") or "").strip(), str(item.get("evidence") or "").strip()
            if point and ok(point, evidence):
                out[side].append({"point": point[:180], "evidence": evidence[:200], "refs": clean_refs(item.get("refs"))})
    for item in (raw.get("watch") or [])[:3]:
        text = str(item or "").strip()
        if text and ok(text):
            out["watch"].append(text[:160])
    return out, report


def synthesize(company: Company, fund: dict, book: dict, claims: list[dict], pressure: list[dict], business: dict | None, llm: LLM) -> tuple[dict | None, dict]:
    pack = fact_pack(company, fund, book, claims, pressure, business)
    raw = llm.json(prompts.SYNTHESIS_V, tier="smart", system=prompts.SYNTHESIS_SYSTEM, user=pack, max_tokens=5000)
    if not isinstance(raw, dict):
        return None, {"checked": 0, "dropped": 0, "dropped_items": []}
    known = {f["id"] for f in fund["facts"]} | {g["id"] for g in book["guidance"]} | {c["id"] for c in claims}
    return _verify(raw, pack, known)


# --- business description ------------------------------------------------------------------

def _business(ar: dict | None, calls: list[dict]) -> dict | None:
    """Prefer the annual report; fall back to how management described the business on calls."""
    if ar and ar.get("what"):
        return {"what": ar["what"], "segments": ar.get("segments", []), "geography": ar.get("geography"),
                "source": f"Annual report {ar['year']}", "url": ar["url"]}
    for call in calls:
        if call["business"].get("what"):
            return {"what": call["business"]["what"], "segments": [{"name": s, "share_pct": None, "note": None, "page": None} for s in call["business"]["segments"]],
                    "geography": None, "source": f"{call['quarter']} earnings call", "url": call["url"]}
    return None


# --- orchestration -------------------------------------------------------------------------

def pick_calls(calls: list[dict], limit: int) -> list[dict]:
    """One call per reported quarter, newest first.

    Companies sometimes hold an extra call (a clarification, an investor day) that maps to the
    same quarter as the results call; the results call — the one filed with a deck — wins.
    """
    by_quarter: dict[str, dict] = {}
    for call in calls:
        period = periods.from_key(call.get("call", ""))
        if not period:
            continue
        quarter = periods.key(periods.quarter_discussed(period))
        current = by_quarter.get(quarter)
        if current is None or (call.get("ppt") and not current.get("ppt")):
            by_quarter[quarter] = call
    return sorted(by_quarter.values(), key=lambda c: c["call"], reverse=True)[:limit]


def build(
    symbol: str,
    *,
    llm: LLM | None = None,
    net: Fetcher | None = None,
    progress: Progress = _noop,
    with_annual_report: bool = True,
    max_transcripts: int = config.MAX_TRANSCRIPTS,
) -> dict:
    """Research one company. Raises screener.UnknownSymbol if the symbol does not exist."""
    started = time.time()
    llm, net = llm or LLM(), net or fetcher()
    notes: list[str] = []

    progress("company", f"Looking up {symbol}")
    company = fetch_company(symbol, net=net)
    progress("financials", f"Reading {len(company.pnl.columns)} years and {len(company.quarters.columns)} quarters of financials")
    fund = fundamentals.build(company)

    available = [c for c in company.concalls if c.get("transcript")]
    wanted = pick_calls(available, max_transcripts)
    calls: list[dict] = []
    have_fast = llm.available("fast")
    if not available:
        notes.append("This company publishes no concall transcripts, so there is no guidance to track; the view is numbers-only.")
    elif not have_fast:
        notes.append("No model key configured: transcripts were not read. Numbers below are complete; guidance and narrative are omitted.")
    else:
        for index, call in enumerate(wanted, 1):
            quarter = periods.fiscal_label(periods.quarter_discussed(periods.from_key(call["call"])), company.fy_end_month)
            progress("transcripts", f"Reading the {quarter} call ({index} of {len(wanted)})")
            try:
                parsed = transcripts.read_transcript(call, company, llm, net)
            except LLMUnavailable as exc:
                notes.append(f"Stopped reading transcripts: {exc}")
                break
            if parsed:
                calls.append(parsed)
            else:
                notes.append(f"The {quarter} transcript could not be read.")

    progress("ledger", "Scoring what was promised against what was reported")
    book = ledger.build(calls, fund)

    claims, pressure = [], []
    for call in sorted(calls, key=lambda c: c["call"], reverse=True):
        for claim in call["claims"]:
            claims.append({"id": f"c{len(claims) + 1}", "call": call["call"], "quarter": call["quarter"], "url": call["url"], **claim})
        for item in call["pressure"]:
            pressure.append({"call": call["call"], "quarter": call["quarter"], "url": call["url"], **item})

    ar = None
    if with_annual_report and have_fast and company.annual_reports:
        progress("annual_report", "Reading the latest annual report")
        try:
            ar = annual_report.read_annual_report(company, llm, net)
        except LLMUnavailable as exc:
            notes.append(f"Annual report not read: {exc}")
        if ar and ar.get("error"):
            notes.append(f"Annual report {ar['year']} not read: {ar['error']}.")
            ar = None
    business = _business(ar, calls)

    synthesis, verification = None, {"checked": 0, "dropped": 0, "dropped_items": []}
    if llm.available("smart") and (calls or ar):
        progress("synthesis", "Setting management's story against the numbers")
        try:
            synthesis, verification = synthesize(company, fund, book, claims, pressure, business, llm)
        except LLMUnavailable as exc:
            notes.append(f"Synthesis unavailable: {exc}")
    if not synthesis and (calls or ar):
        notes.append("The narrative analysis (narrative vs numbers, bull vs bear) is pending: it is generated by a stronger model in a separate run.")
    if verification["dropped"]:
        notes.append(f"{verification['dropped']} synthesized statement(s) were removed because they cited a figure not present in the source data.")

    sources = [{"kind": "financials", "label": f"Standardized financials ({'consolidated' if company.consolidated else 'standalone'})", "url": company.url}]
    sources += [{"kind": "transcript", "label": f"{c['quarter']} earnings call · {c['call_label']}", "url": c["url"], "pages": c["pages"]} for c in calls]
    if ar:
        sources.append({"kind": "annual_report", "label": f"Annual report {ar['year']}", "url": ar["url"], "pages": ar["pages"]})

    quotes = [g for g in book["guidance"] if g.get("quote")] + [c for c in claims if c.get("quote")]
    progress("done", "Research ready")
    return {
        "symbol": company.symbol,
        "name": company.name,
        "sector": company.sector_path[0] if company.sector_path else None,
        "industry": company.sector_path[-1] if company.sector_path else None,
        "url": company.url,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "market": {"price": company.top.get("Current Price"), "mcap_cr": company.top.get("Market Cap"), "pe": company.top.get("Stock P/E"),
                   "high_low": company.top.get("High / Low"), "dividend_yield": company.top.get("Dividend Yield")},
        "fundamentals": fund,
        "business": business,
        "annual_report": ar,
        "calls": [{k: c[k] for k in ("call", "call_label", "quarter", "period_end", "url", "pages", "tone")} | {"guidance": len(c["guidance"]), "claims": len(c["claims"])} for c in calls],
        "guidance": book["guidance"],
        "scorecard": book["scorecard"],
        "walks": book["walks"],
        "claims": claims,
        "pressure": pressure,
        "synthesis": synthesis,
        "sources": sources,
        "coverage": {
            "transcripts_available": len(available),
            "transcripts_read": len(calls),
            "annual_report": bool(ar),
            "synthesis": bool(synthesis),
            "quotes_verified": sum(1 for q in quotes if q.get("quote_verified")),
            "quotes_total": len([g for g in book["guidance"]] + claims),
            "synthesis_model": next((c.model for c in reversed(llm.calls) if c.task.startswith("synthesis")), None) if synthesis else None,
            "statements_checked": verification["checked"],
            "statements_dropped": verification["dropped"],
            "notes": notes,
            "seconds": round(time.time() - started, 1),
        },
    }


def save(research: dict) -> None:
    config.RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    path = config.RESEARCH_DIR / f"{safe_name(research['symbol'])}.json"
    path.write_text(json.dumps(research, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def safe_name(symbol: str) -> str:
    """File-system and URL safe form of a ticker (M&M -> M_M)."""
    return "".join(ch if ch.isalnum() else "_" for ch in symbol.upper())
