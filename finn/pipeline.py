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


def load_companies(symbols: list[str], net: Fetcher) -> dict[str, Company]:
    out: dict[str, Company] = {}
    for symbol in symbols:
        try:
            out[symbol] = fetch_company(symbol, net=net)
        except UnknownSymbol:
            log(f"  ! {symbol}: not found on the fundamentals source, skipped")
        except Exception as exc:  # noqa: BLE001 - one company must not sink the run
            log(f"  ! {symbol}: {exc}")
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


def refresh_prices(symbols: list[str]) -> dict | None:
    from .sources.prices import fetch_history

    try:
        prices = fetch_history(symbols)
    except Exception as exc:  # noqa: BLE001
        log(f"  ! prices failed ({type(exc).__name__}: {exc}); keeping the last good file")
        return None
    if not prices["symbols"]:
        log("  ! prices came back empty; keeping the last good file")
        return None
    if prices["missing"]:
        log(f"  ! no price history for: {', '.join(prices['missing'])}")
    write_json(config.DATA_DIR / "prices.json", prices)
    return prices


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


def run_research(symbols: list[str], llm: LLM, net: Fetcher, with_annual_report: bool = True) -> list[str]:
    done = []
    for index, symbol in enumerate(symbols, 1):
        log(f"research {index}/{len(symbols)}: {symbol}")
        try:
            result = research_mod.build(symbol, llm=llm, net=net, with_annual_report=with_annual_report,
                                        progress=lambda stage, detail: log(f"    {detail}") if stage in {"transcripts", "annual_report", "synthesis"} else None)
        except UnknownSymbol:
            log(f"  ! {symbol}: unknown symbol")
            continue
        except Exception as exc:  # noqa: BLE001
            log(f"  ! {symbol}: {type(exc).__name__}: {exc}")
            continue
        research_mod.save(result)
        cov = result["coverage"]
        log(f"    {cov['transcripts_read']}/{min(cov['transcripts_available'], config.MAX_TRANSCRIPTS)} calls · {len(result['guidance'])} guidance · "
            f"synthesis={'yes' if cov['synthesis'] else 'no'} · {cov['seconds']}s")
        done.append(symbol)
    write_json(config.RESEARCH_DIR / "index.json", research_index())
    return done


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
    if push:
        log(f"loading {len(portfolio.symbols)} portfolio companies")
        companies = load_companies(portfolio.symbols, net)
        log("prices (end of day)")
        prices = refresh_prices(list(companies)) or read_json(config.DATA_DIR / "prices.json")
        log("filings: collect, triage, read")
        feed = digest.build_feed(companies, llm=llm, net=net, previous=feed, progress=lambda stage, detail: log(f"    {detail}"))
        feed["portfolio"] = portfolio_block(companies, prices, portfolio.thresholds.lookback_sessions)
        feed["thresholds"] = vars(portfolio.thresholds)
        feed["prices_as_of"] = (prices or {}).get("as_of")
        write_json(config.DATA_DIR / "feed.json", feed)
        log(f"feed: {feed['stats']['total']} filings → {feed['stats']['by_tier']} · {feed['stats']['read_this_run']} read this run")
        refresh_symbols(net)

    researched: list[str] = []
    if research:
        targets = symbols or (portfolio.symbols + portfolio.research_extra)
        researched = run_research(targets, llm, net, with_annual_report)

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
        "seconds": round(time.time() - started, 1),
    }
    write_json(config.DATA_DIR / "meta.json", meta, pretty=True)
    log(f"done in {meta['seconds']}s · model calls {usage['calls']} (cache hits {usage['cache_hits']}) · "
        f"{usage['tokens_in']:,} in / {usage['tokens_out']:,} out · list-price ${usage['list_cost_usd']:.4f}")
    return meta
