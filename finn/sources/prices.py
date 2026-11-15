"""End-of-day price and volume via yfinance (NSE first, BSE as fallback).

Close is split-adjusted by Yahoo, so a bonus or split does not masquerade as a -50% day.
The Nifty 50 comes along so the app can tell a stock-specific move from a market-wide one.
"""
from __future__ import annotations

import math
import warnings
from datetime import datetime, timezone

INDEX = "^NSEI"


def yahoo_ticker(symbol: str, exchange: str = "NS") -> str:
    return symbol if symbol.startswith("^") else f"{symbol}.{exchange}"


def _series(frame) -> dict | None:
    """yfinance frame for one ticker -> {'d': [...], 'c': [...], 'v': [...]} or None if empty."""
    if frame is None or frame.empty or "Close" not in frame:
        return None
    dates, closes, volumes = [], [], []
    for ts, row in frame.iterrows():
        close = row.get("Close")
        if close is None or (isinstance(close, float) and math.isnan(close)):
            continue
        vol = row.get("Volume")
        dates.append(ts.strftime("%Y-%m-%d"))
        closes.append(round(float(close), 2))
        volumes.append(0 if vol is None or (isinstance(vol, float) and math.isnan(vol)) else int(vol))
    return {"d": dates, "c": closes, "v": volumes} if len(dates) >= 2 else None


def fetch_history(symbols: list[str], period: str = "1y") -> dict:
    """{'as_of': 'YYYY-MM-DD', 'index': {...}, 'symbols': {SYM: {'d','c','v'}}, 'missing': [...]}"""
    import yfinance as yf  # imported lazily: the research path never needs pandas

    warnings.filterwarnings("ignore")

    def download(tickers: list[str]):
        return yf.download(
            tickers, period=period, interval="1d", auto_adjust=False,
            progress=False, group_by="ticker", threads=False,
        )

    out: dict = {"symbols": {}, "missing": []}
    wanted = {yahoo_ticker(s): s for s in symbols}
    frame = download(list(wanted) + [INDEX])
    present = set(frame.columns.get_level_values(0)) if hasattr(frame.columns, "levels") else set()

    for ticker, symbol in wanted.items():
        series = _series(frame[ticker]) if ticker in present else None
        if series is None:  # not on NSE under this code: try BSE once
            alt = yahoo_ticker(symbol, "BO")
            try:
                alt_frame = download([alt])
                sub = alt_frame[alt] if hasattr(alt_frame.columns, "levels") else alt_frame
                series = _series(sub)
            except Exception:  # noqa: BLE001 - a missing ticker must not sink the batch
                series = None
        if series is None:
            out["missing"].append(symbol)
        else:
            out["symbols"][symbol] = series

    out["index"] = _series(frame[INDEX]) if INDEX in present else None
    last_dates = [s["d"][-1] for s in out["symbols"].values()]
    out["as_of"] = max(last_dates) if last_dates else None
    out["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return out
