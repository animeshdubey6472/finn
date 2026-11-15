"""Score a filled-in labels.csv and write docs/accuracy/RESULTS.md.

Metrics
  act precision  share of filings FINN put in "act" that the labeller marked important
  recall         share of filings marked important that FINN put in "act" or "know" (not hidden)
  ledger accuracy share of scored guidance verdicts the labeller marked correct

Stdlib only:  python docs/accuracy/score.py
"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
YES = {"yes", "y", "1", "true"}
NO = {"no", "n", "0", "false"}


def ratio(hits: int, total: int) -> str:
    return f"{hits}/{total} ({hits / total * 100:.0f}%)" if total else "n/a (nothing labelled)"


def main() -> None:
    rows = list(csv.DictReader((HERE / "labels.csv").open(encoding="utf-8")))
    filings = [r for r in rows if r["kind"] == "filing" and r["human_important"].strip().lower() in YES | NO]
    important = lambda r: r["human_important"].strip().lower() in YES  # noqa: E731

    act = [r for r in filings if r["finn_tier"] == "act"]
    act_hits = sum(important(r) for r in act)
    wanted = [r for r in filings if important(r)]
    surfaced = sum(r["finn_tier"] in ("act", "know") for r in wanted)
    labelled_cat = [r for r in filings if r["human_category"].strip()]
    cat_hits = sum(r["human_category"].strip().lower() == r["finn_category"].lower() for r in labelled_cat)

    ledger = [r for r in rows if r["kind"] == "guidance" and r["human_verdict_correct"].strip().lower() in YES | NO]
    ledger_hits = sum(r["human_verdict_correct"].strip().lower() in YES for r in ledger)

    table = [
        ("Filings labelled", str(len(filings))),
        ('"Needs you" precision — act items a human called important', ratio(act_hits, len(act))),
        ("Recall — important filings FINN surfaced (act or know, not hidden)", ratio(surfaced, len(wanted))),
        ("Category agreement (where a category was labelled)", ratio(cat_hits, len(labelled_cat))),
        ("Ledger verdicts labelled", str(len(ledger))),
        ("Ledger accuracy — verdicts a human confirmed", ratio(ledger_hits, len(ledger))),
    ]
    for label, value in table:
        print(f"{label:70s} {value}")

    misses = [r for r in wanted if r["finn_tier"] == "buried"] + [r for r in ledger if r["human_verdict_correct"].strip().lower() in NO]
    lines = [
        "# Accuracy check", "",
        f"Hand-labelled sample, scored {date.today().isoformat()}. Sample built by `make_sheet.py` "
        "(filings stratified across tiers; every scored ledger verdict).", "",
        "| Metric | Result |", "|---|---|",
        *[f"| {label} | {value} |" for label, value in table], "",
    ]
    if misses:
        lines += ["## Disagreements", ""]
        lines += [f"- **{r['symbol']}** ({r['kind']}): {r['title_or_statement'][:110]} — FINN: {r['finn_tier'] or r['finn_verdict']}"
                  + (f"; note: {r['notes']}" if r["notes"].strip() else "") for r in misses]
        lines.append("")
    (HERE / "RESULTS.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {(HERE / 'RESULTS.md').name}")


if __name__ == "__main__":
    main()
