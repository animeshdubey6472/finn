"""Helpers for the scheduled refresh (used by .github/workflows/refresh.yml and pages.yml).

    python scripts/snapshot.py restore <site_url>   pull the published feed/prices/meta if newer than the repo's
    python scripts/snapshot.py validate <prev_dir>  compare a fresh snapshot with the previous one; exit 1 if unsafe

The refresh never commits to the repository: it rebuilds the data on the runner and deploys the
site only when validation passes, so a bad run can never replace the published snapshot.
"""
from __future__ import annotations

import json
import shutil
import sys
import urllib.request
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "web" / "data"
FILES = ("feed.json", "prices.json", "meta.json")
MIN_COMPANIES_SHARE = 0.75   # at least 75% of the portfolio must load
MAX_FILINGS_LOSS = 0.10      # retention can age a few filings out; more than 10% gone is a failure


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def restore(site: str) -> None:
    """Use the published snapshot when it is newer than the repo's copy (feed history carries forward)."""
    site = site.rstrip("/") + "/data/"
    try:
        live = {name: json.loads(urllib.request.urlopen(site + name, timeout=30).read().decode("utf-8")) for name in FILES}
    except Exception as exc:  # noqa: BLE001 - no published snapshot yet, or network trouble
        print(f"restore: using the repo snapshot ({type(exc).__name__})")
        return
    local = _load(DATA / "feed.json") or {}
    if live["feed.json"].get("generated_at", "") > local.get("generated_at", ""):
        for name, data in live.items():
            (DATA / name).write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"restore: published snapshot {live['feed.json']['generated_at']} is newer; using it")
    else:
        print("restore: repo snapshot is current")


def _urls(feed: dict) -> set[str]:
    return {i["url"] for i in feed.get("items", [])} | {r["url"] for i in feed.get("items", []) for r in i.get("related", [])}


def validate(prev_dir: Path) -> list[str]:
    """Reasons the fresh snapshot is unsafe to publish (empty list = safe)."""
    problems = []
    new_feed, old_feed = _load(DATA / "feed.json"), _load(prev_dir / "feed.json")
    new_prices, old_prices = _load(DATA / "prices.json"), _load(prev_dir / "prices.json")
    if not new_feed or not new_feed.get("items"):
        return ["feed.json missing or empty"]
    status = (new_feed.get("status") or {}).get("filings") or {}
    portfolio = len(new_feed.get("portfolio") or [])
    loaded = status.get("companies_loaded", 0)
    if portfolio and loaded < MIN_COMPANIES_SHARE * portfolio:
        problems.append(f"only {loaded} of {portfolio} companies loaded")
    if old_feed:
        lost = _urls(old_feed) - _urls(new_feed)
        if len(lost) > MAX_FILINGS_LOSS * max(1, len(_urls(old_feed))):
            problems.append(f"{len(lost)} previously published filings disappeared")
        if new_feed.get("generated_at", "") < old_feed.get("generated_at", ""):
            problems.append("fresh feed is older than the published one")
    if not new_prices or not new_prices.get("symbols"):
        problems.append("prices.json missing or empty")
    elif old_prices and (new_prices.get("as_of") or "") < (old_prices.get("as_of") or ""):
        problems.append("prices went backwards in time")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "restore":
        restore(argv[1])
        return 0
    if len(argv) >= 2 and argv[0] == "snapshot-to":
        target = Path(argv[1])
        target.mkdir(parents=True, exist_ok=True)
        for name in FILES:
            if (DATA / name).exists():
                shutil.copy(DATA / name, target / name)
        return 0
    if len(argv) >= 2 and argv[0] == "validate":
        problems = validate(Path(argv[1]))
        for p in problems:
            print(f"validate: UNSAFE — {p}")
        if not problems:
            print("validate: snapshot is safe to publish")
        return 1 if problems else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
