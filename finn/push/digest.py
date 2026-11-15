"""Push: gather filings for the portfolio, triage them, read the ones that deserve it.

The published feed is also the pipeline's memory. Each run loads the previous feed, adds what
is new, and only reads filings it has not read before — so a refresh costs a handful of model
calls, and history deepens run over run even though the sources only expose recent items.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Callable

from .. import config, periods
from ..llm import LLM, LLMUnavailable
from ..net import Fetcher
from ..sources import nse_feed
from ..sources.screener import Company
from . import enrich as enrich_mod
from . import results as results_math
from .classify import classify, tier_for

CLUSTER_WINDOW = timedelta(minutes=150)
READ_WINDOW_DAYS = 30
Progress = Callable[[str, str], None]


def item_id(symbol: str, url: str) -> str:
    return hashlib.sha1(f"{symbol}|{url}".encode("utf-8")).hexdigest()[:12]


def _source(url: str) -> str:
    return "NSE" if "nseindia" in url else "BSE" if "bseindia" in url else "Company"


def _when(item: dict) -> datetime:
    return datetime.fromisoformat(item["ts"])


def collect(companies: dict[str, Company], net: Fetcher) -> list[dict]:
    """Classified filings for every company: the aggregator's recent list plus the live NSE feed."""
    items: dict[str, dict] = {}
    for symbol, company in companies.items():
        for ann in company.announcements:
            if not ann.get("ts") or not ann.get("url"):
                continue
            verdict = classify(ann["title"], ann.get("summary", ""))
            entry = {
                "id": item_id(symbol, ann["url"]), "symbol": symbol, "company": company.name, "ts": ann["ts"],
                "title": ann["title"], "source_text": ann.get("summary") or None, "url": ann["url"], "source": _source(ann["url"]),
                "category": verdict.category, "tag": verdict.tag, "score": verdict.score, "tier": verdict.tier,
                "rule": verdict.rule, "needs_read": verdict.needs_read, "related": [], "read": {"status": "rules"},
            }
            items[entry["id"]] = entry

    for ann in nse_feed.fetch_recent(set(companies), net):
        symbol = ann["symbol"]
        verdict = classify(ann["title"], ann["summary"])
        when = datetime.fromisoformat(ann["ts"])
        duplicate = any(
            other["symbol"] == symbol and other["category"] == verdict.category and abs(_when(other) - when) <= timedelta(hours=3)
            for other in items.values()
        )
        if duplicate:  # the same disclosure is filed with both exchanges
            continue
        entry = {
            "id": item_id(symbol, ann["url"]), "symbol": symbol, "company": companies[symbol].name, "ts": ann["ts"],
            "title": ann["title"], "source_text": ann["summary"] or None, "url": ann["url"], "source": "NSE",
            "category": verdict.category, "tag": verdict.tag, "score": verdict.score, "tier": verdict.tier,
            "rule": verdict.rule, "needs_read": verdict.needs_read, "related": [], "read": {"status": "rules"},
        }
        items[entry["id"]] = entry
    return list(items.values())


QUERY_REPLY_WINDOW = timedelta(days=3)
SIZE_RULE_CATEGORIES = {"m_and_a", "fund_raise", "capex"}
SIZE_RULE_SHARE = 0.005  # 0.5% of market value
_AMOUNT = re.compile(r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d+)?)\s*(crores?|cr|lakhs?|million|mn|billion|bn)\b", re.I)
_SCALE = {"crore": 1, "crores": 1, "cr": 1, "lakh": 0.01, "lakhs": 0.01, "million": 0.1, "mn": 0.1, "billion": 100, "bn": 100}


def stated_amounts_cr(item: dict) -> list[float]:
    """Rupee amounts the model quoted from the filing (facts, headline, why), in crore."""
    text = " ".join([item.get("headline") or "", item.get("why") or ""] + [f.get("value", "") for f in item.get("facts") or []])
    return [float(n.replace(",", "")) * _SCALE[u.lower()] for n, u in _AMOUNT.findall(text)]


def apply_size_rule(item: dict, mcap_cr: float | None) -> None:
    """A deal, fund raise or capex whose every stated amount is under 0.5% of market value is real
    news but not an interruption: it moves to "worth knowing", with the arithmetic shown."""
    if item.get("category") not in SIZE_RULE_CATEGORIES or item.get("read", {}).get("status") != "ok" or not mcap_cr:
        return
    amounts = stated_amounts_cr(item)
    if not amounts:
        return  # no stated size: the rules and the model's reading stand
    largest = max(amounts)
    if largest < SIZE_RULE_SHARE * mcap_cr:
        item["score"] = min(item["score"], 60)
        item["size_note"] = f"Largest stated amount ₹{largest:,.0f} cr is {largest / mcap_cr * 100:.2f}% of market value (₹{mcap_cr:,.0f} cr), below the 0.5% interruption threshold."
    else:
        item.pop("size_note", None)


def pair_exchange_queries(items: list[dict]) -> list[dict]:
    """An exchange's request for clarification and the company's reply are one event.

    The query leads (it is the signal); the reply rides along as a related filing, and what the
    company said becomes the card's "why". Without this the investor gets two alerts, in
    different tiers, for one question and its answer.
    """
    queries = [i for i in items if i.get("tag") == "exchange_query"]
    absorbed: set[int] = set()
    for query in queries:
        replies = [
            r for r in items
            if r is not query and id(r) not in absorbed and r["symbol"] == query["symbol"] and r.get("tag") == "clarification"
            and abs(_when(r) - _when(query)) <= QUERY_REPLY_WINDOW
        ]
        for reply in sorted(replies, key=lambda r: r["ts"]):
            absorbed.add(id(reply))
            seen = {x["url"] for x in query.get("related", [])} | {query["url"]}
            for extra in [reply, *reply.get("related", [])]:
                if extra["url"] not in seen:
                    query.setdefault("related", []).append({"title": extra["title"], "url": extra["url"], "ts": extra["ts"]})
            said = reply.get("headline") or reply.get("source_text")
            if said and not query.get("why"):
                query["why"] = f"Company's reply: {said}"[:220]
    return [i for i in items if id(i) not in absorbed]


