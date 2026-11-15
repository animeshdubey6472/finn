"""The scheduled-refresh gate: an unsafe snapshot must never be published."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import snapshot  # noqa: E402


def _write(d: Path, feed: dict, prices: dict):
    d.mkdir(parents=True, exist_ok=True)
    (d / "feed.json").write_text(json.dumps(feed))
    (d / "prices.json").write_text(json.dumps(prices))


def _feed(n_items, loaded=18, ts="2026-11-16T13:30:00+00:00"):
    items = [{"url": f"https://x/{i}.pdf", "related": []} for i in range(n_items)]
    return {"generated_at": ts, "items": items, "portfolio": [{}] * 18, "status": {"filings": {"companies_loaded": loaded}}}


def test_validation_gate(tmp_path, monkeypatch):
    data, prev = tmp_path / "data", tmp_path / "prev"
    monkeypatch.setattr(snapshot, "DATA", data)
    good_prices = {"as_of": "2026-11-16", "symbols": {"A": {}}}
    _write(prev, _feed(50, ts="2026-11-15T13:30:00+00:00"), {"as_of": "2026-11-13", "symbols": {"A": {}}})

    _write(data, _feed(52), good_prices)
    assert snapshot.validate(prev) == []                                    # healthy run publishes

    _write(data, _feed(52, loaded=5), good_prices)
    assert any("companies loaded" in p for p in snapshot.validate(prev))   # most sources failed

    _write(data, _feed(30), good_prices)
    assert any("disappeared" in p for p in snapshot.validate(prev))        # history lost

    _write(data, _feed(52), {"as_of": "2026-11-10", "symbols": {"A": {}}})
    assert any("backwards" in p for p in snapshot.validate(prev))          # stale prices

    _write(data, {"items": []}, good_prices)
    assert snapshot.validate(prev) == ["feed.json missing or empty"]
