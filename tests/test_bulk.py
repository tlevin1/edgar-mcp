"""Bulk tie-out checks on a hand-built Financial Statement Data Set.

Each filing below reproduces a case found in the real 2026 data, so every
expected status and cause can be checked by reading the fixture.
"""

import pytest

pytest.importorskip("pyspark")

from edgar_bulk import pandas_baseline as P  # noqa: E402
from edgar_bulk import tieouts as T  # noqa: E402
from edgar_bulk.schemas import NUM_COLUMNS, SUB_COLUMNS, TAG_COLUMNS  # noqa: E402

D = "20251231"
CUSTOM = "custom"  # stands in for a custom tag's version, which is its filing's accession number
M = 1_000_000_000  # $1B, so gaps are far above the $1M tolerance floor

# adsh -> (name, form, period)
FILINGS = {
    "clean": ("Clean Co", "10-K", D),
    "spac": ("Blank Check Corp", "10-K", D),
    "missing": ("Custom Tag Inc", "10-Q", D),
    "debit": ("Coincidence Inc", "10-K", D),
    "twice": ("Tagged Twice LP", "10-K", D),
    "nosub": ("No Subtotal Co", "10-K", D),
    "week52": ("Fiscal Week Inc", "10-K", "20250930"),  # SEC rounds 2025-09-27 to month end
    "twoaxes": ("Two Axes Inc", "10-K", D),
    "foreign": ("Overseas plc", "20-F", D),
}

