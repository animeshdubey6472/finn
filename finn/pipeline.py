"""The refresh pipeline: sources -> triage -> reading -> research -> published JSON snapshot.

The web app is static and reads only what this writes into web/data/. A refresh is
incremental (cached pages, cached model calls, feed-as-memory) and fault-tolerant: a source
that fails keeps its last good file rather than publishing a hole.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .llm import LLM
from .net import Fetcher, fetcher
from .pull import research as research_mod
from .push import digest, signals
from .sources import nse_feed
from .sources.screener import Company, UnknownSymbol, fetch_company


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def write_json(path: Path, data, pretty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2 if pretty else None, separators=None if pretty else (",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_companies(symbols: list[str], net: Fetcher, failures: dict[str, str] | None = None) -> dict[str, Company]:
    """Fetch each company; a failure is logged and recorded in `failures`, never raised."""
    out: dict[str, Company] = {}
    for symbol in symbols:
        try:
            out[symbol] = fetch_company(symbol, net=net)
        except UnknownSymbol:
            reason = "not found on the fundamentals source"
            log(f"  ! {symbol}: {reason}, skipped")
            if failures is not None:
                failures[symbol] = reason
        except Exception as exc:  # noqa: BLE001 - one company must not sink the run
            log(f"  ! {symbol}: {exc}")
            if failures is not None:
                failures[symbol] = f"fetch failed ({type(exc).__name__})"
    return out


def portfolio_block(companies: dict[str, Company], prices: dict | None, lookback: int) -> list[dict]:
    snapshot = signals.latest_snapshot(prices, lookback) if prices else {}
    rows = []
    for symbol, c in companies.items():
        rows.append({
            "symbol": symbol,
            "name": c.name,
            "sector": c.sector_path[0] if c.sector_path else None,
            "industry": c.sector_path[-1] if c.sector_path else None,
            "mcap_cr": c.top.get("Market Cap"),
            "pe": c.top.get("Stock P/E"),
            "is_lender": c.is_lender,
            "last_quarter": c.quarters.columns[-1] if c.quarters.columns else None,
            "transcripts": sum(1 for call in c.concalls if call.get("transcript")),
            **({"price": snapshot[symbol]} if symbol in snapshot else {}),
        })
    return rows


def refresh_prices(symbols: list[str], status: dict | None = None) -> dict | None:
    """Fetch EOD prices. On any failure the last good prices.json is left untouched and the
    reason is written to `status`, so the app can say the data is stale rather than wrong."""
    from .sources.prices import fetch_history

    status = status if status is not None else {}
    status.update(attempted_at=_now_iso())
    try:
        prices = fetch_history(symbols)
    except Exception as exc:  # noqa: BLE001
        log(f"  ! prices failed ({type(exc).__name__}: {exc}); keeping the last good file")
        status.update(ok=False, error=f"price source failed ({type(exc).__name__})")
        return None
    if not prices["symbols"]:
        log("  ! prices came back empty; keeping the last good file")
        status.update(ok=False, error="price source returned no data")
        return None
    if prices["missing"]:
        log(f"  ! no price history for: {', '.join(prices['missing'])}")
    write_json(config.DATA_DIR / "prices.json", prices)
    status.update(ok=True, updated_at=status["attempted_at"], as_of=prices["as_of"], missing=prices["missing"])
    return prices


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def merge_portfolio_rows(fresh: list[dict], previous: list[dict] | None, symbols: list[str], failures: dict[str, str], now: str) -> list[dict]:
    """Fresh rows are stamped with `refreshed_at`; a company that failed this run keeps its last good
    row, marked stale with the reason, instead of disappearing from the portfolio."""
    rows = [{**r, "refreshed_at": now, "stale": False} for r in fresh]
    have = {r["symbol"] for r in rows}
    for old in previous or []:
        if old["symbol"] in symbols and old["symbol"] not in have:
            rows.append({**old, "stale": True, "stale_reason": failures.get(old["symbol"], "not refreshed this run")})
    order = {sym: i for i, sym in enumerate(symbols)}
    return sorted(rows, key=lambda r: order.get(r["symbol"], len(order)))


def should_replace_research(old: dict | None, new: dict) -> tuple[bool, str]:
    """A refresh may only replace research with something at least as complete. A run that lost
    model access or transcripts must not overwrite a fuller earlier analysis."""
    if not old:
        return True, "new"
    o, n = old.get("coverage", {}), new.get("coverage", {})
    if n.get("transcripts_read", 0) < o.get("transcripts_read", 0):
        return False, f"read {n.get('transcripts_read', 0)} transcripts vs {o.get('transcripts_read', 0)} before"
    if o.get("synthesis") and not n.get("synthesis"):
        return False, "synthesis unavailable this run"
    if o.get("annual_report") and not n.get("annual_report"):
        return False, "annual report not read this run"
    return True, "complete"


def refresh_symbols(net: Fetcher) -> int:
    path = config.DATA_DIR / "symbols.json"
    if path.exists() and time.time() - path.stat().st_mtime < 7 * 86400:
        return len(read_json(path) or [])
    listed = nse_feed.listed_symbols(net)
    if listed:
        write_json(path, [[s, n] for s, n in listed])
    return len(listed)


def research_index() -> list[dict]:
    """Small listing of every research file on disk, for the app's search and home screen."""
    rows = []
    for path in sorted(config.RESEARCH_DIR.glob("*.json")):
        if path.name == "index.json":
            continue
        data = read_json(path)
        if not data:
            continue
        syn, sc = data.get("synthesis") or {}, data.get("scorecard") or {}
        rows.append({
            "symbol": data["symbol"], "file": path.name, "name": data["name"], "industry": data.get("industry"),
            "generated_at": data["generated_at"], "one_liner": syn.get("one_liner"),
            "delivered": sc.get("delivered", 0), "missed": sc.get("missed", 0), "behind": sc.get("behind", 0), "on_track": sc.get("on_track", 0),
            "calls": data["coverage"]["transcripts_read"], "flags_bad": sum(1 for f in data["fundamentals"]["flags"] if f["tone"] == "bad"),
        })
    return rows


