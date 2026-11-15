"""End-of-day price and volume signals.

"Unusual" is the investor's call, so this module computes two plain measures per session and
leaves the thresholds to the caller (the app re-applies the user's own thresholds client-side
with the same definitions):

    move      close vs previous close, in percent
    volume    today's volume ÷ the average of the previous N sessions (today excluded)

Each signal also carries the index move for the day, so a stock falling 4% with the market
down 3% reads differently from one falling 4% on a flat tape.
"""
from __future__ import annotations

from ..config import Thresholds


def daily_metrics(series: dict, lookback: int = 20) -> list[dict]:
    """Per-session move % and volume multiple. `series` is {'d': dates, 'c': closes, 'v': volumes}."""
    dates, closes, volumes = series["d"], series["c"], series["v"]
    out = []
    for i in range(1, len(dates)):
        prev = closes[i - 1]
        move = (closes[i] / prev - 1) * 100 if prev else None
        window = [v for v in volumes[max(0, i - lookback) : i] if v]
        average = sum(window) / len(window) if len(window) >= max(5, lookback // 2) else None
        multiple = volumes[i] / average if average and volumes[i] else None
        out.append({"date": dates[i], "close": closes[i], "move_pct": None if move is None else round(move, 2),
                    "volume": volumes[i], "volume_multiple": None if multiple is None else round(multiple, 2)})
    return out


def detect(prices: dict, thresholds: Thresholds, sessions: int = 30) -> list[dict]:
    """Signals over the last `sessions` trading days for every symbol, newest first."""
    index_moves = {}
    if prices.get("index"):
        index_moves = {m["date"]: m["move_pct"] for m in daily_metrics(prices["index"], thresholds.lookback_sessions)}
    signals = []
    for symbol, series in prices.get("symbols", {}).items():
        for day in daily_metrics(series, thresholds.lookback_sessions)[-sessions:]:
            kinds = []
            if day["move_pct"] is not None and abs(day["move_pct"]) >= thresholds.price_move_pct:
                kinds.append("price")
            if day["volume_multiple"] is not None and day["volume_multiple"] >= thresholds.volume_multiple:
                kinds.append("volume")
            if kinds:
                signals.append({"symbol": symbol, "kinds": kinds, "index_move_pct": index_moves.get(day["date"]), **day})
    return sorted(signals, key=lambda s: (s["date"], abs(s["move_pct"] or 0)), reverse=True)


def latest_snapshot(prices: dict, lookback: int = 20) -> dict[str, dict]:
    """Last session's move and volume multiple per symbol, for the portfolio strip."""
    out = {}
    for symbol, series in prices.get("symbols", {}).items():
        metrics = daily_metrics(series, lookback)
        if metrics:
            last = metrics[-1]
            closes = series["c"]
            out[symbol] = {
                "date": last["date"], "close": last["close"], "move_pct": last["move_pct"], "volume_multiple": last["volume_multiple"],
                "high_52w": max(closes), "low_52w": min(closes),
                "move_1m_pct": round((closes[-1] / closes[-22] - 1) * 100, 1) if len(closes) > 22 and closes[-22] else None,
            }
    return out
