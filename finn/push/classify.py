"""Triage for exchange filings: category, score, tier — decided by rules, for free.

Exchange subject lines are formulaic ("Announcement under Regulation 30 (LODR)-Credit Rating"),
so an ordered list of patterns sorts most of the feed without a model. The rules settle the
obvious cases at both ends — results are always important, a lost share certificate never is —
and mark the middle as `needs_read`, which is the only part that reaches an LLM.

Categories are the seventeen from the brief's reference document. `tag` is our finer grain
inside a category (a results *filing* vs. the newspaper ad about it) and is what the score
actually keys on.

    score >= 70  -> "act"     shown first, eligible to alert
    40..69       -> "know"    shown, compact
    < 40         -> "buried"  collapsed into a count
"""
from __future__ import annotations

import re
from dataclasses import dataclass

CATEGORIES: dict[str, str] = {
    "results": "Results",
    "m_and_a": "M&A",
    "fund_raise": "Fund raise",
    "management_change": "Management",
    "litigation": "Legal & regulatory",
    "insider_trading": "Insider trades",
    "earnings_call": "Earnings call",
    "investor_meetings": "Investor meetings",
    "credit_rating": "Credit rating",
    "dividend": "Dividend",
    "bonus_split": "Bonus / split",
    "capex": "Capex",
    "press_release": "Press release",
    "postal_ballot": "Postal ballot",
    "agm_egm": "AGM / EGM",
    "pledging": "Pledging",
    "unclassified": "Routine",
}

ACT, KNOW = 70, 40


def tier_for(score: float) -> str:
    return "act" if score >= ACT else "know" if score >= KNOW else "buried"


@dataclass(frozen=True)
class Verdict:
    category: str
    tag: str
    score: int
    needs_read: bool  # worth an LLM reading the filing itself
    rule: str  # which rule fired, for the "why is this here" trail

    @property
    def tier(self) -> str:
        return tier_for(self.score)


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


