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


def test_revenue_net_of_excise():
    raw = {**KAYNES_RAW, "unit": "crore",
           "current": {**KAYNES_RAW["current"], "revenue": 30578.4, "excise_duty": 10309.4, "total_expenses": 9003.97 + 10309.4}}
    card = results.build_card(raw)
    m = {x["key"]: x for x in card["metrics"]}
    assert m["revenue"]["value"] == pytest.approx(20269.0, abs=0.1)
    assert card["flags"][0].startswith("Revenue shown net of excise")


def _fund_with_bs(**annual):
    fund = _fund([100, 100, 100, 100, 110, 112], [15] * 6, [10] * 6, ["Jun 2025"] + QL)
    base = {"labels": ["Mar 2025", "Mar 2026"], "revenue": [380, 400], "op_profit": [57, 60], "pat": [38, 40], "capex": [10, 12],
            "borrowings": [900, 450], "deposits": [1000, 1150]}
    base.update(annual)
    fund["series"]["annual"] = base
    return fund


def test_debt_ceiling_scored_on_year_end_borrowings():
    fin = ledger.Financials(_fund_with_bs())
    v = ledger.score(_g(metric="debt", target_fy=2026, low=500, high=500, unit="inr_cr", statement="Debt below ₹500 cr by year end"), fin, (2025, 6))
    assert v["status"] == "delivered" and "₹450 cr" in v["detail"]
    net = ledger.score(_g(metric="debt", target_fy=2026, low=0, high=0, unit="inr_cr", statement="We will be net debt free"), fin, (2025, 6))
    assert net["status"] == "unmeasured"


def test_ebitda_rupee_target_and_deposit_growth():
    fin = ledger.Financials(_fund_with_bs())
    assert ledger.score(_g(metric="ebitda", target_fy=2026, low=70, high=70, unit="inr_cr"), fin, (2025, 6))["status"] == "missed"
    lender = _fund_with_bs(); lender["is_lender"] = True
    v = ledger.score(_g(metric="deposit_growth", target_fy=2026, low=14, high=16, unit="pct"), ledger.Financials(lender), (2025, 6))
    assert v["status"] == "delivered" and "+15.0%" in v["detail"]


def test_revenue_metric_in_percent_is_growth():
    fin = ledger.Financials(_fund([100, 100, 100, 100, 110, 112], [15] * 6, [10] * 6, ["Jun 2025"] + QL))
    assert ledger.score(_g(metric="revenue", low=8, high=10, unit="pct"), fin, (2026, 3))["status"] == "on_track"


def test_margin_miss_that_depends_on_other_income_is_not_a_verdict():
    fund = _fund([100, 100, 100, 100, 100, 100], [25] * 6, [10] * 6, ["Jun 2025"] + QL)
    fund["series"]["quarterly"]["other_income"] = [6] * 6
    v = ledger.score(_g(metric="operating_margin", low=30, high=31, margin_basis="ebitda"), ledger.Financials(fund), (2026, 3))
    assert v["status"] == "unmeasured" and "including it" in v["detail"]


def test_complete_year_older_than_quarterly_window_uses_annual_table():
    fund = _fund([100] * 6, [15] * 6, [10] * 6, ["Jun 2025"] + QL)
    fund["series"]["annual"] = {"labels": ["Mar 2022", "Mar 2023"], "revenue": [100, 105], "op_profit": [25, 27], "pat": [10, 11], "capex": [5, 5]}
    v = ledger.score(_g(target_fy=2023, low=7, high=8, unit="pct"), ledger.Financials(fund), (2022, 9))
    assert v["status"] == "missed" and "FY23" in v["detail"] and "+5.0%" in v["detail"]


