"""Paths, portfolio and model routing. Everything tunable lives here or in the environment."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = Path(os.environ.get("FINN_CACHE_DIR", ROOT / ".cache"))
WEB_DIR = ROOT / "web"
DATA_DIR = WEB_DIR / "data"
RESEARCH_DIR = DATA_DIR / "research"

# Announcements older than this fall out of the published feed.
FEED_RETENTION_DAYS = 60
# How many concalls the promise ledger reads. Four is the brief's worked example; two more
# give earlier promises time to come due.
MAX_TRANSCRIPTS = 6


def load_env(path: Path | None = None) -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing environment variables win."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class Thresholds:
    volume_multiple: float = 2.0
    price_move_pct: float = 4.0
    lookback_sessions: int = 20


@dataclass
class Portfolio:
    symbols: list[str] = field(default_factory=list)
    research_extra: list[str] = field(default_factory=list)
    thresholds: Thresholds = field(default_factory=Thresholds)

    @classmethod
    def load(cls, path: Path | None = None) -> "Portfolio":
        path = path or ROOT / "portfolio.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            symbols=[s.strip().upper() for s in raw.get("portfolio", [])],
            research_extra=[s.strip().upper() for s in raw.get("research_extra", [])],
            thresholds=Thresholds(**raw.get("thresholds", {})),
        )


# --- Model routing -----------------------------------------------------------------
# Three tiers. Each is an ordered list of candidates; the client falls through on quota or
# server errors, so a free-tier daily cap on one model degrades to the next instead of
# failing the run. Override any tier with e.g. FINN_MODELS_FAST="gemini:gemini-3.1-flash-lite".
#
#   fast   — bulk reading: one call per filing or transcript. Cheapest model that is reliable.
#   smart  — the one synthesis call per company where reasoning quality shows.
#   search — optional web-grounded lookups for push-side bios (never used on the pull side);
#            empty by default because grounding needs a paid key.
#
# Prices are USD per 1M tokens (input, output) at paid list rates, used only to report what a
# run *would* cost; the default models are all on Google's free tier.
DEFAULT_ROUTES: dict[str, list[str]] = {
    "fast": [
        "gemini:gemini-3.1-flash-lite",
        "gemini:gemini-3.5-flash-lite",
    ],
    "smart": [
        "gemini:gemini-3.6-flash",  # 3.8-flash hung on the free tier in testing (Nov 2026); kept as a fallback
        "gemini:gemini-3.8-flash",
        "gemini:gemini-3.5-flash-lite",
    ],
    # Web-search grounding is not part of the Gemini free tier for current models (Nov 2026), so
    # the bio lookup is opt-in: set FINN_MODELS_SEARCH="gemini:gemini-3.6-flash" with a paid key.
    "search": [],
}

LIST_PRICES: dict[str, tuple[float, float]] = {
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.6-flash": (0.75, 3.75),
}

# Requests per minute we allow ourselves per model (free-tier friendly).
RPM: dict[str, int] = {
    "gemini-3.1-flash-lite": 10,
    "gemini-3.5-flash-lite": 10,
    "gemini-2.5-flash-lite": 10,
    "gemini-3.8-flash": 5,
    "gemini-3.6-flash": 5,
}
DEFAULT_RPM = 8


def routes() -> dict[str, list[str]]:
    out = {}
    for tier, default in DEFAULT_ROUTES.items():
        override = os.environ.get(f"FINN_MODELS_{tier.upper()}")
        out[tier] = [m.strip() for m in override.split(",") if m.strip()] if override else list(default)
    return out
