# Accuracy check

Labelled by the build agent (Claude Code) as a self-assessment, not an independent human review — each verdict was re-checked against its transcript quote and the reported figures; two ledger rows were re-labelled after the fixes that corrected them (noted in labels.csv); scored 2026-11-15. Sample built by `make_sheet.py` (filings stratified across tiers; every scored or not-yet-due ledger verdict).

| Metric | Result |
|---|---|
| Filings labelled | 30 |
| "Needs you" precision — act items labelled important | 10/10 (100%) |
| Recall — important filings FINN surfaced (act or know, not hidden) | 10/10 (100%) |
| Category agreement (where a category was labelled) | n/a (nothing labelled) |
| Ledger verdicts labelled | 16 |
| Ledger accuracy — verdicts labelled correct | 16/16 (100%) |

## Read this before quoting the numbers

This sample is the one the fixes were found on. The first scoring (same labels) gave 90% precision,
100% recall and 14/16 ledger verdicts; the four disagreements it surfaced were then fixed in code:

| Disagreement | Fix | Test |
|---|---|---|
| SUNPHARMA "Clarification Sought From …" in *worth knowing* | Exchange queries are a rule of their own, go to *needs you*, cannot be demoted, and are paired with the company's reply | `test_exchange_clarification_request_needs_attention`, `test_exchange_query_and_company_reply_become_one_event`, `test_model_cannot_demote_an_exchange_query` |
| HAL "13% on an average" scored against one year | Average/multi-year growth promises are scored as a CAGR over completed years, or left unscored with the reason | `test_average_growth_promise_is_scored_as_cagr_never_one_year`, `test_onwards_starts_the_cagr_at_the_named_year`, `test_single_year_promise_with_a_multi_year_aside_in_the_quote_stays_single_year` |
| ASIANPAINT "700 crores of CapEx" compared with all fixed-asset spend | Capex is scored only when the promise is clearly company-wide; otherwise "not comparable like for like" with both numbers | `test_partial_capex_promise_is_not_compared_with_total_spend` |
| ETERNAL ~₹150 cr restructuring in *needs you* | Deals, fund raises and capex whose stated amounts are all under 0.5% of market value go to *worth knowing*, with the arithmetic shown | `test_size_rule_relative_to_market_value` |

So 100% here means "every known error in this sample is fixed", not "FINN is always right". A fresh,
independently labelled sample is the honest next measurement.