# (name, pattern on title+summary, category, tag, score, needs_read)
# Order matters: first match wins, so specific noise comes before broad signal.
_RULES: list[tuple[str, re.Pattern, str, str, int, bool]] = [
    # --- noise that looks like signal --------------------------------------------
    ("lost_certificate", _rx(r"loss of (share )?certificate|duplicate (share )?certificate|reg(ulation|\.)?\s*39"), "unclassified", "share_certificates", 2, False),
    ("compliance_cert", _rx(r"reg(ulation|\.)?\s*74|compliance certificate|compliances?\s*-|certificate under|reconciliation of share capital|corporate governance report|investor (grievance|complaint)|shareholding pattern"), "unclassified", "compliance", 3, False),
    ("trading_window", _rx(r"trading window"), "unclassified", "trading_window", 4, False),
    ("demat_reminder", _rx(r"reminder letter|dematerialis|iepf|unclaimed (dividend|shares)|fractional entitlement"), "unclassified", "shareholder_admin", 3, False),
    ("registrar", _rx(r"change in corporate office|registrar (and|&) (share )?transfer|\brta\b|change of address"), "unclassified", "registrar", 4, False),
    ("esop", _rx(r"\besop\b|\besps\b|employee(s'?)? stock option|stock option (scheme|plan|trust)|grant of options"), "unclassified", "esop", 5, False),
    ("esg", _rx(r"business responsibility|\bbrsr\b|\besg\b|sustainability report"), "unclassified", "esg", 8, False),
    ("monitoring_agency", _rx(r"monitoring agency"), "unclassified", "monitoring_agency", 14, False),
    ("results_ad", _rx(r"newspaper (publication|advertisement).*result|result.*newspaper"), "results", "newspaper_ad", 5, False),
    ("newspaper", _rx(r"newspaper (publication|advertisement)"), "unclassified", "newspaper_ad", 4, False),
    # --- earnings call -----------------------------------------------------------
    ("call_transcript", _rx(r"transcript"), "earnings_call", "transcript", 44, False),
    ("call_recording", _rx(r"(audio|video)(/video)? recording|recording of (the )?(earnings|conference|analyst)"), "earnings_call", "recording", 14, False),
    ("call_notice", _rx(r"(earnings|results?|quarterly) (conference )?call|\bconcall\b|q[1-4]\s*fy\s*'?\d{2,4}\s*(earnings |conference |results )?call"), "earnings_call", "notice", 16, False),
    # --- pledging / insider (before generic 'acquisition') -------------------------
    ("pledge", _rx(r"\bpledg|encumbr|invocation of"), "pledging", "pledge", 76, True),
    ("insider", _rx(r"insider trading|reg(ulation|\.)?\s*7\s*\(2\)|\bsast\b|reg(ulation|\.)?\s*29|substantial acquisition of shares|promoter.{0,40}(acquir|sold|sale|purchas)|inter-?se transfer"), "insider_trading", "disclosure", 50, True),
    # --- results -----------------------------------------------------------------
    ("results_deck", _rx(r"investor'?s?'? presentation|results? presentation|earnings presentation|media statement|shareholder'?s? letter|half yearly report"), "results", "deck", 46, False),
    ("results_upcoming", _rx(r"(board meeting intimation|intimation of (the )?board meeting|notice of (the )?board meeting).{0,200}result|result.{0,200}board meeting intimation"), "results", "upcoming", 34, False),
    ("results_filing", _rx(r"financial results?|(un)?audited.{0,40}results?|results? (for|of) the (quarter|year|half|second|first|third|fourth|period)|integrated filing.{0,20}financ|outcome of (the )?board meeting.{0,120}(result|quarter)|board meeting outcome.{0,160}(result|quarter)"), "results", "filing", 92, True),
    ("board_notice", _rx(r"board meeting intimation|intimation of (the )?board meeting|notice of (the )?board meeting"), "unclassified", "board_notice", 26, False),
    # --- investor meetings (after call rules: 'Analyst / Investor Meet - Outcome' is also used for transcripts)
    ("investor_meet", _rx(r"analysts?\s*(\(s\))?\s*/\s*(institutional )?investors?|investors? meet|institutional investor|non-?deal roadshow|investor conference"), "investor_meetings", "meeting", 8, False),
    # --- corporate actions ---------------------------------------------------------
    ("bonus_split", _rx(r"\bbonus (issue|shares?)\b|stock split|sub-?division of (equity )?shares|split of (equity )?shares"), "bonus_split", "bonus_split", 68, True),
    ("dividend", _rx(r"\bdividend"), "dividend", "declared", 60, True),
    ("record_date", _rx(r"record date|book closure"), "dividend", "record_date", 42, False),
    # --- credit rating -------------------------------------------------------------
    ("rating_down", _rx(r"(credit )?rating.{0,160}downgrad|downgrad.{0,160}rating"), "credit_rating", "downgrade", 88, True),
    ("rating_up", _rx(r"(credit )?rating.{0,160}upgrad|upgrad.{0,160}rating"), "credit_rating", "upgrade", 72, True),
    ("rating_same", _rx(r"(credit )?rating.{0,160}(re-?affirm|affirm)|(re-?affirm|affirm).{0,160}rating"), "credit_rating", "reaffirmed", 28, False),
    ("rating", _rx(r"credit rating|\brating\b"), "credit_rating", "action", 50, True),
    # --- legal / regulatory --------------------------------------------------------
    ("insolvency", _rx(r"insolvency|\bcirp\b|\bnclt\b|\bnclat\b|winding up|liquidat"), "litigation", "insolvency", 78, True),
    ("regulatory", _rx(r"penalt|\bfine[sd]?\b|show cause|tax demand|demand (order|notice)|gst (order|demand)|income tax (order|demand)|sebi (order|notice)|warning letter|\busfda\b|form 483|import alert|search and seizure|litigation|arbitra|court order|\blawsuit|regulatory (action|order)|adjudicat"), "litigation", "order", 66, True),
    # --- fund raise ----------------------------------------------------------------
    ("debt_routine", _rx(r"commercial papers?|\bcp\b.{0,30}(redeem|repay|allot)|redemption of (ncd|debenture)|repayment of"), "fund_raise", "debt_routine", 24, False),
    ("fund_raise", _rx(r"\bqip\b|qualified institution|rights issue|preferential (issue|allotment)|\bwarrants?\b|buy-?back|fund rais|raising of funds|issue of (ncd|non-?convertible|debenture|securities|equity)|\bncds?\b|non-?convertible debenture|\bfpo\b|\bipo\b"), "fund_raise", "raise", 74, True),
    # --- M&A -----------------------------------------------------------------------
    ("ma_update", _rx(r"updates? on acquisition|completion of acquisition"), "m_and_a", "update", 60, True),
    ("ma", _rx(r"acquisition|\bacquir|merger|amalgamat|demerger|scheme of arrangement|joint venture|\bjv\b|restructuring|slump sale|divest|disinvest|stake sale|sale of (stake|shares|undertaking|business)|term sheet|share purchase agreement|share swap"), "m_and_a", "deal", 82, True),
    ("subsidiary", _rx(r"incorporation of.{0,40}(subsidiary|company)|wholly owned subsidiary"), "m_and_a", "subsidiary", 46, True),
    # --- people --------------------------------------------------------------------
    ("auditor", _rx(r"auditor"), "unclassified", "auditor", 22, False),
    ("cs_change", _rx(r"company secretary|compliance officer"), "management_change", "company_secretary", 20, False),
    ("top_job", _rx(r"(chief executive|\bceo\b|\bcfo\b|chief financial|managing director|\bmd\b|chairman|chairperson|whole-?time director|executive director).{0,200}(appoint|resign|retire|cessation|superannuat|step|demise|elevat)|(appoint|resign|retire|cessation|superannuat|step|demise|elevat).{0,200}(chief executive|\bceo\b|\bcfo\b|chief financial|managing director|\bmd\b|chairman|chairperson|whole-?time director|executive director)"), "management_change", "top_job", 84, True),
    ("mgmt_change", _rx(r"change in (management|directorate|directors|key managerial|senior management)|\bkmp\b|key managerial|resignation|cessation|superannuat|appointment of|re-?appoint"), "management_change", "change", 58, True),
    # --- capex / operations --------------------------------------------------------
    ("capex", _rx(r"\bcapex\b|capital expenditure|capacity (expansion|addition)|new (plant|facility|unit)|greenfield|brownfield|commenc\w+ of commercial production|commissioning"), "capex", "capex", 68, True),
    ("order_win", _rx(r"award of order|receipt of order|bagging|order (win|inflow|received)|letter of (award|intent|acceptance)|\bloa\b|wins? .{0,40}order|secures? .{0,40}order"), "press_release", "order_win", 58, True),
    ("rumour", _rx(r"rumou?r|clarification (sought|on|regarding)|price movement|spurt in volume"), "press_release", "clarification", 62, True),
    ("op_update", _rx(r"(report|update|sales|production|volumes?) for the month|monthly (business|sales|production|volume)|business update|quarterly update|operational update"), "press_release", "operating_update", 56, True),
    ("press", _rx(r"press release|media release"), "press_release", "press_release", 42, True),
    # --- meetings ------------------------------------------------------------------
    ("postal_ballot", _rx(r"postal ballot"), "postal_ballot", "postal_ballot", 18, False),
    ("agm", _rx(r"\bagm\b|annual general meeting|\begm\b|extra-?ordinary general|voting results?|scrutini[sz]er"), "agm_egm", "meeting", 18, False),
    # --- vague titles that can hide anything ---------------------------------------
    ("allotment", _rx(r"allotment"), "fund_raise", "allotment", 44, True),
    ("board_outcome", _rx(r"board meeting outcome|outcome of (the )?board meeting|meeting updates"), "unclassified", "board_outcome", 52, True),
]