def test_feed_keeps_history_when_a_company_fails_to_load(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from finn.push import digest
    monkeypatch.setattr(digest, "collect", lambda companies, net: [])
    ts = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    old = {"id": "x1", "symbol": "TITAN", "company": "Titan", "ts": ts, "title": "Financial Results for the quarter ended 30 September 2026",
           "source_text": None, "url": "https://example.com/a.pdf", "source": "BSE", "category": "results", "tag": "filing",
           "score": 92, "tier": "act", "rule": "results_filing", "needs_read": True, "related": [], "read": {"status": "ok", "kind": "results"}}

    class NoModel:
        def available(self, tier):
            return False

    feed = digest.build_feed({}, llm=NoModel(), net=None, previous={"items": [old]}, portfolio=["TITAN"])
    assert [i["id"] for i in feed["items"]] == ["x1"]


def test_exchange_clarification_request_needs_attention():
    v = classify("Clarification Sought From Sun Pharmaceutical Industries Ltd", "Exchange has sought clarification with reference to a news report")
    assert v.tier == "act" and v.tag == "exchange_query"
    assert classify("Rumour verification - Regulation 30(11)", "").tier == "know"


def test_knowledge_bio_accepted_only_when_confident_and_specific():
    from finn.push.enrich import accept_knowledge_bio
    good = {"found": True, "confidence": "high", "background": "Long career at a large private bank, rising to executive director and later chief executive of its insurance arm.",
            "previous_roles": ["Executive Director, Example Bank"], "education": "MBA"}
    assert accept_knowledge_bio(good)["background_source"] == "model"
    assert accept_knowledge_bio({**good, "confidence": "medium"}) == {}
    assert accept_knowledge_bio({**good, "found": False}) == {}
    assert accept_knowledge_bio({**good, "previous_roles": []}) == {}
    assert accept_knowledge_bio("not json") == {}


def _item(id_, tag, ts, title, **kw):
    v = {"id": id_, "symbol": "SUNPHARMA", "company": "Sun Pharma", "ts": ts, "title": title, "source_text": None,
         "url": f"https://example.com/{id_}.pdf", "source": "BSE", "category": "press_release", "tag": tag, "score": 62,
         "tier": "know", "rule": tag, "needs_read": True, "related": [], "read": {"status": "rules"}}
    v.update(kw)
    return v


def test_exchange_query_and_company_reply_become_one_event():
    from finn.push.digest import cluster
    query = _item("q", "exchange_query", "2026-11-02T14:23:00+05:30", "Clarification Sought From Sun Pharmaceutical Industries Ltd", score=72, tier="act")
    reply = _item("r", "clarification", "2026-11-02T20:58:00+05:30", "Rumour verification - Regulation 30(11)", category="unclassified",
                  score=38, tier="buried", headline="Report refers to August events, not a new development", read={"status": "ok"})
    other = _item("o", "clarification", "2026-11-20T10:00:00+05:30", "Rumour verification - Regulation 30(11)")  # weeks later: separate
    out = cluster([reply, query, other])
    ids = sorted(i["id"] for i in out)
    assert ids == ["o", "q"]
    lead = next(i for i in out if i["id"] == "q")
    assert [r["url"] for r in lead["related"]] == ["https://example.com/r.pdf"]
    assert lead["why"].startswith("Company's reply: Report refers to August events")


def test_model_cannot_demote_an_exchange_query():
    verdict = classify("Clarification Sought From Sun Pharmaceutical Industries Ltd")
    assert blend(verdict, 0)[1] >= 70 and blend(verdict, 1)[1] >= 70


def test_lender_aum_guidance_is_not_revenue():
    lender = _fund([100, 100, 100, 100, 110, 112], [15] * 6, [10] * 6, ["Jun 2025"] + QL)
    lender["is_lender"] = True
    fin = ledger.Financials(lender)
    aum = ledger.score(_g(low=20, high=24, statement="We're confident of a 20% to 24% AUM growth", label="FY27 AUM growth"), fin, (2026, 3))
    vague = ledger.score(_g(low=26, high=27, statement="Between 26%, 27% growth we should deliver on a full year basis", label="FY27 revenue growth"), fin, (2026, 3))
    income = ledger.score(_g(low=8, high=10, statement="Total income should grow 8-10% this year", label="FY27 revenue growth"), fin, (2026, 3))
    assert aum["status"] == "unmeasured" and "AUM" in aum["detail"]
    assert vague["status"] == "unmeasured"
    assert income["status"] == "on_track"


def _fund_annual(revenues, labels):
    fund = _fund([100] * 6, [15] * 6, [10] * 6, ["Jun 2025"] + QL)
    n = len(labels)
    fund["series"]["annual"] = {"labels": labels, "revenue": revenues, "op_profit": [1] * n, "pat": [1] * n, "capex": [1] * n}
    return fund


def test_average_growth_promise_is_scored_as_cagr_never_one_year():
    fin = ledger.Financials(_fund_annual([100, 113, 115.3, 123.1], ["Mar 2024", "Mar 2025", "Mar 2026", "Mar 2027"]))
    v = ledger.score(_g(low=13, high=13, target_fy=2025, statement="We will sustain our growth of 13% on an average"), fin, (2024, 3))
    assert "compound annual rate" in v["detail"] and v["status"] == "behind"
    one_year = ledger.Financials(_fund_annual([100, 113], ["Mar 2024", "Mar 2025"]))
    assert ledger.score(_g(low=13, high=13, statement="13% on an average"), one_year, (2024, 3))["status"] == "unmeasured"


def test_onwards_starts_the_cagr_at_the_named_year():
    fin = ledger.Financials(_fund_annual([100, 110, 121, 133.1], ["Mar 2024", "Mar 2025", "Mar 2026", "Mar 2027"]))
    v = ledger.score(_g(low=10, high=10, target_fy=2026, statement="double-digit growth of 10% from FY26 onwards"), fin, (2024, 3))
    assert v["detail"].startswith("FY26–FY27") and v["status"] == "on_track"


def test_partial_capex_promise_is_not_compared_with_total_spend():
    fin = ledger.Financials(_fund_annual([100, 110], ["Mar 2025", "Mar 2026"]))
    part = ledger.score(_g(metric="capex", target_fy=2026, low=700, high=700, unit="inr_cr", statement="We have committed closer to 700 crores of CapEx this year."), fin, (2025, 6))
    whole = ledger.score(_g(metric="capex", target_fy=2026, low=1, high=1, unit="inr_cr", statement="Total capex for the year will be about 1 crore"), fin, (2025, 6))
    assert part["status"] == "unmeasured" and "like for like" in part["detail"]
    assert whole["status"] == "delivered"


def test_industry_forecasts_and_undated_aspirations_are_not_scored():
    lender = _fund_annual([100, 111], ["Mar 2025", "Mar 2026"]); lender["is_lender"] = True
    lender["series"]["annual"]["deposits"] = [100, 111]
    fin = ledger.Financials(lender)
    industry = ledger.score(_g(metric="deposit_growth", target_fy=2026, low=11, high=12, statement="Deposit growth of scheduled commercial banks may remain 11-12%"), fin, (2025, 6))
    assert industry["status"] == "unmeasured" and "industry" in industry["detail"]
    fin2 = ledger.Financials(_fund([100, 100, 100, 100, 110, 112], [15] * 6, [4] * 6, ["Jun 2025"] + QL))
    eventually = ledger.score(_g(metric="operating_margin", target_fy=2027, low=20, high=20, statement="Eventually, the margin should rise beyond 20%"), fin2, (2026, 3))
    assert eventually["status"] == "unmeasured"
    dated = ledger.score(_g(metric="operating_margin", target_fy=2027, low=14, high=16, firmness="aspiration", statement="We aim for 14-16% margin for FY27"), fin2, (2026, 3))
    assert dated["status"] != "unmeasured"   # the model's label alone does not hide a dated promise


def test_numeric_level_without_period_is_not_a_direction_check():
    fin = ledger.Financials(_fund([100] * 6, [30] * 6, [10] * 6, ["Jun 2025"] + QL))
    v = ledger.score(_g(metric="operating_margin", target_fy=None, low=30, high=30, direction="flat", statement="We will maintain 30% operating margin"), fin, (2026, 3))
    assert v["status"] == "unmeasured"


def test_single_year_promise_with_a_multi_year_aside_in_the_quote_stays_single_year():
    fin = ledger.Financials(_fund_annual([100, 106.8], ["Mar 2025", "Mar 2026"]))
    v = ledger.score(_g(low=8, high=10, target_fy=2026, statement="The company expects revenue growth of 8% to 10% for the current year.",
                        quote="the guidance is 8%-10%, and it may so happen a double-digit growth from next year onwards."), fin, (2025, 3))
    assert v["status"] == "missed" and "FY26" in v["detail"]


def test_size_rule_relative_to_market_value():
    from finn.push.digest import apply_size_rule, stated_amounts_cr
    eternal = {"category": "m_and_a", "score": 78, "read": {"status": "ok"}, "headline": "Consolidating business; swapping stake",
               "facts": [{"label": "ZEPL cash consideration", "value": "INR 130.76 crore"}, {"label": "Mygate stake value", "value": "INR 156 crore"}]}
    apply_size_rule(eternal, 350935)
    assert eternal["score"] <= 60 and "0.04%" in eternal["size_note"]
    persistent = {"category": "fund_raise", "score": 86, "read": {"status": "ok"}, "facts": [{"label": "Raised", "value": "INR 42,646.09 million"}]}
    apply_size_rule(persistent, 99550)
    assert persistent["score"] == 86  # ₹4,265 cr is 4.3% of market value: stays an interruption
    no_amount = {"category": "m_and_a", "score": 86, "read": {"status": "ok"}, "facts": [{"label": "Stake", "value": "49%"}]}
    apply_size_rule(no_amount, 23057)
    assert no_amount["score"] == 86 and "size_note" not in no_amount
    assert stated_amounts_cr({"facts": [{"value": "₹70 billion"}, {"value": "Rs 5 lakh"}]}) == [7000.0, 0.05]
    price = {"category": "fund_raise", "score": 86, "read": {"status": "ok"}, "facts": [{"label": "Floor price", "value": "₹ 992.25"}]}
    apply_size_rule(price, 695439)
    assert price["score"] == 86  # a per-share price is not a deal size
