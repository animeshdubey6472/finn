"""Annual report: the business in the company's own words, plus what the auditors and notes flag.

Annual reports run to hundreds of pages and tens of megabytes, so we never send one whole.
Pages are scored locally by what they contain (MD&A, segment note, auditor's report,
contingent liabilities, related parties) and only the best few go to one `fast`-tier call.
"""
from __future__ import annotations

from .. import prompts
from ..llm import LLM
from ..net import Fetcher, FetchError
from ..sources import pdf
from ..sources.screener import Company

MAX_BYTES = 60_000_000
BUDGET_S = 240
PAGE_CHARS = 4200
TOTAL_CHARS = 64_000

# (bucket, how many pages to take, phrases that must all appear, phrases that add weight)
_BUCKETS = [
    ("business", 6, ["management discussion"], ["segment", "revenue", "business", "overview", "performance", "outlook", "industry"]),
    ("segments", 2, ["segment revenue"], ["segment results", "segment assets", "reportable segment", "geograph"]),
    ("audit", 2, ["independent auditor", "opinion"], ["consolidated", "basis for", "emphasis of matter", "qualified", "key audit matter"]),
    ("contingent", 2, ["contingent liabilit"], ["claims against", "not acknowledged as debt", "guarantee", "disputed", "crore", "lakh"]),
    ("related", 2, ["related party"], ["transactions", "key management personnel", "holding company", "subsidiar", "outstanding"]),
]


def select_pages(pages: list[str]) -> list[int]:
    """Indices of the pages worth reading, in document order."""
    chosen: list[int] = []
    lowered = [p.lower() for p in pages]
    for _bucket, take, required, weighted in _BUCKETS:
        scored = []
        for index, text in enumerate(lowered):
            if index < 3 or len(text) < 600:  # cover, contents and near-empty pages
                continue
            if not all(phrase in text for phrase in required):
                continue
            score = sum(text.count(w) for w in weighted) + 3 * sum(text.count(r) for r in required)
            scored.append((score, index))
        picks = sorted(sorted(scored, reverse=True)[:take], key=lambda s: s[1])
        for _score, index in picks:
            for candidate in (index, index + 1) if _bucket == "business" else (index,):
                if candidate < len(pages) and candidate not in chosen:
                    chosen.append(candidate)
    return sorted(chosen)


def read_annual_report(company: Company, llm: LLM, net: Fetcher) -> dict | None:
    reports = [r for r in company.annual_reports if r.get("year") and r.get("url", "").lower().split("?")[0].endswith(".pdf")]
    if not reports:
        return None
    report = max(reports, key=lambda r: r["year"])
    try:
        data = pdf.download(report["url"], net=net, max_bytes=MAX_BYTES, budget_s=BUDGET_S)
    except FetchError as exc:
        return {"year": report["year"], "url": report["url"], "error": exc.reason}
    if not pdf.is_pdf(data):
        return {"year": report["year"], "url": report["url"], "error": "not a PDF"}
    pages = pdf.page_texts(data)
    if pdf.is_scanned(pages):
        return {"year": report["year"], "url": report["url"], "error": "scanned report without a text layer"}
    indices = select_pages(pages)
    if not indices:
        return {"year": report["year"], "url": report["url"], "error": "no recognisable sections"}

    parts, used = [], 0
    for index in indices:
        chunk = f"[p.{index + 1}] {pages[index][:PAGE_CHARS]}"
        if used + len(chunk) > TOTAL_CHARS:
            break
        parts.append(chunk)
        used += len(chunk)
    user = f"Company: {company.name} ({company.symbol}). Annual report {report['year']}. Selected pages follow.\n\n" + "\n\n".join(parts)
    raw = llm.json(prompts.ANNUAL_REPORT_V, tier="fast", system=prompts.ANNUAL_REPORT_SYSTEM, user=user, max_tokens=2500)
    if not isinstance(raw, dict):
        return {"year": report["year"], "url": report["url"], "error": "unreadable model output"}

    def page_ok(value) -> int | None:
        return value if isinstance(value, int) and 1 <= value <= len(pages) else None

    def block(key: str, fields: tuple[str, ...]) -> dict | None:
        src = raw.get(key)
        if not isinstance(src, dict):
            return None
        out = {f: (str(src[f])[:220] if src.get(f) not in (None, "") else None) for f in fields}
        out["page"] = page_ok(src.get("page"))
        return out if any(out[f] for f in fields) else None

    segments = []
    for seg in (raw.get("segments") or [])[:8]:
        if isinstance(seg, dict) and seg.get("name"):
            share = seg.get("share_pct")
            segments.append({
                "name": str(seg["name"])[:60],
                "share_pct": round(float(share), 1) if isinstance(share, (int, float)) and not isinstance(share, bool) and 0 < share <= 100 else None,
                "note": str(seg["note"])[:100] if seg.get("note") else None,
                "page": page_ok(seg.get("page")),
            })
    risks = [
        {"risk": str(r["risk"])[:160], "page": page_ok(r.get("page"))}
        for r in (raw.get("risks") or [])[:3] if isinstance(r, dict) and r.get("risk")
    ]
    audit = block("audit", ("opinion", "note"))
    if audit and audit["opinion"] not in {"unmodified", "qualified", "emphasis_of_matter"}:
        audit = None
    return {
        "year": report["year"],
        "url": report["url"],
        "pages": len(pages),
        "pages_read": [i + 1 for i in indices],
        "what": str(raw["what"])[:360] if raw.get("what") else None,
        "segments": segments,
        "geography": str(raw["geography"])[:200] if raw.get("geography") else None,
        "risks": risks,
        "audit": audit,
        "contingent_liabilities": block("contingent_liabilities", ("amount", "note")),
        "related_party": block("related_party", ("note",)),
    }
