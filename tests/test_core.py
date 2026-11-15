"""Offline tests for the deterministic core: no network, no model."""
from __future__ import annotations

import pytest

from finn import periods
from finn.pull import ledger
from finn.pull.grounding import allowed_numbers, locate_quote, ungrounded_numbers
from finn.push import results
from finn.push.classify import blend, classify
from finn.sources.screener import parse_number


# --- periods --------------------------------------------------------------------------

@pytest.mark.parametrize("call,expected", [((2026, 11), (2026, 9)), ((2026, 10), (2026, 9)), ((2026, 1), (2025, 12)),
                                           ((2026, 4), (2026, 3)), ((2026, 9), (2026, 6)), ((2026, 12), (2026, 9))])
def test_quarter_discussed(call, expected):
    assert periods.quarter_discussed(call) == expected


@pytest.mark.parametrize("period,label", [((2026, 9), "Q2 FY27"), ((2026, 6), "Q1 FY27"), ((2026, 12), "Q3 FY27"), ((2027, 3), "Q4 FY27")])
def test_fiscal_label(period, label):
    assert periods.fiscal_label(period) == label


def test_parse_number():
    assert parse_number("1,23,456") == 123456.0
    assert parse_number("+12%") == 12.0
    assert parse_number("-2,066") == -2066.0
    assert parse_number("") is None and parse_number("--") is None


# --- triage ---------------------------------------------------------------------------

@pytest.mark.parametrize("title,summary,category,tier", [
    ("Financial Results For The Second Quarter And Half Year Ended 30 September 2026", "", "results", "act"),
    ("Announcement under Regulation 30 (LODR)-Allotment of ESOP / ESPS", "", "unclassified", "buried"),
    ("Announcement under Regulation 30 (LODR)-Newspaper Publication", "results ad", "unclassified", "buried"),
    ("Announcement under Regulation 30 (LODR)-Analyst / Investor Meet - Intimation", "Morgan Stanley conference", "investor_meetings", "buried"),
    ("Announcement under Regulation 30 (LODR)-Credit Rating", "S&P reaffirmed A- stable", "credit_rating", "buried"),
    ("Announcement under Regulation 30 (LODR)-Credit Rating", "ICRA downgraded the long-term rating to AA-", "credit_rating", "act"),
    ("Announcement under Regulation 30 (LODR)-Meeting Updates", "Bajaj Finance opens QIP; floor price ₹992.25", "fund_raise", "act"),
    ("Announcement under Regulation 30 (LODR)-Allotment", "allotted 12,000 commercial papers worth INR 6 billion", "fund_raise", "buried"),
    ("Announcement under Regulation 30 (LODR)-Change in Management", "Mr X resigned as Chief Financial Officer", "management_change", "act"),
    ("Disclosure Under Regulation 30", "DoT levied penalties of Rs.1.60 lakh", "litigation", "buried"),
    ("Shareholder Meeting / Postal Ballot-Outcome of Postal_Ballot", "approved Anup Bagchi as MD & CEO", "management_change", "act"),
])
def test_classify(title, summary, category, tier):
    verdict = classify(title, summary)
    assert (verdict.category, verdict.tier) == (category, tier)


def test_reappointment_is_not_change():
    assert classify("Change in Directorate", "reappointed V. Rangan as Executive Director").tier == "know"


def test_blend_guard_rails():
    results_filing = classify("Unaudited Financial Results for the quarter ended 30 September 2026")
    assert blend(results_filing, 0)[1] >= 88  # a results filing never drops out of "act"
    esop = classify("Allotment of ESOP")
    assert blend(esop, 3)[1] == esop.score  # the model cannot promote paperwork
    unknown = classify("Something entirely new")
    assert blend(unknown, 3)[1] >= 70 and blend(unknown, 0)[1] < 40


# --- results card ---------------------------------------------------------------------

