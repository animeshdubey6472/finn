"""Local server: serves the app and a live research endpoint for any NSE symbol.

    GET /api/research/{symbol}         cached research if fresh, else 404 with {"cached": false}
    GET /api/research/{symbol}/stream  Server-Sent Events: progress lines, then the result
    GET /api/health                    model availability

The static app works without this server (it reads web/data/). With it, the search box can
research a company that was never precomputed.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config, pipeline
from .llm import LLM
from .net import fetcher
from .pull import research as research_mod
from .sources.screener import UnknownSymbol

config.load_env()
app = FastAPI(title="FINN", docs_url=None, redoc_url=None)
_SYMBOL = re.compile(r"^[A-Z0-9&\-]{1,20}$")
_FRESH_S = 24 * 3600
_running: dict[str, threading.Event] = {}
_lock = threading.Lock()


def _clean(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if not _SYMBOL.match(symbol):
        raise HTTPException(400, "Not a valid NSE symbol")
    return symbol


def _cached(symbol: str, max_age: float | None = _FRESH_S) -> dict | None:
    path = config.RESEARCH_DIR / f"{research_mod.safe_name(symbol)}.json"
    if not path.exists() or (max_age is not None and time.time() - path.stat().st_mtime > max_age):
        return None
    return pipeline.read_json(path)


@app.get("/api/health")
def health() -> dict:
    llm = LLM()
    return {"fast": llm.available("fast"), "smart": llm.available("smart")}


@app.get("/api/research/{symbol}")
def get_research(symbol: str):
    data = _cached(_clean(symbol))
    return data if data else JSONResponse({"cached": False}, status_code=404)


@app.get("/api/research/{symbol}/stream")
async def stream_research(symbol: str, fresh: bool = False):
    symbol = _clean(symbol)
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def emit(kind: str, payload) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (kind, payload))

    def work() -> None:
        with _lock:
            busy = symbol in _running
            done = _running.setdefault(symbol, threading.Event())
        if busy:  # someone else is already researching this symbol: wait for their result
            emit("progress", {"stage": "queued", "detail": "Already being researched — waiting"})
            done.wait(timeout=900)
            data = _cached(symbol, None)
            emit("result" if data else "error", data or {"message": "Research failed"})
            return
        try:
            data = None if fresh else _cached(symbol)
            if data is None:
                data = research_mod.build(symbol, llm=LLM(), net=fetcher(), progress=lambda s, d: emit("progress", {"stage": s, "detail": d}))
                research_mod.save(data)
                pipeline.write_json(config.RESEARCH_DIR / "index.json", pipeline.research_index())
            emit("result", data)
        except UnknownSymbol:
            emit("error", {"message": f"{symbol} was not found. Check the NSE symbol."})
        except Exception as exc:  # noqa: BLE001
            emit("error", {"message": f"Research failed: {type(exc).__name__}"})
        finally:
            with _lock:
                _running.pop(symbol, None)
            done.set()

    threading.Thread(target=work, daemon=True).start()

    async def events():
        while True:
            kind, payload = await queue.get()
            yield f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            if kind in {"result", "error"}:
                break

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/")
def index():
    return FileResponse(config.WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=config.WEB_DIR), name="web")
