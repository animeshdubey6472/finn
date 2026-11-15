"""Live research server: admission control, CORS, caching, health (no network, no model)."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from finn import config, server


def test_gate_one_run_at_a_time_then_per_ip_then_daily():
    gate = server.Gate(server.Limits(origins=[], per_ip_per_hour=2, per_day=3))
    now = 1_800_000_000.0
    assert gate.admit("1.1.1.1", "TCS", now) is None
    assert gate.admit("2.2.2.2", "ITC", now).startswith("busy")          # one at a time
    gate.release()
    assert gate.admit("1.1.1.1", "ITC", now + 1) is None; gate.release()
    assert "rate limited" in gate.admit("1.1.1.1", "LT", now + 2)        # 2 per IP per hour
    assert gate.admit("3.3.3.3", "LT", now + 3) is None; gate.release()
    assert "daily limit" in gate.admit("4.4.4.4", "HAL", now + 4)        # 3 per day across everyone
    assert gate.admit("4.4.4.4", "HAL", now + 86400) is None             # resets the next day


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESEARCH_DIR", tmp_path)
    monkeypatch.setattr(server, "GATE", server.Gate(server.Limits(origins=["https://animeshdubey6472.github.io"], per_ip_per_hour=1, per_day=1)))
    return TestClient(server.app)


def test_cors_only_for_the_pages_origin(client, monkeypatch):
    monkeypatch.setattr(server, "health_body", lambda: {"ok": True, "busy": False})
    ok = client.get("/api/health", headers={"Origin": "https://animeshdubey6472.github.io"})
    other = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert ok.headers.get("access-control-allow-origin") == "https://animeshdubey6472.github.io"
    assert "access-control-allow-origin" not in other.headers


def test_fresh_cache_is_served_without_a_run(client, tmp_path):
    (tmp_path / "TCS.json").write_text(json.dumps({"symbol": "TCS", "cached_marker": True}))
    body = client.get("/api/research/TCS/stream").text
    assert "event: result" in body and "cached_marker" in body
    body2 = client.get("/api/research/TCS/stream").text                 # cached answers are not rate limited
    assert "event: result" in body2
    assert server.GATE.today == 0


def test_busy_server_refuses_clearly(client):
    server.GATE.running = "ITC"
    body = client.get("/api/research/LT/stream").text
    assert "event: refused" in body and "busy" in body


def test_invalid_symbol_rejected(client):
    assert client.get("/api/research/..%2Fetc/stream").status_code in (400, 404)
    assert client.get("/api/research/BAD$SYM").status_code == 400


def test_health_reports_each_dependency(monkeypatch):
    server._health_cache.update(at=0.0, body=None)
    monkeypatch.setattr(server, "_model_reachable", lambda: True)
    monkeypatch.setattr(server, "_probe", lambda url, method="HEAD": "screener" in url)
    body = server.health_body()
    assert body["checks"]["fundamentals_source"] is True and body["checks"]["filings_pdf_source"] is False
    assert body["ok"] is False and "busy" in body and body["limits"]["per_day"] >= 1