KAYNES_RAW = {  # transcribed from the real Q2 FY27 filing, Rs million
    "basis": "consolidated", "unit": "million", "period_end": "2026-09", "is_lender": False, "page": 8,
    "current": {"revenue": 9525.15, "other_income": 186.15, "total_expenses": 9003.97, "finance_costs": 387.26, "depreciation": 347.33,
                "pbt_before_exceptional": 707.33, "exceptional_items": 0, "pbt": 647.31, "tax": 249.31, "pat": 398.00},
    "previous_quarter": {"revenue": 9462.12, "other_income": 165.21, "total_expenses": 8810.74, "finance_costs": 371.36, "depreciation": 402.53,
                         "pbt_before_exceptional": 816.59, "pbt": 772.21, "tax": 318.08, "pat": 454.13},
    "year_ago_quarter": {"revenue": 9121.25, "other_income": 340.84, "total_expenses": 7954.72, "finance_costs": 132.63, "depreciation": 198.98,
                         "pbt_before_exceptional": 1507.37, "pbt": 1481.19, "tax": 324.08, "pat": 1157.11},
    "flags": ["Prior periods restated"],
}


def test_results_card_math():
    card = results.build_card(KAYNES_RAW)
    m = {x["key"]: x for x in card["metrics"]}
    assert m["revenue"]["value"] == pytest.approx(952.52, abs=0.01)  # million -> crore
    # EBITDA = revenue - (expenses - finance - depreciation)
    assert m["ebitda"]["value"] == pytest.approx((9525.15 - (9003.97 - 387.26 - 347.33)) / 10, abs=0.01)
    assert m["pat"]["yoy_pct"] == pytest.approx(-65.6, abs=0.1)
    assert m["revenue"]["qoq_pct"] == pytest.approx(0.7, abs=0.1)
    assert card["quarter"] == "Q2 FY27" and card["checks"] == []


def test_results_card_catches_misread_column():
    bad = {**KAYNES_RAW, "current": {**KAYNES_RAW["current"], "pat": 900.0}}
    assert any("PBT - tax" in c for c in results.build_card(bad)["checks"])


def test_growth_off_a_loss_is_not_a_percentage():
    assert results.pct_change(50, -20) is None and results.pct_change(50, 0) is None


def test_fmt_cr_indian_grouping():
    assert results.fmt_cr(1234567) == "12,34,567" and results.fmt_cr(952.5) == "953" and results.fmt_cr(39.8) == "39.8"


# --- grounding ------------------------------------------------------------------------

PAGES = ["Welcome to the call.", "We expect revenue growth of twelve to fourteen percent in FY27 driven by strong demand.", "Thank you."]


def test_quote_found_on_right_page():
    verified, page, _ = locate_quote("We expect revenue growth of twelve to fourteen percent in FY27", PAGES, 1)
    assert verified and page == 2


def test_invented_quote_rejected():
    verified, _, score = locate_quote("Margins will double next year because of pricing power everywhere", PAGES, 2)
    assert not verified and score < 0.4


def test_ungrounded_numbers():
    allowed = allowed_numbers("Revenue +20%; PAT ₹95,625 cr; margin 16.2% in FY27")
    assert ungrounded_numbers("PAT ₹95,600 cr, margin 16%, revenue up 20% in FY27", allowed) == []
    assert ungrounded_numbers("revenue grew 37%", allowed) == [37.0]


# --- ledger ---------------------------------------------------------------------------

def _fund(rev, op, pat, labels):
    return {"fy_end_month": 3, "is_lender": False, "series": {
        "quarterly": {"labels": labels, "revenue": rev, "op_profit": op, "pat": pat},
        "annual": {"labels": [], "revenue": [], "op_profit": [], "pat": [], "capex": []}}}


QL = ["Sep 2025", "Dec 2025", "Mar 2026", "Jun 2026", "Sep 2026"]



def _g(**kw):
    base = dict(metric="revenue_growth", target_fy=2027, target_quarter=None, low=None, high=None, unit="pct", direction=None)
    base.update(kw)
    return base


