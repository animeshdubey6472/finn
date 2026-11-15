"""Build docs/accuracy/labels.csv: a sample of FINN's decisions for a human to label.

Section (a): up to 30 filings, stratified across tiers (act / know / buried).
Section (b): every guidance item whose ledger verdict was actually scored.

Stdlib only. Run from anywhere:  python docs/accuracy/make_sheet.py
"""
from __future__ import annotations

import csv
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "web" / "data"
OUT = Path(__file__).resolve().parent / "labels.csv"
PER_TIER = 10
SEED = 7  # fixed so the sample is reproducible

COLUMNS = [
    "kind", "id", "symbol", "date", "title_or_statement", "finn_tier", "finn_category", "finn_headline",
    "target_text", "finn_verdict", "verdict_detail", "quote", "url",
    "human_important", "human_category", "human_verdict_correct", "notes",
]


def filings() -> list[dict]:
    feed = json.loads((DATA / "feed.json").read_text(encoding="utf-8"))
    rng = random.Random(SEED)
    rows = []
    for tier in ("act", "know", "buried"):
        items = [i for i in feed.get("items", []) if i.get("tier") == tier]
        rng.shuffle(items)
        for item in sorted(items[:PER_TIER], key=lambda i: i["ts"], reverse=True):
            rows.append({
                "kind": "filing", "id": item["id"], "symbol": item["symbol"], "date": item["ts"][:10],
                "title_or_statement": item["title"], "finn_tier": tier, "finn_category": item.get("category", ""),
                "finn_headline": item.get("headline") or "", "url": item["url"],
            })
    return rows


def guidance() -> list[dict]:
    rows = []
    for path in sorted((DATA / "research").glob("*.json")):
        if path.name == "index.json":
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for g in data.get("guidance", []):
            verdict = g.get("verdict", {})
            if verdict.get("status") in (None, "unmeasured"):
                continue
            url = g.get("url", "")
            if g.get("page"):
                url = f"{url}#page={g['page']}"
            rows.append({
                "kind": "guidance", "id": f"{data['symbol']}:{g['id']}", "symbol": data["symbol"], "date": g.get("quarter", ""),
                "title_or_statement": g.get("statement", ""), "target_text": g.get("target_text") or "",
                "finn_verdict": verdict.get("status", ""), "verdict_detail": verdict.get("detail", ""),
                "quote": g.get("quote") or "", "url": url,
            })
    return rows


def main() -> None:
    a, b = filings(), guidance()
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for row in a + b:
            writer.writerow({col: row.get(col, "") for col in COLUMNS})
    print(f"wrote {OUT.relative_to(ROOT)}: {len(a)} filings, {len(b)} guidance items")


if __name__ == "__main__":
    main()
