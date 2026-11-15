"""Server: the app plus a live research API for any NSE symbol.

    GET /api/health                     model + data-source reachability (cached briefly)
    GET /api/research/{symbol}          cached research if fresh, else 404 {"cached": false}
    GET /api/research/{symbol}/stream   Server-Sent Events: progress lines, then the result

Built to run on a small public host as well as locally:
  * CORS only for the configured origins (the GitHub Pages app by default);
  * one research run at a time (a second caller gets a clear "busy" event, not a queue);
  * a per-IP hourly limit and a global daily cap on fresh runs (cached answers are free);
  * fresh cached research is served without a model call.
Configuration is by environment variable (see `Limits`); the Gemini key never leaves the host.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config, pipeline
from .llm import LLM
from .net import USER_AGENT, fetcher
from .pull import research as research_mod
from .sources.screener import UnknownSymbol

config.load_env()
_SYMBOL = re.compile(r"^[A-Z0-9&\-]{1,20}$")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Limits:
    origins: list[str] = field(default_factory=lambda: [o.strip() for o in os.environ.get(
        "FINN_ALLOWED_ORIGINS", "https://animeshdubey6472.github.io").split(",") if o.strip()])
    per_ip_per_hour: int = field(default_factory=lambda: _env_int("FINN_RUNS_PER_IP_PER_HOUR", 3))
    per_day: int = field(default_factory=lambda: _env_int("FINN_RUNS_PER_DAY", 20))
    fresh_hours: int = field(default_factory=lambda: _env_int("FINN_CACHE_HOURS", 24))
    max_transcripts: int = field(default_factory=lambda: _env_int("FINN_LIVE_TRANSCRIPTS", config.MAX_TRANSCRIPTS))
    annual_report: bool = field(default_factory=lambda: os.environ.get("FINN_LIVE_ANNUAL_REPORT", "1") != "0")


class Gate:
    """Admission control for fresh research runs: one at a time, per-IP and daily limits."""

    def __init__(self, limits: Limits):
        self.limits = limits
        self.lock = threading.Lock()
        self.running: str | None = None
        self.by_ip: dict[str, deque] = defaultdict(deque)
        self.day = time.strftime("%Y-%m-%d")
        self.today = 0

    def admit(self, ip: str, symbol: str, now: float | None = None) -> str | None:
        """None if the run may start (and it is recorded); otherwise the reason it may not."""
        now = now or time.time()
        with self.lock:
            day = time.strftime("%Y-%m-%d", time.gmtime(now))
            if day != self.day:
                self.day, self.today = day, 0
            if self.running:
                return f"busy: {self.running} is being researched; try again in a minute"
            if self.today >= self.limits.per_day:
                return "daily limit reached for live research on this demo server; try again tomorrow or run locally"
            recent = self.by_ip[ip]
            while recent and now - recent[0] > 3600:
                recent.popleft()
            if len(recent) >= self.limits.per_ip_per_hour:
                return "rate limited: a few live runs per hour per visitor; precomputed research is still available"
            recent.append(now)
            self.today += 1
            self.running = symbol
            return None

    def release(self) -> None:
        with self.lock:
            self.running = None


LIMITS = Limits()
GATE = Gate(LIMITS)
app = FastAPI(title="FINN", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=LIMITS.origins, allow_methods=["GET"], allow_headers=["*"], max_age=3600)


def _clean(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if not _SYMBOL.match(symbol):
        raise HTTPException(400, "Not a valid NSE symbol")
    return symbol


def _cached(symbol: str, max_age: float | None) -> dict | None:
    path = config.RESEARCH_DIR / f"{research_mod.safe_name(symbol)}.json"
    if not path.exists() or (max_age is not None and time.time() - path.stat().st_mtime > max_age):
        return None
    return pipeline.read_json(path)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


# --- health ----------------------------------------------------------------------------

_health_cache: dict = {"at": 0.0, "body": None}
_PROBE_PDF = "https://www.bseindia.com/xml-data/corpfiling/AttachHis/6ad82347-ac2d-4385-ae80-3cd07c428f69.pdf"


def _probe(url: str, method: str = "HEAD") -> bool:
    try:
        resp = requests.request(method, url, headers={"User-Agent": USER_AGENT}, timeout=8, allow_redirects=True, stream=True)
        resp.close()
        return resp.status_code < 400
    except requests.RequestException:
        return False


def _model_reachable() -> bool:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        return False
    try:
        resp = requests.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1", headers={"x-goog-api-key": key}, timeout=8)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def health_body() -> dict:
    """What a caller needs to decide whether live research can work right now (cached 5 minutes)."""
    if _health_cache["body"] and time.time() - _health_cache["at"] < 300:
        return {**_health_cache["body"], "busy": GATE.running is not None}
    llm = LLM()
    checks = {
        "model_configured": llm.available("fast") and llm.available("smart"),
        "model_reachable": _model_reachable(),
        "fundamentals_source": _probe("https://www.screener.in/"),
        "filings_pdf_source": _probe(_PROBE_PDF),
    }
    body = {
        "ok": all(checks.values()),
        "checks": checks,
        "limits": {"per_ip_per_hour": LIMITS.per_ip_per_hour, "per_day": LIMITS.per_day, "runs_today": GATE.today},
        "fast": checks["model_configured"] and checks["model_reachable"],  # kept for older clients
    }
    _health_cache.update(at=time.time(), body=body)
    return {**body, "busy": GATE.running is not None}


@app.get("/api/health")
def health() -> dict:
    return health_body()


@app.get("/api/research/{symbol}")
def get_research(symbol: str):
    data = _cached(_clean(symbol), LIMITS.fresh_hours * 3600)
    return data if data else JSONResponse({"cached": False}, status_code=404)


@app.get("/api/research/{symbol}/stream")
async def stream_research(symbol: str, request: Request, fresh: bool = False):
    symbol = _clean(symbol)
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def emit(kind: str, payload) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (kind, payload))

    cached = None if fresh else _cached(symbol, LIMITS.fresh_hours * 3600)
    if cached:  # cached answers cost nothing and are not rate limited
        emit("result", cached)
    else:
        refusal = GATE.admit(_client_ip(request), symbol)
        if refusal:
            emit("refused", {"message": refusal})
        else:
            def work() -> None:
                try:
                    data = research_mod.build(symbol, llm=LLM(), net=fetcher(), with_annual_report=LIMITS.annual_report,
                                              max_transcripts=LIMITS.max_transcripts,
                                              progress=lambda s, d: emit("progress", {"stage": s, "detail": d}))
                    research_mod.save(data)
                    pipeline.write_json(config.RESEARCH_DIR / "index.json", pipeline.research_index())
                    emit("result", data)
                except UnknownSymbol:
                    emit("failed", {"message": f"{symbol} was not found. Check the NSE symbol."})
                except Exception as exc:  # noqa: BLE001
                    emit("failed", {"message": f"Research failed partway ({type(exc).__name__}). Precomputed research is still available."})
                finally:
                    GATE.release()

            threading.Thread(target=work, daemon=True).start()

    async def events():
        while True:
            kind, payload = await queue.get()
            yield f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            if kind in {"result", "failed", "refused"}:
                break

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/")
def index():
    return FileResponse(config.WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=config.WEB_DIR), name="web")
