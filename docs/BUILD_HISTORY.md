# How FINN was built — AI build history

FINN was built in one working session (15 Nov 2026) with **Claude Code** as the coding agent,
working in the terminal on this repository. The product itself uses **Google Gemini** models at
runtime (see the README's model-routing table). The full session transcript is exported
alongside this file: [`claude-code-session.txt`](claude-code-session.txt).

The session ran on two Claude accounts: when the first reached its usage limit, the agent wrote a
handover file and the same Claude Code session continued after logging in to a second account. The
transcript is one continuous export covering both parts, including my original instructions.

This page is the readable summary: how the work was split between me and the agent, the
decisions I made, and the mistakes that review caught.

## How I worked with the agent

- **I set direction and made the calls; the agent researched, built and tested.** I gave it the
  brief and the glossary, then steered by asking for status, effort estimates and critical
  reviews at each stage rather than accepting the first version.
- **Explicit checkpoints.** I asked the agent to stop and report before outward-facing steps
  (creating the public repo, deploying, rotating the API key) and to wait for my approval.
- **Scope discipline.** When further research started to look like over-investment, I limited it
  to the single item that was actually necessary (confirming the SEBI disclosure rule the design
  relies on), and deferred the accuracy check to the end.
- **Independent review before shipping.** I asked for a final review written as a sceptical
  engineering lead and recruiter — verify every claim against the code and the deployed site,
  separate critical fixes from nice-to-haves, and reject impressive-sounding features that add
  risk. Several of the fixes below came from that review.
- **Parallel work.** A helper agent did the SEBI check, the README citation and the accuracy
  sheet while the main thread fixed the pipeline; long research runs ran in the background.

## Timeline

| Phase | What happened |
|---|---|
| Understand | Read the brief and glossary; extracted mandatory requirements and grading signals ("thin but working beats broad but broken", efficiency bonus, primary sources only). |
| Research | Reviewed Dexter, FinRobot, StrataLens, Arthneeti, BseIndiaApi, Lightyear, FinanceBench, ECTSum and Google's SRE alerting chapter. Settled on *models read, code decides* and on scoring guidance against reported numbers. |
| Probe sources | Tested every data source before coding: yfinance works, NSE's site blocks scripts but its RSS and symbol list don't, screener.in carries financials and document links, BSE serves PDFs but throttles bursts. |
| Build backend | Polite fetcher with caching; tiered model client with fallback and a cost ledger; rules-first triage; filing reader; results maths; signals; deterministic fundamentals; transcript extraction with quote verification; promise ledger; annual-report reader; synthesis with a figure check. |
| Validate with a real model | Ran Kaynes end to end and read every output by hand; reviewed every push card. |
| Build app | Static, mobile-first app: Today (inbox), Portfolio, Research, Company, Alerts. Checked at phone width, light and dark. |
| Ship | Public repo, CI tests, GitHub Pages deploy. |
| Final review | Verified the deployed site, measured ledger coverage, probed generalisation on nine extra symbols, fixed what was critical. |

## Mistakes caught by review (and fixed)

Every one of these was found by reading real output, not by tests passing.

| Found | Problem | Fix |
|---|---|---|
| Kaynes ledger | "₹4,500 cr revenue" was scored as a *growth rate* | Rupee targets are compared with reported revenue levels |
| Kaynes ledger | "We'll double the market's growth" marked *delivered* because growth accelerated | Directional growth claims are watch items, not verdicts |
| Titan ledger | Eyewear-division and jewellery-EBIT guidance scored against whole-company numbers | Extraction records scope (company/segment) and margin basis; only like-for-like promises are scored |
| Kaynes calls | An extra December call mapped to the same quarter as the results call | One call per quarter, preferring the results call |
| First synthesis | The strongest model hung on the free tier, stalling the run for minutes | Timeouts fail over to the next model immediately; routing reordered |
| Push run | 5 filings failed to download (BSE throttling) | Fetch the archived PDF directly (half the requests) and back off on 406 |
| Push run | NSE reset every connection — the User-Agent contained a URL | Plain, still-honest User-Agent; symbol list and live feed now load |
| Push run | The model relabelled monthly sales data as "results" | Only rules may assign the results category |
| Alerts | A −0.5% day with the Nifty −0.6% was labelled "stock-specific" | Compares the stock's move with the market's before saying so |
| Results card | Indian number formatting rounded 952.5 down to 952 | Half-up rounding (caught by a unit test) |
| Bio lookup | Web-grounded background search silently returned nothing | Grounding is not in the Gemini free tier for current models — made it opt-in and said so in the README |
| IndiGo ledger | "Mid-teens improvement in unit revenue" scored as company revenue growth | Extraction rules: per-unit measures are never company revenue growth |
| HAL ledger | EBITDA margin "behind" (24.9% vs 30–31%) only because other income was excluded | When a margin verdict depends on the EBITDA definition, both numbers are shown instead |
| ITC results | Revenue included excise duty (₹30,578 cr vs ₹20,269 cr net) — caught by the reconciliation badge | Excise is netted from revenue; card now matches standardized data |
| All companies | 8 of 42 transcripts silently dropped — the model wrapped its answer in a one-item list | Unwrapped at the client; recovered from cache at no extra cost |
| Accuracy labelling | HAL promises about FY23 were shown as "not yet due" — FY23 is older than the 13-quarter window | Complete years fall back to the annual table; FY23 promises now scored |
| Second audit | A company that failed to load during a refresh lost all its feed history; a total outage would still re-stamp the feed "updated just now"; a run without model access would overwrite complete research with a numbers-only version | Failed refreshes never replace good data: history is kept per portfolio company, an outage leaves the published feed untouched, degraded research never replaces fuller research. The feed records per-source status; the app shows last-updated times, ⚠ warnings and stale rows. Six failure-scenario tests |
| Second audit | The brief's flagship case — HDFC Bank's new CEO — showed "No profile" | Searched every primary source FINN holds (the filing, six call transcripts, a 678-page annual report): none carry the profile. Added a model-knowledge fallback that must answer "found" at high confidence with a named prior role, labelled *AI-generated · unverified* with a verify link. The model declined for this person, so the card explains why there is no profile and links a search instead of guessing |
| Second audit | An exchange's request for clarification sat in "worth knowing", and the company's reply was a separate, buried item | Exchange queries go to "needs you" and cannot be demoted by the model; the query and the company's reply are paired into one event, with the reply as its summary |
| Regression run | The app's default alert-type filter (which excludes press releases) silently demoted the exchange query again | Exchange queries are exempt from the type filter |
| Final review | The deployed app measured "last 24h" from today's date, so it would look empty a day after publishing | Windows are measured from the snapshot time, shown in the header |
| Final review | README overstated traceability and used a symbol (TATAMOTORS) that no longer exists | Claims corrected; example replaced; limitations section added |

## Who decided what

- **Agent proposed:** the portfolio mix, the architecture, the interaction model (an inbox you
  clear, with the promise ledger as the centrepiece) and the build plan.
- **I decided:** when to stop researching and what was strictly necessary, the order of work and
  what to defer, which review findings to act on, and every outward-facing step — making the
  repository public, how commits are attributed, GitHub Pages hosting, and rotating the API key
  before submission.
- **Neither model decides numbers:** figures shown in the app are computed by code from source
  documents; the models only extract and phrase.