def test_ledger_needs_year_ago_quarters():
    fin = ledger.Financials(_fund([100, 100, 100, 100, 110, 112], [15] * 6, [10] * 6, ["Jun 2025"] + QL))
    verdict = ledger.score(_g(low=8, high=10), fin, (2026, 3))
    assert verdict["status"] == "on_track"  # H1 FY27 revenue +11% vs the same quarters a year earlier


def test_ledger_behind_reports_required_run_rate():
    fin = ledger.Financials(_fund([100, 100, 100, 100, 110, 112], [15] * 6, [10] * 6, ["Jun 2025"] + QL))
    verdict = ledger.score(_g(low=20, high=22), fin, (2026, 3))
    assert verdict["status"] == "behind" and "must grow" in verdict["detail"]


def test_ledger_ignores_targets_already_reported():
    fin = ledger.Financials(_fund([100] * 5, [15] * 5, [10] * 5, QL))
    assert ledger.score(_g(target_fy=2026, low=10), fin, (2026, 3))["status"] == "unmeasured"


def test_ledger_leaves_non_financial_metrics_unscored():
    fin = ledger.Financials(_fund([100] * 5, [15] * 5, [10] * 5, QL))
    assert ledger.score(_g(metric="order_inflow", low=10), fin, (2026, 3))["status"] == "unmeasured"


def test_walks_detect_raised_guidance():
    items = [{"id": "g1", "metric": "revenue_growth", "target_fy": 2027, "target_quarter": None, "unit": "pct", "low": 12, "high": 14, "call": "2026-04", "quarter": "Q4 FY26"},
             {"id": "g2", "metric": "revenue_growth", "target_fy": 2027, "target_quarter": None, "unit": "pct", "low": 15, "high": 17, "call": "2026-07", "quarter": "Q1 FY27"}]
    assert ledger.walks(items)[0]["drift"] == "raised"


def test_rupee_revenue_target_compares_levels_not_growth():
    fund = _fund([900, 950, 1000, 1150, 1100, 1120], [15] * 6, [10] * 6, ["Jun 2025"] + QL)
    fund["series"]["annual"] = {"labels": ["Mar 2026"], "revenue": [4000], "op_profit": [600], "pat": [400], "capex": [100]}
    fin = ledger.Financials(fund)
    verdict = ledger.score(_g(target_fy=2026, low=4500, high=4500, unit="inr_cr"), fin, (2025, 6))
    assert verdict["status"] == "missed" and "₹4,000 cr" in verdict["detail"]


def test_directional_growth_is_not_scored():
    fin = ledger.Financials(_fund([100] * 6, [15] * 6, [10] * 6, ["Jun 2025"] + QL))
    assert ledger.score(_g(direction="up"), fin, (2026, 3))["status"] == "unmeasured"


def test_segment_guidance_is_not_scored_against_company():
    fin = ledger.Financials(_fund([100] * 6, [15] * 6, [10] * 6, ["Jun 2025"] + QL))
    verdict = ledger.score(_g(low=13, high=14, scope="segment", segment="Eyewear"), fin, (2026, 3))
    assert verdict["status"] == "unmeasured" and "Eyewear" in verdict["detail"]


def test_ebit_margin_not_compared_with_ebitda():
    fin = ledger.Financials(_fund([100] * 6, [15] * 6, [10] * 6, ["Jun 2025"] + QL))
    assert ledger.score(_g(metric="operating_margin", low=11, high=11.5, margin_basis="ebit"), fin, (2026, 3))["status"] == "unmeasured"


def test_model_output_wrapped_in_list_is_unwrapped():
    from finn.llm import _unwrap, extract_json
    assert _unwrap(extract_json('[{"guidance": []}]')) == {"guidance": []}
    assert _unwrap([{"a": 1}, {"b": 2}]) == [{"a": 1}, {"b": 2}]
