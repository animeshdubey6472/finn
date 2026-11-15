"""Command line: python -m finn <command>

    refresh              full pipeline: prices, filings, research -> web/data
    push                 prices and filings only
    research SYM [SYM]   research any NSE symbol(s), cached to web/data/research
    rescore [SYM]        re-apply the ledger rules to saved research (offline)
    alerts               print price/volume signals at the configured thresholds
    serve                run the app + live research API locally
"""
from __future__ import annotations

import argparse
import sys

from . import config, pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="finn", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("refresh", "push"):
        p = sub.add_parser(name)
        p.add_argument("--no-annual-report", action="store_true", help="skip annual reports (large downloads)")
    r = sub.add_parser("research")
    r.add_argument("symbols", nargs="+")
    r.add_argument("--no-annual-report", action="store_true")
    rs = sub.add_parser("rescore", help="re-apply ledger rules to saved research (offline)")
    rs.add_argument("symbols", nargs="*")
    a = sub.add_parser("alerts")
    a.add_argument("--volume", type=float, help="volume multiple threshold")
    a.add_argument("--move", type=float, help="absolute daily move threshold, percent")
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    if args.command == "refresh":
        pipeline.refresh(with_annual_report=not args.no_annual_report)
    elif args.command == "push":
        pipeline.refresh(research=False)
    elif args.command == "research":
        pipeline.refresh(push=False, symbols=[s.upper() for s in args.symbols], with_annual_report=not args.no_annual_report)
    elif args.command == "rescore":
        from .pull import ledger
        from .pull import research as research_mod

        files = [config.RESEARCH_DIR / f"{research_mod.safe_name(s)}.json" for s in args.symbols] if args.symbols else \
            [p for p in sorted(config.RESEARCH_DIR.glob("*.json")) if p.name != "index.json"]
        for path in files:
            data = pipeline.read_json(path)
            if not data:
                print(f"  ! {path.name}: not found")
                continue
            before = dict(data.get("scorecard", {}))
            pipeline.write_json(path, ledger.rescore(data))
            after = data["scorecard"]
            changed = {k: (before.get(k), after[k]) for k in after if before.get(k) != after[k]}
            print(f"  {data['symbol']:11s} {'unchanged' if not changed else changed}")
        pipeline.write_json(config.RESEARCH_DIR / "index.json", pipeline.research_index())
    elif args.command == "alerts":
        from .push import signals

        portfolio = config.Portfolio.load()
        prices = pipeline.read_json(config.DATA_DIR / "prices.json")
        if not prices:
            print("No prices yet. Run: python -m finn push", file=sys.stderr)
            return 1
        th = portfolio.thresholds
        th.volume_multiple = args.volume or th.volume_multiple
        th.price_move_pct = args.move or th.price_move_pct
        found = signals.detect(prices, th)
        print(f"{len(found)} signals in the last 30 sessions at volume ≥ {th.volume_multiple}× and |move| ≥ {th.price_move_pct}%")
        for sig in found[:40]:
            vol = f"{sig['volume_multiple']:.1f}×" if sig["volume_multiple"] else "  n/a"
            idx = f"{sig['index_move_pct']:+.1f}%" if sig["index_move_pct"] is not None else " n/a"
            print(f"  {sig['date']}  {sig['symbol']:11s} {sig['move_pct']:+6.2f}%  vol {vol:>6s}  nifty {idx}  [{'+'.join(sig['kinds'])}]")
    elif args.command == "serve":
        import uvicorn

        config.load_env()
        uvicorn.run("finn.server:app", host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
