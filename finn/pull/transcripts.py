"""Read one concall transcript: guidance, claims, analyst pressure — every item quoted and paged.

One `fast`-tier call per transcript. The model's output is not trusted as-is: each quote is
looked up in the page text and marked verified or not, and items whose quote cannot be found
anywhere in the document are dropped.
"""
from __future__ import annotations

from .. import periods, prompts
from ..llm import LLM, Attachment
from ..net import Fetcher, FetchError
from ..sources import pdf
from ..sources.screener import Company
from .grounding import locate_quote

MAX_CHARS = 240_000  # ~60k tokens; a long call is ~90k characters
MEASURABLE = {"revenue_growth", "operating_margin", "pat_growth", "pat_margin", "capex"}
METRICS = MEASURABLE | {"volume_growth", "order_inflow", "capacity", "debt", "other"}


def _clean_number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _check_quote(quote: str, pages: list[str], hint) -> tuple[bool, bool, int | None]:
    """(keep, verified, page). An item is dropped when its quote is absent or matches nothing."""
    hint = hint if isinstance(hint, int) and hint > 0 else None
    if not pages:  # scanned document: no text layer to check against
        return True, False, hint
    if not quote:
        return False, False, hint
    verified, page, score = locate_quote(quote, pages, hint)
    if verified:
        return True, True, page
    return (True, False, page) if score >= 0.4 else (False, False, None)


def _clean_guidance(item: dict, pages: list[str]) -> dict | None:
    quote = str(item.get("quote") or "").strip()
    statement = str(item.get("statement") or "").strip()
    if not statement:
        return None
    keep, verified, page = _check_quote(quote, pages, item.get("page"))
    if not keep:
        return None
    low, high = _clean_number(item.get("low")), _clean_number(item.get("high"))
    if low is not None and high is not None and low > high:
        low, high = high, low
    metric = item.get("metric") if item.get("metric") in METRICS else "other"
    fy = item.get("target_fy")
    target_fy = fy if isinstance(fy, int) and not isinstance(fy, bool) and 2000 < fy < 2100 else None
    target_q = item.get("target_quarter") if item.get("target_quarter") in (1, 2, 3, 4) else None
    return {
        "metric": metric,
        "label": str(item.get("label") or metric.replace("_", " ")).strip()[:60],
        "statement": statement[:260],
        "quote": quote[:320] if verified else None,
        "quote_verified": verified,
        "page": page,
        "speaker": (str(item["speaker"]).strip()[:60] if item.get("speaker") else None),
        "horizon": str(item.get("horizon") or "").strip()[:40] or None,
        "target_fy": target_fy,
        "target_quarter": target_q,
        "low": low if low is not None else high,
        "high": high if high is not None else low,
        "unit": item.get("unit") if item.get("unit") in {"pct", "bps", "inr_cr", "x", "count"} else None,
        "comparison": item.get("comparison") if item.get("comparison") in {"yoy", "level", "qoq", "absolute"} else None,
        "direction": item.get("direction") if item.get("direction") in {"up", "down", "flat"} else None,
        "firmness": item.get("firmness") if item.get("firmness") in {"numeric", "directional", "aspiration"} else "directional",
    }


def _clean_quoted(item: dict, pages: list[str], text_key: str, extra: dict) -> dict | None:
    text = str(item.get(text_key) or "").strip()
    if not text:
        return None
    quote = str(item.get("quote") or "").strip()
    keep, verified, page = _check_quote(quote, pages, item.get("page"))
    if not keep:
        return None
    return {text_key: text[:240], "quote": quote[:300] if verified else None, "quote_verified": verified, "page": page, **extra}


def read_transcript(call: dict, company: Company, llm: LLM, net: Fetcher) -> dict | None:
    """Extract structured content from one call. Returns None if the document cannot be read."""
    url = call.get("transcript")
    call_period = periods.from_key(call.get("call", ""))
    if not url or not call_period:
        return None
    try:
        data = pdf.download(url, net=net)
    except FetchError:
        return None
    if not pdf.is_pdf(data):
        return None
    pages = pdf.page_texts(data)
    if not pages:
        return None

    discussed = periods.quarter_discussed(call_period)
    quarter = periods.fiscal_label(discussed, company.fy_end_month)
    header = (
        f"Company: {company.name} ({company.symbol})\n"
        f"Call held: {periods.label(call_period)}. By date this call discusses {quarter} (quarter ended {periods.label(discussed)}).\n"
        f"Fiscal year ends in {periods.MONTHS[company.fy_end_month - 1]}, so FY{(discussed[0] if discussed[1] <= company.fy_end_month else discussed[0] + 1) % 100:02d} is the year in progress.\n\n"
    )
    attachments: list[Attachment] = []
    if pdf.is_scanned(pages):
        attachments = [Attachment(pdf.head(data, 40))]
        body = "The transcript is attached as a PDF (scanned). Use the PDF's page numbers."
    else:
        body = "TRANSCRIPT\n" + pdf.with_page_marks(pages, budget_chars=MAX_CHARS)
    raw = llm.json(
        f"{prompts.TRANSCRIPT_V}", tier="fast", system=prompts.TRANSCRIPT_SYSTEM, user=header + body,
        attachments=attachments, max_tokens=6000,
    )
    if not isinstance(raw, dict):
        return None

    scanned = bool(attachments)
    check_pages = [] if scanned else pages  # quotes from a scanned PDF cannot be verified against text
    guidance = [g for g in (_clean_guidance(i, check_pages) for i in (raw.get("guidance") or []) if isinstance(i, dict)) if g][:8]
    claims = [
        c for c in (
            _clean_quoted(i, check_pages, "claim", {"topic": str(i.get("topic") or "other")[:24]})
            for i in (raw.get("claims") or []) if isinstance(i, dict)
        ) if c
    ][:6]
    pressure = []
    for item in (raw.get("pressure") or [])[:4]:
        if isinstance(item, dict) and item.get("question"):
            pressure.append({
                "topic": str(item.get("topic") or "")[:40],
                "question": str(item["question"])[:200],
                "response": item.get("response") if item.get("response") in {"direct", "partial", "deflected"} else "partial",
                "page": item.get("page") if isinstance(item.get("page"), int) else None,
            })
    business = raw.get("business") if isinstance(raw.get("business"), dict) else {}
    return {
        "call": call["call"],
        "call_label": periods.label(call_period),
        "period_end": periods.key(discussed),
        "quarter": quarter,
        "url": url,
        "pages": len(pages),
        "scanned": scanned,
        "tone": raw.get("tone") if raw.get("tone") in {"confident", "measured", "cautious", "defensive"} else None,
        "guidance": guidance,
        "claims": claims,
        "pressure": pressure,
        "business": {"what": (str(business.get("what"))[:260] if business.get("what") else None),
                     "segments": [str(s)[:60] for s in (business.get("segments") or []) if s][:8]},
    }