_FALLBACK = Verdict("unclassified", "unknown", 40, True, "no_rule")

_SMALL_MONEY = re.compile(r"(rs\.?|₹|inr)\s*[\d,.]+\s*lakh", re.IGNORECASE)
_BIG_MONEY = re.compile(r"crore|\bcr\b|billion|\bbn\b|million|\bmn\b", re.IGNORECASE)


def classify(title: str, summary: str = "") -> Verdict:
    """Rule-based verdict for one filing.

    The title is matched first because it is the exchange's own subject line. The summary is
    consulted when the title matches nothing or is one of the bland catch-all headings, and
    to settle direction (a rating "action" is a downgrade or a reaffirmation only in the body).
    """
    title, summary = (title or "").strip(), (summary or "").strip()
    verdict = _first_match(title) or _first_match(f"{title} {summary}") or _FALLBACK

    text = f"{title} {summary}"
    # A bland heading can carry anything ("Meeting Updates" for a QIP opening, "Allotment" for
    # ESOPs or for a rights issue), so for those the summary decides, in either direction.
    if verdict.tag in _VAGUE_TAGS and summary:
        deeper = _first_match(summary)
        if deeper and deeper.tag not in _VAGUE_TAGS:
            verdict = deeper
    # Rating actions are filed under one subject line; the direction is in the body.
    if verdict.category == "credit_rating" and verdict.tag == "action":
        for name in ("rating_down", "rating_up", "rating_same"):
            hit = _match_rule(name, text)
            if hit:
                verdict = hit
                break
    # "Change in Management" is one subject line for a CFO exit and a branch head retiring.
    if verdict.category == "management_change" and verdict.tag == "change":
        verdict = _match_rule("top_job", text) or verdict

    score, category, tag, needs_read = verdict.score, verdict.category, verdict.tag, verdict.needs_read
    # A postal ballot or AGM that installs a CEO matters more than the envelope it came in.
    if category in {"postal_ballot", "agm_egm"} and _TOP_JOB.search(text):
        category, tag, score, needs_read = "management_change", "top_job_vote", 72, True
    # Penalties counted in lakhs are noise for any company an investor would hold here.
    if category == "litigation" and _SMALL_MONEY.search(text) and not _BIG_MONEY.search(text):
        score = min(score, 30)
    # Re-appointing an incumbent is continuity, not change.
    if category == "management_change" and re.search(r"re-?appoint", text, re.IGNORECASE):
        score = min(score, 46)
    return Verdict(category, tag, score, needs_read and score >= 30, verdict.rule)