def cluster(items: list[dict]) -> list[dict]:
    """Fold filings about the same event into one item.

    Companies routinely file one event several times (the board outcome, the results, the
    press release). Same company + same category within a couple of hours is one event; the
    highest-scoring filing leads and the rest ride along as `related`. An exchange query and
    the company's reply are paired first (see `pair_exchange_queries`).
    """
    items = pair_exchange_queries(items)
    ordered = sorted(items, key=lambda i: (i["symbol"], i["category"], i["ts"]))
    groups: list[list[dict]] = []
    for item in ordered:
        last = groups[-1] if groups else None
        if last and last[-1]["symbol"] == item["symbol"] and last[-1]["category"] == item["category"] and _when(item) - _when(last[-1]) <= CLUSTER_WINDOW:
            last.append(item)
        else:
            groups.append([item])
    out = []
    for group in groups:
        lead = max(group, key=lambda i: (i["score"], i.get("read", {}).get("status") == "ok", -_when(i).timestamp()))
        seen = {r["url"] for r in lead.get("related", [])} | {lead["url"]}
        for other in group:
            if other is lead:
                continue
            for extra in [other, *other.get("related", [])]:
                if extra["url"] not in seen:
                    seen.add(extra["url"])
                    lead.setdefault("related", []).append({"title": extra["title"], "url": extra["url"], "ts": extra["ts"]})
        out.append(lead)
    return out


def _standardized_fallback(item: dict, company: Company) -> dict | None:
    """Deterministic results card — only if the standardized table already holds the quarter this filing reports."""
    when = _when(item)
    expected = periods.quarter_discussed((when.year, when.month))
    latest = periods.parse_label(company.quarters.columns[-1]) if company.quarters.columns else None
    return results_math.card_from_standardized(company) if latest == expected else None


def build_feed(
    companies: dict[str, Company],
    *,
    llm: LLM,
    net: Fetcher,
    previous: dict | None = None,
    max_reads: int = 60,
    progress: Progress = lambda stage, detail: None,
    portfolio: list[str] | None = None,
) -> dict:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=config.FEED_RETENTION_DAYS)
    fresh = collect(companies, net)

    # Carry over history the sources no longer list, and everything already read. Keyed on the
    # portfolio, not on which companies loaded this run: a transient fetch failure for one
    # company must not erase its history from the feed.
    keep = set(portfolio or []) | set(companies)
    carried = [i for i in (previous or {}).get("items", []) if i.get("symbol") in keep and _when(i) >= cutoff]
    for old in carried:
        if old.get("read", {}).get("status") != "ok":  # not read yet: let today's rules re-judge it
            verdict = classify(old["title"], old.get("source_text") or "")
            old.update(category=verdict.category, tag=verdict.tag, score=verdict.score, rule=verdict.rule, needs_read=verdict.needs_read)
    known_urls = {i["url"] for i in carried} | {r["url"] for i in carried for r in i.get("related", [])}
    items = cluster(carried + [i for i in fresh if i["url"] not in known_urls and _when(i) >= cutoff])

    # Read what deserves reading: highest score first, recent only, never twice.
    def readable(item: dict) -> bool:
        state = item.get("read", {})
        return (item["symbol"] in companies and bool(item.get("needs_read"))
                and state.get("status") in {"rules", "failed"} and state.get("attempts", 0) < 2)

    queue = sorted(
        (i for i in items if readable(i) and _when(i) >= now - timedelta(days=READ_WINDOW_DAYS)),
        key=lambda i: (-i["score"], i["ts"]),
    )[:max_reads]
    reads = 0
    if llm.available("fast"):
        for index, item in enumerate(queue, 1):
            progress("read", f"{item['symbol']}: {item['title'][:60]} ({index}/{len(queue)})")
            verdict = classify(item["title"], item.get("source_text") or "")
            attempts = item.get("read", {}).get("attempts", 0) + 1
            try:
                update = enrich_mod.enrich(item, verdict, companies[item["symbol"]], llm, net)
            except LLMUnavailable as exc:
                progress("read", f"stopped: {exc}")
                break
            update.setdefault("read", {"status": "failed"})["attempts"] = attempts
            enrich_mod.apply(item, update)
            reads += 1
    # Results filings always carry numbers, even with no model: fall back to standardized data.
    for item in items:
        if item["category"] == "results" and item["tag"] == "filing" and not item.get("enrichment") and item["symbol"] in companies:
            card = _standardized_fallback(item, companies[item["symbol"]])
            if card:
                item.update(enrichment=card, headline=results_math.headline(card))
    for item in items:
        company = companies.get(item["symbol"])
        apply_size_rule(item, company.top.get("Market Cap") if company else None)
    for item in items:
        item["tier"] = tier_for(item["score"])

    items = cluster(items)  # categories may have changed after reading
    items.sort(key=lambda i: i["ts"], reverse=True)
    by_tier = {tier: sum(1 for i in items if i["tier"] == tier) for tier in ("act", "know", "buried")}
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "items": items,
        "stats": {
            "total": len(items),
            "by_tier": by_tier,
            "read_by_model": sum(1 for i in items if i["read"]["status"] == "ok"),
            "settled_by_rules": sum(1 for i in items if i["read"]["status"] == "rules"),
            "read_this_run": reads,
        },
    }