def run_research(symbols: list[str], llm: LLM, net: Fetcher, with_annual_report: bool = True, errors: dict[str, str] | None = None) -> list[str]:
    done = []
    for index, symbol in enumerate(symbols, 1):
        log(f"research {index}/{len(symbols)}: {symbol}")
        try:
            result = research_mod.build(symbol, llm=llm, net=net, with_annual_report=with_annual_report,
                                        progress=lambda stage, detail: log(f"    {detail}") if stage in {"transcripts", "annual_report", "synthesis"} else None)
        except UnknownSymbol:
            log(f"  ! {symbol}: unknown symbol")
            if errors is not None:
                errors[symbol] = "unknown symbol"
            continue
        except Exception as exc:  # noqa: BLE001
            log(f"  ! {symbol}: {type(exc).__name__}: {exc}")
            if errors is not None:
                errors[symbol] = f"failed ({type(exc).__name__}); earlier research kept"
            continue
        existing = read_json(config.RESEARCH_DIR / f"{research_mod.safe_name(result['symbol'])}.json")
        replace, why = should_replace_research(existing, result)
        if not replace:
            log(f"  ! {symbol}: kept the earlier, more complete research ({why})")
            if errors is not None:
                errors[symbol] = f"kept earlier research: {why}"
            continue
        research_mod.save(result)
        cov = result["coverage"]
        log(f"    {cov['transcripts_read']}/{min(cov['transcripts_available'], config.MAX_TRANSCRIPTS)} calls · {len(result['guidance'])} guidance · "
            f"synthesis={'yes' if cov['synthesis'] else 'no'} · {cov['seconds']}s")
        done.append(symbol)
    write_json(config.RESEARCH_DIR / "index.json", research_index())
    return done