# adsh, tag, version, ddate, value, segments, coreg
FACTS = [
    # Clean: 10 = 6 + 4. Segment and co-registrant rows must be ignored.
    ("clean", "Assets", "us-gaap/2025", D, 10 * M, None, None),
    ("clean", "Assets", "us-gaap/2025", "20241231", 9 * M, None, None),  # prior-year comparative
    ("clean", "Assets", "us-gaap/2025", D, 3 * M, "BusinessSegments=Europe;", None),
    ("clean", "Assets", "us-gaap/2025", D, 2 * M, None, "SubsidiaryCo"),
    ("clean", "Liabilities", "us-gaap/2025", D, 6 * M, None, None),
    ("clean", "StockholdersEquity", "us-gaap/2025", D, 4 * M, None, None),
    ("clean", "LiabilitiesAndStockholdersEquity", "us-gaap/2025", D, 10 * M, None, None),
    # SPAC: redeemable shares tagged only by share class, under two members of
    # the same axis for the same line.
    ("spac", "Assets", "us-gaap/2025", D, 5 * M, None, None),
    ("spac", "Liabilities", "us-gaap/2025", D, 1 * M, None, None),
    ("spac", "StockholdersEquity", "us-gaap/2025", D, -1 * M, None, None),
    ("spac", "LiabilitiesAndStockholdersEquity", "us-gaap/2025", D, 5 * M, None, None),
    ("spac", "TemporaryEquityCarryingAmountAttributableToParent", "us-gaap/2025", D, 5 * M, "ClassOfStock=CommonClassA;", None),
    ("spac", "TemporaryEquityCarryingAmountAttributableToParent", "us-gaap/2025", D, 5 * M, "ClassOfStock=SharesSubjectToRedemption;", None),
    ("spac", "AssetsHeldInTrustNoncurrent", "us-gaap/2025", D, 5 * M, None, None),
    # Missing tag: a company-specific mezzanine tag explains the 2B gap.
    ("missing", "Assets", "us-gaap/2025", D, 10 * M, None, None),
    ("missing", "Liabilities", "us-gaap/2025", D, 5 * M, None, None),
    ("missing", "StockholdersEquity", "us-gaap/2025", D, 3 * M, None, None),
    ("missing", "LiabilitiesAndStockholdersEquity", "us-gaap/2025", D, 10 * M, None, None),
    ("missing", "MezzanineUnits", CUSTOM, D, 2 * M, None, None),
    # Debit coincidence: the only tag equal to the gap is an asset, which can't explain it.
    ("debit", "Assets", "us-gaap/2025", D, 10 * M, None, None),
    ("debit", "Liabilities", "us-gaap/2025", D, 5 * M, None, None),
    ("debit", "StockholdersEquity", "us-gaap/2025", D, 3 * M, None, None),
    ("debit", "LiabilitiesAndStockholdersEquity", "us-gaap/2025", D, 10 * M, None, None),
    ("debit", "MarketableSecuritiesNoncurrent", "us-gaap/2025", D, 2 * M, None, None),
    # One mezzanine line tagged twice with equal values (KKR): count it once.
    ("twice", "Assets", "us-gaap/2025", D, 10 * M, None, None),
    ("twice", "Liabilities", "us-gaap/2025", D, 6 * M, None, None),
    ("twice", "StockholdersEquity", "us-gaap/2025", D, 3 * M, None, None),
    ("twice", "LiabilitiesAndStockholdersEquity", "us-gaap/2025", D, 10 * M, None, None),
    ("twice", "TemporaryEquityCarryingAmountAttributableToParent", "us-gaap/2025", D, 1 * M, None, None),
    ("twice", "RedeemableNoncontrollingInterestEquityCarryingAmount", "us-gaap/2025", D, 1 * M, None, None),
    # No Liabilities subtotal: skipped, not failed.
    ("nosub", "Assets", "us-gaap/2025", D, 10 * M, None, None),
    ("nosub", "StockholdersEquity", "us-gaap/2025", D, 4 * M, None, None),
    ("nosub", "LiabilitiesAndStockholdersEquity", "us-gaap/2025", D, 10 * M, None, None),
    # 52/53-week year: balance sheet dated 2025-09-27, sub.period says 2025-09-30.
    ("week52", "Assets", "us-gaap/2025", "20250927", 8 * M, None, None),
    ("week52", "Liabilities", "us-gaap/2025", "20250927", 5 * M, None, None),
    ("week52", "StockholdersEquity", "us-gaap/2025", "20250927", 3 * M, None, None),
    ("week52", "LiabilitiesAndStockholdersEquity", "us-gaap/2025", "20250927", 8 * M, None, None),
    # BlackRock: the gap is the sum of members on two different axes. Summing
    # across axes can double count, so this stays unexplained by design.
    ("twoaxes", "Assets", "us-gaap/2025", D, 10 * M, None, None),
    ("twoaxes", "Liabilities", "us-gaap/2025", D, 5 * M, None, None),
    ("twoaxes", "StockholdersEquity", "us-gaap/2025", D, 2 * M, None, None),
    ("twoaxes", "LiabilitiesAndStockholdersEquity", "us-gaap/2025", D, 10 * M, None, None),
    ("twoaxes", "TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests", "us-gaap/2025", D, 2 * M, "ClassOfStock=SubCo;", None),
    ("twoaxes", "TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests", "us-gaap/2025", D, 1 * M, "ConsolidatedEntities=Funds;", None),
    # A 20-F is not a 10-K/10-Q: excluded entirely, even though it fails.
    ("foreign", "Assets", "us-gaap/2025", D, 10 * M, None, None),
    ("foreign", "LiabilitiesAndStockholdersEquity", "us-gaap/2025", D, 1 * M, None, None),
]

# tag, version, crdr
TAGS = [
    ("MezzanineUnits", "missing", "C"),  # version = accession of the "missing" filing
    ("MarketableSecuritiesNoncurrent", "us-gaap/2025", "D"),
    ("AssetsHeldInTrustNoncurrent", "us-gaap/2025", "D"),
    ("TemporaryEquityCarryingAmountAttributableToParent", "us-gaap/2025", "C"),
    ("TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests", "us-gaap/2025", "C"),
]


def _tsv(path, columns, rows):
    names = [c for c, _ in columns]
    lines = ["\t".join(names)] + ["\t".join("" if r.get(n) is None else str(r[n]) for n in names) for r in rows]
    path.write_text("\n".join(lines) + "\n")


