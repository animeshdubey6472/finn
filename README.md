# FINN — a portfolio agent that filters, not aggregates

FINN is a mobile-first investment agent for Indian equities, built for the ENAM AI Engineer
build challenge. It does two jobs:

- **Push — "what changed, and should I care?"** Every exchange filing across an 18-stock
  portfolio is triaged into *needs you / worth knowing / hidden*. Results filings become a
  verified Revenue · EBITDA · PBT · PAT card (QoQ and YoY); management changes come with the
  appointee's background; unusual price or volume moves fire at thresholds you set.
- **Pull — "is management doing what it said?"** For any NSE company, FINN reads the last six
  earnings-call transcripts and the latest annual report, extracts every concrete commitment
  with a verbatim quote and page number, and **scores each promise against the numbers the
  company later reported**. Then it sets management's narrative against the numbers.

> The point of view: an investor doesn't need more information, they need *less, better
> sorted, and checkable*. So FINN is an inbox you can clear, not a feed you scroll — and every
> claim it makes links back to the page it came from.

---

## Try it

| | |
|---|---|
| **App** | **https://animeshdubey6472.github.io/finn/** — open on a phone |
| **Run locally** | `python -m venv .venv && .venv/bin/pip install -r requirements.txt` → `cp .env.example .env` and add a free Gemini key → `.venv/bin/python -m finn serve` → http://127.0.0.1:8000 |
| **Research any ticker** | `python -m finn research TATAMOTORS` (or type it into the app's search when the local server runs) |
| **Refresh everything** | `python -m finn refresh` |
| **Alerts in the terminal** | `python -m finn alerts --move 3 --volume 2.5` |

Without an API key everything deterministic still works: prices, alerts, triage by rules,
fundamentals, and results cards from standardized data.

---

## What's on screen

**Today** — a one-line briefing ("3 things need you across 2 companies"), then:
- *Needs you*: material filings, each read and summarised. Results show the four headline
  numbers with a reconciliation badge; management changes show who the person is.
- *Unusual moves*: price/volume breaches at **your** thresholds, with the Nifty's move that day
  (is it the stock or the market?) and any filing made around the same time.
- *Worth knowing*: a compact list, one line each, tap to expand.
- *Hidden*: routine paperwork (ESOP allotments, newspaper ads, investor-meet intimations)
  collapsed into a count. It is still one tap away — hidden, not deleted.
- Window chips (24h default, 3/7/30 days). Seen items dim; the goal state is *caught up*.

**Company** — research ordered by what an analyst would want first:
1. **Promise ledger** — delivered / missed / behind / not yet due, each with the quote and a
   link to the transcript page. Verdicts are computed in code from reported financials.
2. **Narrative vs numbers** — "management says" beside "numbers show", tagged aligned /
   stretched / diverging.
3. **Bull vs bear**, three points each, every figure tied to a computed fact.
4. **Fundamentals** — snapshot, trend, balance sheet, cash quality (lenders get bank-appropriate
   measures instead of operating margin and cash conversion).
5. **From the annual report** — auditor's opinion, contingent liabilities, related parties.
6. **Sources & checks** — how many quotes were found verbatim, how many synthesized statements
   passed the figure check, which documents were read.

**Alerts** — threshold sliders with a live preview ("at these settings: 6 alerts in the last
month"), the filing types allowed to interrupt you, and what the run cost.

---

## Architecture

```
            ┌────────────── sources ──────────────┐
            │ screener.in   NSE RSS   Yahoo EOD    │   polite fetcher: honest UA, per-host pacing,
            │ (financials,  (live     (price,      │   disk cache (filings cached forever)
            │  filing links) filings)  volume)     │
            └──────┬──────────────┬────────┬───────┘
                   │              │        │
     PUSH          ▼              ▼        ▼                    PULL (any NSE symbol)
  ┌───────────────────────────┐  ┌─────────────┐      ┌──────────────────────────────────┐
  │ triage by rules (free)    │  │ signals     │      │ fundamentals → facts f1..fn (code)│
  │  └ borderline → read PDF  │  │ move%, vol× │      │ transcripts → guidance, claims    │
  │     results → numbers     │  │ vs Nifty    │      │   (cheap model, quoted + paged)   │
  │     people  → profile     │  └─────────────┘      │ quote check (code)                │
  │     other   → materiality │                       │ promise ledger (code)             │
  │ cluster duplicates        │                       │ annual report → notes (cheap)     │
  │ feed = memory across runs │                       │ synthesis (stronger model, 1 call)│
  └────────────┬──────────────┘                       │ figure check (code)               │
               │                                      └────────────────┬─────────────────┘
               └──────────────► web/data/*.json ◄──────────────────────┘
                                      │
                         static mobile web app (no build step)
                         + optional local server for live research
```

**The rule throughout: models read, code decides.** A model transcribes numbers from a
results filing; Python computes EBITDA, margins, QoQ and YoY, checks that PBT − tax = PAT, and
reconciles against standardized data. A model extracts guidance; Python decides whether it was
delivered. A model writes the synthesis; Python deletes any sentence containing a figure that
isn't in the fact pack it was given.

### Model routing and cost

| Tier | Default model | Used for | Calls per company |
|---|---|---|---|
| — | none (rules) | triaging filings — most of the feed | 0 |
| fast | `gemini-3.1-flash-lite` | reading a filing, a transcript, an annual report | ~1 per document |
| smart | `gemini-3.6-flash` | the one synthesis per company | 1 |
| search | off by default | appointee background from the web, only if the filing has none — opt-in, because Google search grounding is not in the free tier | 0 |

Each tier is an ordered fallback list; a free-tier cap or a hung model falls through to the
next. Model calls are cached by content, so a refresh only pays for what is new. Every call is
logged with tokens and list-price cost (`web/data/meta.json`, and the Alerts tab).

---

## Key decisions and trade-offs

- **Rules before models for triage.** Exchange subject lines are formulaic; ~50 ordered rules
  sort most filings for free and flag only the ambiguous middle for a model. Cost: rules need
  maintenance and will mis-sort novel phrasing — mitigated by a "no rule matched → read it"
  default and by letting the model move items across tiers (with guard rails: a results filing
  can't be demoted, paperwork can't be promoted).
- **Score only what is checkable.** The ledger scores revenue growth, operating margin, PAT
  growth/margin, rupee revenue targets and annual capex. Order books, store counts, segment
  targets and "we'll outgrow the market" stay as *watch items*. A wrong verdict would be worse
  than none.
- **Fundamentals from standardized data, narrative from primary documents.** Multi-year
  financials come from screener's standardized tables (consistent across companies, cheap);
  guidance and claims come only from transcripts and annual reports. No news, broker notes or
  model knowledge is used on the pull side.
- **Static app + JSON snapshot.** The app is three files with no build step, deployable
  anywhere and fast on a phone. Live research for an arbitrary ticker needs the small server.
- **Feed as memory.** Sources only list recent filings; the published feed is re-loaded each
  run, so history accumulates and nothing is read twice.

## Deliberately left out

- Intraday data and real-time push notifications (the brief is end-of-day; in-app alerts suffice).
- Chat as the main interface. Q&A exists in the engine but the product bet is that a ranked,
  clearable inbox plus a promise ledger beats a blank chat box for a daily workflow.
- Buy/sell calls, price targets, valuation models.
- LinkedIn scraping. SEBI requires every disclosure of a change in directors, KMP or senior
  management to include a "brief profile (in case of appointment)" (item 7.3 of
  [SEBI circular SEBI/HO/CFD/CFD-PoD-1/P/CIR/2023/123, 13 July 2023](https://www.sebi.gov.in/legal/circulars/jul-2023/disclosure-of-material-events-information-by-listed-entities-under-regulations-30-and-30a-of-securities-and-exchange-board-of-india-listing-obligations-and-disclosure-requirements-regulations-201-_73910.html)),
  so FINN reads the profile from the filing. A web lookup exists for filings without one, but is off by default: search grounding needs a paid Gemini key (set `FINN_MODELS_SEARCH`).

## Where I'd take it next

1. Scheduled refresh (GitHub Actions cron) and web-push for "needs you" items.
2. Segment-level ledger: map segment guidance to segment results from investor decks.
3. An evaluation set: 50 hand-labelled filings and 20 hand-scored guidance items to measure
   triage precision and ledger accuracy, run on every prompt change.
4. Peer view: the same promise ledger across a sector ("who guides conservatively?").

---

## Repository

```
finn/
  sources/   screener.py (financials + document links), nse_feed.py, prices.py, pdf.py
  push/      classify.py (rules), enrich.py (read filings), results.py (card maths),
             signals.py (price/volume), digest.py (feed build)
  pull/      fundamentals.py (facts), transcripts.py, ledger.py, annual_report.py,
             grounding.py (quote + figure checks), research.py (orchestration)
  llm.py     tiered routing, fallback, cache, cost ledger
  prompts.py every prompt, versioned (the version is part of the cache key)
  pipeline.py, server.py, __main__.py
web/         index.html, app.css, app.js, data/
tests/       offline tests for the deterministic core
```

## Research and references

What I looked at before building, and what I took from each:

| Project | What it does | Taken / avoided |
|---|---|---|
| [virattt/dexter](https://github.com/virattt/dexter) | Autonomous US financial-research agent (plan → act → validate) | Self-validation idea; avoided open-ended agent loops — fixed pipelines are cheaper and auditable |
| [AI4Finance FinRobot](https://github.com/AI4Finance-Foundation/FinRobot) | Multi-agent equity research; numbers computed by code, not the LLM | Adopted "model reasons, code calculates" |
| [StrataLens / finance-agent](https://github.com/kamathhrishi/finance-agent) | Q&A over SEC filings with line-level citations | Citation-to-source discipline |
| [Arthneeti methodology](https://arthneeti.com/methodology) | Indian concall scorecard: guidance vs next quarter | Closest prior art I found; FINN scores against *reported financials* with explicit tolerances and shows its arithmetic |
| [BseIndiaApi](https://github.com/BennyThadikaran/BseIndiaApi) | Unofficial BSE wrapper | Confirmed BSE throttling behaviour; used official NSE RSS instead |
| [Lightyear AI updates](https://lightyear.com/en-gb/blog/ai-powered-market-intelligence-for-every-investor) | Retail app: summaries when a holding "moves significantly" | FINN makes the threshold the user's and shows market context |
| [FinanceBench (arXiv:2311.11944)](https://arxiv.org/abs/2311.11944) | LLM+retrieval answered 81% of financial questions wrongly or refused | Why every figure is computed or checked in code |
| [ECTSum (arXiv:2210.12467)](https://arxiv.org/abs/2210.12467) | Earnings-call summarisation benchmark | Calls are long and unstructured → extract structured items, not summaries |
| [Google SRE book — alerting](https://sre.google/sre-book/monitoring-distributed-systems/) | "Every page should be actionable"; signal over noise | The design rule for "needs you" vs "worth knowing" |

## Not investment advice

FINN is a prototype for a hiring exercise. It may be wrong; every figure links to its source so
you can check.