def push_snapshot(portfolio, companies: dict, failures: dict[str, str], prices: dict | None, price_status: dict,
                  previous: dict | None, llm: LLM, net: Fetcher) -> dict | None:
    """Build the new feed, or return None when nothing loaded (so the last good feed stays published).

    The feed carries a `status` block: when filings and prices were last refreshed successfully, and
    which companies failed this run. The app shows it as last-updated times and stale warnings.
    """
    if not companies:
        return None
    now = _now_iso()
    feed = digest.build_feed(companies, llm=llm, net=net, previous=previous, portfolio=portfolio.symbols,
                             progress=lambda stage, detail: log(f"    {detail}"))
    rows = portfolio_block(companies, prices, portfolio.thresholds.lookback_sessions)
    feed["portfolio"] = merge_portfolio_rows(rows, (previous or {}).get("portfolio"), portfolio.symbols, failures, now)
    feed["thresholds"] = vars(portfolio.thresholds)
    feed["prices_as_of"] = (prices or {}).get("as_of")
    previous_prices = ((previous or {}).get("status") or {}).get("prices") or {}
    feed["status"] = {
        "filings": {"updated_at": now, "companies_loaded": len(companies), "companies_failed": failures},
        "prices": price_status if price_status.get("ok") else {
            **price_status, "updated_at": previous_prices.get("updated_at") or (prices or {}).get("fetched_at"),
            "as_of": (prices or {}).get("as_of")},
    }
    return feed


def _merge_usage(previous: dict | None, run: dict) -> dict:
    total = dict(previous or {"calls": 0, "tokens_in": 0, "tokens_out": 0, "list_cost_usd": 0.0})
    for key in ("calls", "tokens_in", "tokens_out"):
        total[key] = total.get(key, 0) + run[key]
    total["list_cost_usd"] = round(total.get("list_cost_usd", 0.0) + run["list_cost_usd"], 4)
    return total


def refresh(*, push: bool = True, research: bool = True, symbols: list[str] | None = None, with_annual_report: bool = True) -> dict:
    """Run the whole pipeline (or part of it) and publish the snapshot."""
    started = time.time()
    config.load_env()
    portfolio = config.Portfolio.load()
    net, llm = fetcher(), LLM()
    log(f"model access: fast={'yes' if llm.available('fast') else 'NO'} smart={'yes' if llm.available('smart') else 'NO'}")

    feed = read_json(config.DATA_DIR / "feed.json")
    run_status: dict = {"attempted_at": _now_iso()}
    if push:
        log(f"loading {len(portfolio.symbols)} portfolio companies")
        failures: dict[str, str] = {}
        companies = load_companies(portfolio.symbols, net, failures)
        log("prices (end of day)")
        price_status: dict = {}
        fresh_prices = refresh_prices(list(companies), price_status) if companies else None
        if not companies:
            price_status.update(ok=False, error="skipped: no company data loaded")
        prices = fresh_prices or read_json(config.DATA_DIR / "prices.json")
        log("filings: collect, triage, read")
        new_feed = push_snapshot(portfolio, companies, failures, prices, price_status, feed, llm, net)
        run_status["push"] = {"companies_loaded": len(companies), "companies_failed": failures, "prices": price_status,
                              "feed_written": new_feed is not None}
        if new_feed is None:
            log("  ! no company data loaded at all; the published feed was left untouched")
        else:
            feed = new_feed
            write_json(config.DATA_DIR / "feed.json", feed)
            log(f"feed: {feed['stats']['total']} filings → {feed['stats']['by_tier']} · {feed['stats']['read_this_run']} read this run"
                + (f" · {len(failures)} companies failed: {', '.join(failures)}" if failures else ""))
            refresh_symbols(net)

    researched: list[str] = []
    if research:
        targets = symbols or (portfolio.symbols + portfolio.research_extra)
        research_errors: dict[str, str] = {}
        researched = run_research(targets, llm, net, with_annual_report, research_errors)
        run_status["research"] = {"updated": researched, "errors": research_errors}

    previous_meta = read_json(config.DATA_DIR / "meta.json") or {}
    usage = llm.usage()
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "version": __import__("finn").__version__,
        "portfolio": portfolio.symbols,
        "research_extra": portfolio.research_extra,
        "routes": llm.routes,
        "usage_run": usage,
        "usage_total": _merge_usage(previous_meta.get("usage_total"), usage),
        "network": net.stats,
        "feed_stats": (feed or {}).get("stats"),
        "research_count": len(research_index()),
        "last_run": run_status,
        "seconds": round(time.time() - started, 1),
    }
    write_json(config.DATA_DIR / "meta.json", meta, pretty=True)
    log(f"done in {meta['seconds']}s · model calls {usage['calls']} (cache hits {usage['cache_hits']}) · "
        f"{usage['tokens_in']:,} in / {usage['tokens_out']:,} out · list-price ${usage['list_cost_usd']:.4f}")
    return meta