@pytest.fixture(scope="module")
def quarter_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("data") / "2026q1"
    d.mkdir()
    _tsv(d / "sub.txt", SUB_COLUMNS, [
        {"adsh": a, "cik": i, "name": n, "form": f, "period": p, "fy": 2025, "fp": "FY", "filed": "20260215"}
        for i, (a, (n, f, p)) in enumerate(FILINGS.items(), 1)
    ])
    _tsv(d / "num.txt", NUM_COLUMNS, [
        {"adsh": a, "tag": t, "version": a if v == CUSTOM else v, "ddate": dd, "qtrs": 0, "uom": "USD",
         "segments": seg, "coreg": co, "value": f"{val}.0000"}
        for a, t, v, dd, val, seg, co in FACTS
    ])
    _tsv(d / "tag.txt", TAG_COLUMNS, [
        {"tag": t, "version": v, "custom": int(not v.startswith("us-gaap/")), "abstract": 0,
         "datatype": "monetary", "iord": "I", "crdr": c}
        for t, v, c in TAGS
    ])
    return str(d)


@pytest.fixture(scope="module")
def spark():
    from edgar_bulk.job import spark_session

    s = spark_session("tests")
    yield s
    s.stop()


@pytest.fixture(scope="module")
def results(spark, quarter_dir):
    sub, num, tags = T.load_raw(spark, quarter_dir)
    facts = T.consolidated_instants(num, sub)
    res = T.run_checks(T.balance_sheets(facts))
    causes = T.explain_failures(res, facts, num, tags)
    rows = res.join(causes, "adsh", "left").collect()
    return {r["adsh"]: r for r in rows}


def test_only_10k_and_10q_are_checked(results):
    assert "foreign" not in results
    assert set(results) == set(FILINGS) - {"foreign"}


def test_clean_filing_passes_and_ignores_segments_and_coregistrants(results):
    r = results["clean"]
    assert (r["balance_status"], r["lpe_status"]) == ("pass", "pass")
    assert r["Assets"] == 10 * M  # not 10 + 3 + 2, and not the prior-year 9


def test_52_week_year_is_not_dropped_by_month_end_rounding(results):
    r = results["week52"]
    assert r["ddate"] == "20250927" and r["lpe_status"] == "pass"


def test_missing_liabilities_subtotal_is_skipped_and_flagged(results):
    r = results["nosub"]
    assert r["lpe_status"] == "skipped" and r["no_liabilities_subtotal"]


def test_same_line_tagged_twice_is_counted_once(results):
    assert results["twice"]["lpe_status"] == "pass"


def test_spac_shares_tagged_only_by_class_are_explained(results):
    r = results["spac"]
    assert r["lpe_status"] == "fail"
    assert r["cause"] == "dimensional_only"
    assert r["explained_by"] == "TemporaryEquityCarryingAmountAttributableToParent by ClassOfStock"


def test_custom_credit_tag_explains_gap(results):
    r = results["missing"]
    assert r["lpe_status"] == "fail" and r["lpe_diff"] == -2 * M
    assert (r["cause"], r["explained_by"]) == ("missing_tag", "MezzanineUnits")


def test_asset_with_matching_value_is_not_an_explanation(results):
    r = results["debit"]
    assert r["lpe_status"] == "fail" and r["cause"] == "unexplained"


def test_members_on_different_axes_are_not_summed(results):
    r = results["twoaxes"]
    assert r["lpe_status"] == "fail" and r["cause"] == "unexplained"


def test_pandas_baseline_agrees_with_spark(results, quarter_dir):
    sub, num = P.read_tsv_quarter(quarter_dir)
    pdf = P.run_checks(sub, num).set_index("adsh")
    for adsh, r in results.items():
        assert (pdf.loc[adsh, "balance_status"], pdf.loc[adsh, "lpe_status"]) == (r["balance_status"], r["lpe_status"]), adsh
