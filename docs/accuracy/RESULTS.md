# Accuracy check

Labelled by the build agent (Claude Code) as a self-assessment, not an independent human review — each verdict was re-checked against its transcript quote and the reported figures; scored 2026-11-15. Sample built by `make_sheet.py` (filings stratified across tiers; every scored or not-yet-due ledger verdict).

| Metric | Result |
|---|---|
| Filings labelled | 30 |
| "Needs you" precision — act items labelled important | 9/10 (90%) |
| Recall — important filings FINN surfaced (act or know, not hidden) | 10/10 (100%) |
| Category agreement (where a category was labelled) | n/a (nothing labelled) |
| Ledger verdicts labelled | 16 |
| Ledger accuracy — verdicts labelled correct | 14/16 (88%) |

## Disagreements (including important filings FINN put in "worth knowing" rather than "needs you")

- **ETERNAL** (filing): Announcement under Regulation 30 (LODR)-Restructuring — FINN: act; note: [agent self-assessed] internal reorganisation; ~₹150 cr against ₹3.5 lakh cr market cap — immaterial
- **SUNPHARMA** (filing): Clarification Sought From Sun Pharmaceutical Industries Ltd — FINN: know; note: [agent self-assessed] exchange sought clarification on a news report — should be 'act'
- **ASIANPAINT** (guidance): We have committed closer to 700 crores of CapEx this year. — FINN: missed; note: [agent self-assessed] uncertain: standardized capex is all fixed-asset spend; the ₹700 cr commitment may cover only part of it
- **HAL** (guidance): We will be able to sustain our growth what we have shown in the current financial year of 13% on an average. — FINN: missed; note: [agent self-assessed] promise was a multi-year average; scored against a single year
