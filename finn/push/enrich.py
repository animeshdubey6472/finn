"""Read a filing and turn it into something worth a push.

Three readers, chosen by the rule verdict:

    results            -> headline numbers transcribed from the filing, then computed and
                          reconciled in `results.py`
    management change  -> who, what role, and the appointee's background from the profile that
                          listing rules require inside the filing (web lookup only as a fallback)
    anything else      -> materiality 0-3, a one-line headline and why it matters

Routine filings never get here: the classifier settles them for free.
"""
from __future__ import annotations

from .. import prompts
from ..llm import LLM, Attachment, LLMUnavailable
from ..net import Fetcher, FetchError
from ..sources import pdf
from ..sources.screener import Company
from . import results as results_math
from .classify import Verdict, blend, tier_for

GENERIC_CHARS = 7_500
PEOPLE_CHARS = 11_000
RESULTS_CHARS = 26_000


def _load(url: str, net: Fetcher) -> tuple[bytes, list[str]] | None:
    try:
        data = pdf.download(url, net=net, max_bytes=25_000_000, budget_s=90)
    except FetchError:
        return None
    if not pdf.is_pdf(data):
        return None
    return data, pdf.page_texts(data, max_pages=40)


def results_pages(texts: list[str]) -> list[int]:
    """Indices of the profit-and-loss statement pages (not cash flows, not restatement notes)."""
    hits = []
    for index, text in enumerate(texts):
        low = text.lower()
        top = "revenue from operations" in low or "total income" in low or "interest earned" in low
        bottom = "profit before tax" in low or "profit for the period" in low or "net profit" in low or "profit/(loss)" in low
        if top and bottom and "cash flow" not in low[:500] and sum(ch.isdigit() for ch in text) > 250:
            hits.append(index)
    if not hits:
        return []
    return [i for i in hits if i <= hits[0] + 8][:4]


def _results_text(texts: list[str]) -> str | None:
    pages = results_pages(texts)
    if not pages:
        return None
    parts = [f"[p.1] {texts[0][:1200]}"] if 0 not in pages and texts[0] else []
    parts += [f"[p.{i + 1}] {texts[i][:7000]}" for i in pages]
    notes = [
        i for i, t in enumerate(texts[:24])
        if i not in pages and any(k in t.lower() for k in ("exceptional item", "restated", "qualified", "emphasis of matter"))
    ][:2]
    parts += [f"[p.{i + 1}] {texts[i][:2500]}" for i in notes]
    return "\n\n".join(parts)[:RESULTS_CHARS]


def read_results(item: dict, company: Company, llm: LLM, data: bytes, texts: list[str]) -> dict | None:
    header = f"Company: {company.name} ({company.symbol}). Filing: {item['title']}\n\n"
    text = None if pdf.is_scanned(texts) else _results_text(texts)
    if text:
        raw = llm.json(prompts.RESULTS_V, tier="fast", system=prompts.RESULTS_SYSTEM, user=header + text, max_tokens=2500)
    else:  # scanned filing: let a multimodal model read the pages themselves
        raw = llm.json(prompts.RESULTS_V, tier="fast", system=prompts.RESULTS_SYSTEM,
                       user=header + "The filing is attached as a PDF. Use its page numbers.",
                       attachments=[Attachment(pdf.head(data, 14))], max_tokens=2500)
    return results_math.build_card(raw, company)


def results_why(card: dict) -> str | None:
    """A deterministic 'so what' for a results card: the margin move, plus anything the filing flags."""
    by_key = {m["key"]: m for m in card["metrics"]}
    parts = []
    margin = by_key.get("ebitda", {})
    now, then = margin.get("margin_pct"), margin.get("margin_year_ago_pct")
    if now is not None and then is not None and not card["is_lender"]:
        bps = round((now - then) * 100)
        if abs(bps) >= 100:
            parts.append(f"EBITDA margin {'up' if bps > 0 else 'down'} {abs(bps)} bps YoY to {now:.1f}%")
        else:
            parts.append(f"EBITDA margin steady at {now:.1f}%")
    rev, pat = by_key.get("revenue", {}), by_key.get("pat", {})
    if rev.get("yoy_pct") is not None and pat.get("yoy_pct") is not None and rev["yoy_pct"] > 0 > pat["yoy_pct"]:
        parts.append("profit fell on higher revenue")
    return "; ".join(parts)[:220] or None  # filing flags are shown separately on the card


