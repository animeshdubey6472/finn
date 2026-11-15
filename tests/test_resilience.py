"""Failure scenarios: a refresh that fails must never replace good data, and must say so."""
import json
from types import SimpleNamespace

import pytest

from finn import config, pipeline


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESEARCH_DIR", tmp_path / "research")
    (tmp_path / "research").mkdir()
    return tmp_path


def test_price_failure_keeps_last_good_file_and_reports_it(data_dir, monkeypatch):
    good = {"as_of": "2026-11-13", "symbols": {"TITAN": {"d": ["2026-11-12", "2026-11-13"], "c": [1, 2], "v": [3, 4]}}}
    (data_dir / "prices.json").write_text(json.dumps(good))
    import finn.sources.prices as prices_mod

    def boom(symbols):
        raise ConnectionError("blocked")

    monkeypatch.setattr(prices_mod, "fetch_history", boom)
    status = {}
    assert pipeline.refresh_prices(["TITAN"], status) is None
    assert json.loads((data_dir / "prices.json").read_text()) == good  # untouched
    assert status["ok"] is False and "ConnectionError" in status["error"]


def test_empty_price_response_does_not_overwrite(data_dir, monkeypatch):
    good = {"as_of": "2026-11-13", "symbols": {"TITAN": {}}}
    (data_dir / "prices.json").write_text(json.dumps(good))
    import finn.sources.prices as prices_mod
    monkeypatch.setattr(prices_mod, "fetch_history", lambda symbols: {"symbols": {}, "missing": symbols, "as_of": None})
    status = {}
    assert pipeline.refresh_prices(["TITAN"], status) is None
    assert json.loads((data_dir / "prices.json").read_text()) == good
    assert status["ok"] is False


def test_total_outage_leaves_published_feed_untouched():
    portfolio = SimpleNamespace(symbols=["TITAN"], thresholds=SimpleNamespace(lookback_sessions=20))
    previous = {"generated_at": "2026-11-15T14:34:31+00:00", "items": [{"id": "x"}]}
    assert pipeline.push_snapshot(portfolio, {}, {"TITAN": "fetch failed"}, None, {}, previous, llm=None, net=None) is None


def test_failed_company_keeps_its_last_good_row_marked_stale():
    previous = [{"symbol": "TITAN", "name": "Titan", "refreshed_at": "2026-11-14T10:00:00+00:00"},
                {"symbol": "ITC", "name": "ITC", "refreshed_at": "2026-11-14T10:00:00+00:00"}]
    rows = pipeline.merge_portfolio_rows([{"symbol": "ITC", "name": "ITC"}], previous, ["TITAN", "ITC"],
                                         {"TITAN": "fetch failed (Timeout)"}, "2026-11-15T10:00:00+00:00")
    by = {r["symbol"]: r for r in rows}
    assert [r["symbol"] for r in rows] == ["TITAN", "ITC"]  # portfolio order kept
    assert by["TITAN"]["stale"] is True and by["TITAN"]["refreshed_at"] == "2026-11-14T10:00:00+00:00"
    assert by["TITAN"]["stale_reason"] == "fetch failed (Timeout)"
    assert by["ITC"]["stale"] is False and by["ITC"]["refreshed_at"] == "2026-11-15T10:00:00+00:00"


def _research(transcripts, synthesis=True, annual_report=True):
    return {"symbol": "X", "coverage": {"transcripts_read": transcripts, "synthesis": synthesis, "annual_report": annual_report}}


def test_degraded_research_never_replaces_complete_research():
    full = _research(6)
    assert pipeline.should_replace_research(full, _research(0, synthesis=False, annual_report=False))[0] is False  # e.g. no model key
    assert pipeline.should_replace_research(full, _research(6, synthesis=False))[0] is False
    assert pipeline.should_replace_research(full, _research(4))[0] is False
    assert pipeline.should_replace_research(full, _research(6))[0] is True
    assert pipeline.should_replace_research(None, _research(0, False, False))[0] is True  # first run: anything beats nothing


def test_run_research_keeps_earlier_file_when_new_run_is_degraded(data_dir, monkeypatch):
    full = {"symbol": "TITAN", "name": "Titan", "generated_at": "2026-11-15T10:00:00+00:00", "synthesis": {}, "scorecard": {},
            "coverage": {"transcripts_read": 6, "synthesis": True, "annual_report": True}, "fundamentals": {"flags": []}}
    (data_dir / "research" / "TITAN.json").write_text(json.dumps(full))
    degraded = {**full, "generated_at": "2026-11-16T10:00:00+00:00", "coverage": {"transcripts_read": 0, "synthesis": False, "annual_report": False}}
    monkeypatch.setattr(pipeline.research_mod, "build", lambda *a, **k: degraded)
    errors = {}
    assert pipeline.run_research(["TITAN"], llm=None, net=None, errors=errors) == []
    assert json.loads((data_dir / "research" / "TITAN.json").read_text())["generated_at"] == "2026-11-15T10:00:00+00:00"
    assert "kept earlier research" in errors["TITAN"]