_VAGUE_TAGS = {"board_outcome", "unknown", "allotment", "meeting"}
_TOP_JOB = re.compile(r"chief executive|\bceo\b|\bcfo\b|chief financial|managing director|\bmd\b|chairman|chairperson", re.IGNORECASE)


def _match_rule(rule_name: str, text: str) -> Verdict | None:
    for name, pattern, category, tag, score, needs_read in _RULES:
        if name == rule_name:
            return Verdict(category, tag, score, needs_read, name) if pattern.search(text) else None
    return None


def _first_match(text: str) -> Verdict | None:
    for name, pattern, category, tag, score, needs_read in _RULES:
        if pattern.search(text):
            return Verdict(category, tag, score, needs_read, name)
    return None


def blend(verdict: Verdict, materiality: int | None, category: str | None = None) -> tuple[str, int]:
    """Combine the rule verdict with what a model concluded after reading the filing.

    `materiality` is 0 (noise) .. 3 (could move the stock). The model may move an item across
    tiers, with two guard rails: an actual results filing never drops out of "act", and the
    model cannot promote something the rules consider pure paperwork.
    """
    final_category = category if category in CATEGORIES else verdict.category
    # Only the rules may put something in "results": that tier promises a numbers card.
    if final_category == "results" and verdict.category != "results":
        final_category = verdict.category
    score = verdict.score
    if materiality is None:
        return final_category, score
    if verdict.tag == "filing" and verdict.category == "results":
        return verdict.category, max(score, 88)
    if not verdict.needs_read:
        return verdict.category, score
    if materiality >= 3:
        score = max(score, 86)
    elif materiality == 2:
        score = min(max(score, 56), 78)
    elif materiality == 1:
        score = min(score, 38)
    else:
        score = min(score, 12)
    return final_category, score