def read_people(item: dict, company: Company, llm: LLM, data: bytes, texts: list[str]) -> dict | None:
    header = f"Company: {company.name} ({company.symbol}). Filing: {item['title']}\n\n"
    if pdf.is_scanned(texts):
        raw = llm.json(prompts.PEOPLE_V, tier="fast", system=prompts.PEOPLE_SYSTEM, user=header + "The filing is attached as a PDF.",
                       attachments=[Attachment(pdf.head(data, 6))], max_tokens=2000)
    else:
        raw = llm.json(prompts.PEOPLE_V, tier="fast", system=prompts.PEOPLE_SYSTEM,
                       user=header + pdf.with_page_marks(texts[:8], budget_chars=PEOPLE_CHARS), max_tokens=2000)
    if not isinstance(raw, dict):
        return None
    people = []
    for person in (raw.get("people") or [])[:4]:
        if not isinstance(person, dict) or not person.get("name"):
            continue
        people.append({
            "name": str(person["name"])[:80],
            "change": person.get("change") if person.get("change") in {"appointment", "reappointment", "resignation", "retirement", "cessation", "redesignation"} else "appointment",
            "role": str(person.get("role") or "")[:100],
            "level": person.get("level") if person.get("level") in {"ceo", "cfo", "chair", "board", "senior_management", "other"} else "other",
            "effective": str(person["effective"])[:10] if person.get("effective") else None,
            "reason": str(person["reason"])[:140] if person.get("reason") else None,
            "background": str(person["background"])[:420] if person.get("background") else None,
            "previous_roles": [str(r)[:90] for r in (person.get("previous_roles") or []) if r][:4],
            "education": str(person["education"])[:120] if person.get("education") else None,
            "background_source": "filing" if person.get("background") else None,
        })
    if not people:
        return None
    # Fallback: a top-level appointee whose filing carries no profile -> one grounded web lookup.
    for person in people:
        if person["change"] == "appointment" and person["level"] in {"ceo", "cfo", "chair"} and not person["background"] and llm.available("search"):
            try:
                found = llm.json(f"{prompts.BIO_V}", tier="search", system=prompts.BIO_SYSTEM,
                                 user=f"{person['name']}, newly appointed {person['role']} of {company.name} (India). What is their professional background?",
                                 search=True, max_tokens=900)
            except LLMUnavailable:
                continue
            if isinstance(found, dict) and found.get("found") and found.get("background"):
                person.update(
                    background=str(found["background"])[:420],
                    previous_roles=[str(r)[:90] for r in (found.get("previous_roles") or []) if r][:4],
                    education=str(found["education"])[:120] if found.get("education") else None,
                    background_source="web",
                    background_links=[s for s in (found.get("_sources") or []) if s.get("url")][:3],
                )
    return {
        "type": "people",
        "people": people,
        "headline": str(raw.get("headline") or "")[:140] or None,
        "why": str(raw["why"])[:220] if raw.get("why") else None,
        "materiality": raw.get("materiality") if raw.get("materiality") in (0, 1, 2, 3) else None,
    }


def read_generic(item: dict, company: Company, llm: LLM, data: bytes, texts: list[str]) -> dict | None:
    header = f"Company: {company.name} ({company.symbol}). Exchange subject line: {item['title']}\n\n"
    if pdf.is_scanned(texts):
        raw = llm.json(prompts.READ_FILING_V, tier="fast", system=prompts.READ_FILING_SYSTEM, user=header + "The filing is attached as a PDF.",
                       attachments=[Attachment(pdf.head(data, 4))], max_tokens=1200)
    else:
        raw = llm.json(prompts.READ_FILING_V, tier="fast", system=prompts.READ_FILING_SYSTEM,
                       user=header + pdf.with_page_marks(texts[:5], budget_chars=GENERIC_CHARS), max_tokens=1200)
    if not isinstance(raw, dict):
        return None
    facts = [
        {"label": str(f["label"])[:30], "value": str(f["value"])[:60]}
        for f in (raw.get("facts") or [])[:4] if isinstance(f, dict) and f.get("label") and f.get("value")
    ]
    return {
        "category": raw.get("category"),
        "materiality": raw.get("materiality") if raw.get("materiality") in (0, 1, 2, 3) else None,
        "headline": str(raw.get("headline") or "")[:140] or None,
        "why": str(raw["why"])[:220] if raw.get("why") else None,
        "facts": facts,
    }


def enrich(item: dict, verdict: Verdict, company: Company, llm: LLM, net: Fetcher) -> dict:
    """Read the filing behind `item` and return the fields to merge into it.

    Never raises for a bad document: the item keeps its rule verdict and records why it was
    not read. Only LLMUnavailable propagates, so a run can stop asking once quota is gone.
    """
    loaded = _load(item["url"], net)
    if loaded is None:
        return {"read": {"status": "failed", "reason": "filing could not be downloaded"}}
    data, texts = loaded
    update: dict = {"pages": len(texts) or None}

    if verdict.category == "results" and verdict.tag == "filing":
        card = read_results(item, company, llm, data, texts)
        if card:
            update.update(enrichment=card, headline=results_math.headline(card), why=results_why(card), score=max(verdict.score, 90),
                          read={"status": "ok", "kind": "results"})
            return update
        fallback = results_math.card_from_standardized(company)
        update["read"] = {"status": "failed", "reason": "headline numbers could not be read from the filing"}
        if fallback:
            update.update(enrichment=fallback, headline=results_math.headline(fallback), why="Numbers from standardized data; the filing itself could not be parsed.")
        return update

    if verdict.category == "management_change":
        people = read_people(item, company, llm, data, texts)
        if people:
            category, score = blend(verdict, people["materiality"], "management_change")
            update.update(enrichment={"type": "people", "people": people["people"]}, headline=people["headline"], why=people["why"],
                          category=category, score=score, read={"status": "ok", "kind": "people"})
            return update

    generic = read_generic(item, company, llm, data, texts)
    if not generic:
        update["read"] = {"status": "failed", "reason": "model output unusable"}
        return update
    category, score = blend(verdict, generic["materiality"], generic["category"])
    update.update(headline=generic["headline"], why=generic["why"], facts=generic["facts"], category=category, score=score,
                  materiality=generic["materiality"], read={"status": "ok", "kind": "generic"})
    return update


def apply(item: dict, update: dict) -> dict:
    item.update({k: v for k, v in update.items() if v is not None})
    item["tier"] = tier_for(item["score"])
    return item
