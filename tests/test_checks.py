from edgar_mcp import facts as F
from edgar_mcp.checks import run_checks

from .conftest import ACCN, END, Q_ACCN, clean_10k, payload, row


def statuses(result):
    return {c["name"]: c["status"] for c in result["checks"]}


def latest(facts, form="10-K"):
    return F.list_filings(facts, form)[0]


def test_clean_filing_passes_everything(clean_facts):
    result = run_checks(clean_facts, latest(clean_facts))
    assert result["summary"] == {"pass": 5, "fail": 0, "skipped": 0}


def test_filing_period_end_ignores_prior_year_comparatives(clean_facts):
    filing = latest(clean_facts)
    assert filing.accn == ACCN and filing.period_end == END


def test_broken_balance_sheet_fails_with_difference():
    c = clean_10k()
    c["Assets"] = [row(1050, END)]  # $50 that no liability or equity explains
    facts = payload(c)
    # Scale tolerance down: synthetic numbers are tiny next to the $1M floor.
    result = run_checks(facts, latest(facts), rel_tol=0.0)
    bs = next(x for x in result["checks"] if x["name"] == "balance_sheet_balances")
    assert bs["difference"] == 50


def test_breaks_are_caught_at_real_scale():
    c = clean_10k()
    scaled = {k: [{**r, "val": r["val"] * 1_000_000} for r in rows] for k, rows in c.items()}
    scaled["GrossProfit"] = [{**scaled["GrossProfit"][0], "val": 210_000_000}]  # off by $10M
    facts = payload(scaled)
    s = statuses(run_checks(facts, latest(facts)))
    assert s["gross_profit"] == "fail"
    assert s["balance_sheet_balances"] == "pass"


def test_missing_input_is_skipped_not_passed():
    c = clean_10k()
    del c["GrossProfit"]
    facts = payload(c)
    result = run_checks(facts, latest(facts))
    gp = next(x for x in result["checks"] if x["name"] == "gross_profit")
    assert gp["status"] == "skipped" and "GrossProfit" in gp["note"]


def test_untagged_liabilities_subtotal_is_explained():
    c = clean_10k()
    del c["Liabilities"]
    facts = payload(c)
    check = next(x for x in run_checks(facts, latest(facts))["checks"] if x["name"] == "liabilities_plus_equity")
    assert check["status"] == "skipped" and "tagging gap" in check["note"]


def test_noncontrolling_interest_added_when_only_parent_equity_tagged():
    c = clean_10k()
    c["StockholdersEquity"] = [row(380, END)]
    c["MinorityInterest"] = [row(20, END)]
    facts = payload(c)
    check = next(x for x in run_checks(facts, latest(facts))["checks"] if x["name"] == "liabilities_plus_equity")
    assert check["status"] == "pass" and check["computed"] == 1000
    assert "MinorityInterest" in check["note"]


def test_missing_fx_effect_is_treated_as_zero_and_noted():
    c = clean_10k()
    del c["EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]
    facts = payload(c)
    check = next(x for x in run_checks(facts, latest(facts), rel_tol=0.0)["checks"] if x["name"] == "cash_flow_sections_sum")
    assert check["computed"] == 40 and "treated as zero" in check["note"]


def test_roll_forward_uses_alternate_balance_tag_consistently():
    c = clean_10k()
    balances = c.pop("CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents")
    c["CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations"] = balances
    facts = payload(c)
    assert statuses(run_checks(facts, latest(facts)))["cash_roll_forward"] == "pass"


def test_10q_compares_matching_periods():
    """A 10-Q tags both the quarter and year-to-date. Mixing them would break
    the identity; every term must share the same start date."""
    q = lambda v, start: row(v, "2026-06-30", accn=Q_ACCN, start=start, form="10-Q", fy=2026, fp="Q2", filed="2026-08-01")  # noqa: E731
    facts = payload({
        "RevenueFromContractWithCustomerExcludingAssessedTax": [q(100, "2026-04-01"), q(190, "2026-01-01")],
        "CostOfGoodsAndServicesSold": [q(60, "2026-04-01"), q(115, "2026-01-01")],
        "GrossProfit": [q(40, "2026-04-01"), q(75, "2026-01-01")],
    })
    gp = next(x for x in run_checks(facts, latest(facts, "10-Q"), rel_tol=0.0)["checks"] if x["name"] == "gross_profit")
    assert gp["status"] == "pass"
    assert {i["start"] for i in gp["inputs"]} == {"2026-01-01"}


def test_financials_derives_fcf_and_reports_gaps_as_null(clean_facts):
    items = F.financials(clean_facts, latest(clean_facts))["line_items"]
    assert items["revenue"]["value"] == 500
    assert items["revenue"]["concept"] == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert items["free_cash_flow"]["value"] == 110
    assert items["eps_diluted"] is None
