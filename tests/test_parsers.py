"""Parser tests against a synthetic page that mimics screener.in's structure (no network)."""
from pathlib import Path

from finn.pull import fundamentals
from finn.sources.screener import parse_company

HTML = (Path(__file__).parent / "fixtures" / "screener_min.html").read_text(encoding="utf-8")


def company():
    return parse_company(HTML, "example", "https://www.screener.in/company/EXAMPLE/consolidated/")


def test_identity_and_top_ratios():
    c = company()
    assert (c.symbol, c.name, c.bse_code, c.nse_symbol) == ("EXAMPLE", "Example Industries Ltd", "500001", "EXAMPLE")
    assert c.top["Market Cap"] == 123456.0 and c.top["High / Low"] == [1612.0, 1152.0]
    assert c.sector_path == ["Energy", "Oil & Gas"]  # index links are not sectors


def test_tables_skip_raw_pdf_and_keep_negatives_and_blanks():
    c = company()
    assert c.quarters.columns[-1] == "Sep 2026"
    assert "Raw PDF" not in c.quarters.rows
    assert c.quarters.row("Sales") == [1000, 1100, 1200, 1320]
    assert c.quarters.value("Operating Profit", "Sep 2026") == -20
    assert c.quarters.row("OPM %")[-1] is None
    assert "CFO/EBITDA" in c.cashflow.rows  # tooltip text is not part of the label


def test_documents():
    c = company()
    first, second = c.announcements
    assert first["url"] == "https://www.screener.in/company/id/1/"  # relative links made absolute
    assert first["ts"] == "2026-11-15T00:21:18+05:30" and first["title"] == "Clarification Sought"
    assert second["summary"] == "Rating reaffirmed at AA+."
    assert c.annual_reports[0]["year"] == 2026
    assert [x["call"] for x in c.concalls] == ["2026-11", "2026-08"]  # same-month entries merged
    assert c.concalls[0]["transcript"].endswith("t1.pdf") and c.concalls[0]["recording"].endswith("rec")
    assert c.growth["Compounded Sales Growth"]["3 Years"] == 12.0


def test_fundamentals_tolerate_sparse_pages():
    facts = fundamentals.build(company())
    assert facts["snapshot"]["quarter"] == "Sep 2026" and isinstance(facts["facts"], list)
