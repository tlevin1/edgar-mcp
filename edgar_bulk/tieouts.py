"""Balance-sheet tie-out checks over every filing in a Financial Statement
Data Set, as Spark transformations.

Nothing here runs until an action (count, collect, write) is called: each
function adds steps to a query plan that Spark optimizes as a whole.
"""

from __future__ import annotations

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

from edgar_mcp.checks import (
    ABS_TOL,
    DEFAULT_REL_TOL,
    RNCI_PARTS,
    RNCI_TOTAL,
    TEMP_EQUITY_PARENT,
    TEMP_EQUITY_TOTAL,
)
from .schemas import NUM_SCHEMA, SUB_SCHEMA, TAG_SCHEMA

FORMS = ["10-K", "10-Q"]

# Same temporary-equity rules and tag names as the single-filing check.
TEMP_EQUITY = [TEMP_EQUITY_TOTAL, TEMP_EQUITY_PARENT, RNCI_TOTAL, *RNCI_PARTS]
KEY_TAGS = [
    "Assets",
    "Liabilities",
    "LiabilitiesAndStockholdersEquity",
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    "MinorityInterest",
    *TEMP_EQUITY,
]


def read_tsv(spark: SparkSession, path: str, schema) -> DataFrame:
    return spark.read.csv(path, sep="\t", header=True, schema=schema, quote="", enforceSchema=True)


def load_raw(spark: SparkSession, quarter_dir: str) -> tuple[DataFrame, DataFrame, DataFrame]:
    """Read one quarter's TSVs. `quarter` is added to sub so results can be
    traced back to their source dataset, matching the Parquet lake layout."""
    quarter = quarter_dir.rstrip("/").split("/")[-1]
    return (
        read_tsv(spark, f"{quarter_dir}/sub.txt", SUB_SCHEMA).withColumn("quarter", F.lit(quarter)),
        read_tsv(spark, f"{quarter_dir}/num.txt", NUM_SCHEMA),
        read_tsv(spark, f"{quarter_dir}/tag.txt", TAG_SCHEMA),
    )


def filings(sub: DataFrame) -> DataFrame:
    """The small side of the join: one row per 10-K / 10-Q."""
    return sub.filter(F.col("form").isin(FORMS)).select("adsh", "cik", "name", "sic", "form", "fy", "fp", "filed", "quarter")


def consolidated_instants(num: DataFrame, sub: DataFrame) -> DataFrame:
    """Company-wide, USD, point-in-time values from 10-Ks and 10-Qs.

    - segments null: the total, not a breakdown by segment or other axis
    - coreg null: the parent registrant, not a co-registrant subsidiary
    - qtrs == 0: a balance-sheet date, not a period flow

    The join to `filings` also drops 20-Fs, S-1s, etc. `filings` is a few
    thousand rows, so it is broadcast: every executor gets a full copy and
    joins locally, and the millions of `num` rows never move (no shuffle).
    """
    vals = num.filter(
        (F.col("qtrs") == 0)
        & (F.col("uom") == "USD")
        & F.col("segments").isNull()
        & F.col("coreg").isNull()
        & F.col("value").isNotNull()
    ).select("adsh", "tag", "version", "ddate", "value")
    return vals.join(F.broadcast(filings(sub)), "adsh")


def balance_sheets(facts: DataFrame) -> DataFrame:
    """One row per filing, one column per key tag, at the filing's own
    balance-sheet date.

    The date comes from the data, not sub.period: SEC rounds `period` to the
    month end, so Apple's 2025-09-27 year end appears as 2025-09-30 and a join
    on it would silently drop every 52/53-week filer.
    """
    std = facts.filter(F.col("version").startswith("us-gaap/") & F.col("tag").isin(KEY_TAGS))
    bs_date = std.filter(F.col("tag") == "Assets").groupBy("adsh").agg(F.max("ddate").alias("ddate"))
    # One row per filing: small enough to broadcast. Spark can't know that from
    # an aggregate's estimated size, so without the hint it shuffles both sides.
    at_date = std.join(F.broadcast(bs_date), ["adsh", "ddate"])
    # Passing the pivot values explicitly saves Spark a pass over the data to
    # discover them, and guarantees every column exists even if no filer used it.
    return (
        at_date.groupBy("adsh", "cik", "name", "sic", "form", "fy", "fp", "filed", "quarter", "ddate")
        .pivot("tag", KEY_TAGS)
        .agg(F.first("value"))
    )


def _sum_if_any(cols):
    """Sum treating nulls as 0, but null if every input is null."""
    return F.when(F.coalesce(*cols).isNotNull(), sum(F.coalesce(c, F.lit(0)) for c in cols))


def _temporary_equity():
    rnci = F.coalesce(F.col(RNCI_TOTAL), _sum_if_any([F.col(t) for t in RNCI_PARTS]))
    parent = F.col(TEMP_EQUITY_PARENT)
    # Measured on 2026Q1: when parent and RNCI values differ they are separate
    # lines and their sum ties out (12/12); when exactly equal it is one line
    # tagged twice (e.g. KKR) and counting it once ties out.
    parent_plus_rnci = F.when(parent == rnci, parent).otherwise(_sum_if_any([parent, rnci]))
    return F.coalesce(F.col(TEMP_EQUITY_TOTAL), parent_plus_rnci, F.lit(0))


def _status(reported, computed, inputs_present):
    diff = computed - reported
    tol = F.greatest(F.lit(ABS_TOL), F.lit(DEFAULT_REL_TOL) * F.greatest(F.abs(computed), F.abs(reported)))
    status = F.when(~inputs_present, "skipped").when(F.abs(diff) <= tol, "pass").otherwise("fail")
    return status, F.when(inputs_present, diff)


def run_checks(bs: DataFrame) -> DataFrame:
    """The same two balance-sheet identities as edgar-mcp's check_tieouts,
    with the same tolerance, applied to every filing at once."""
    a, le, liab = F.col("Assets"), F.col("LiabilitiesAndStockholdersEquity"), F.col("Liabilities")
    se = F.col("StockholdersEquity")
    se_total = F.col("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")
    # Total equity: the consolidated tag if present, else parent equity plus NCI.
    equity = F.coalesce(se_total, se + F.coalesce(F.col("MinorityInterest"), F.lit(0)))
    temp = _temporary_equity()

    bal_status, bal_diff = _status(le, a, a.isNotNull() & le.isNotNull())
    lpe_status, lpe_diff = _status(le, liab + equity + temp, le.isNotNull() & liab.isNotNull() & equity.isNotNull())
    return bs.select(
        "adsh", "cik", "name", "sic", "form", "fy", "fp", "filed", "quarter", "ddate",
        "Assets", "Liabilities", "LiabilitiesAndStockholdersEquity",
        equity.alias("total_equity"), temp.alias("temporary_equity"),
        bal_status.alias("balance_status"), bal_diff.alias("balance_diff"),
        lpe_status.alias("lpe_status"), lpe_diff.alias("lpe_diff"),
        F.when(liab.isNull() & le.isNotNull() & equity.isNotNull(), True).otherwise(False).alias("no_liabilities_subtotal"),
    )


def _credit_tags(tags: DataFrame) -> DataFrame:
    return tags.filter(F.col("crdr") == "C").select("tag", "version")


def explain_failures(results: DataFrame, facts: DataFrame, num: DataFrame, tags: DataFrame) -> DataFrame:
    """Attribute each failed liabilities-plus-equity check to a cause.

    Pass 1, `missing_tag`: a single company-wide tag in the same filing and
    date whose value equals the gap, i.e. a line item the identity left out
    (a custom equity tag, a redeemable-share variant).

    Pass 2, `dimensional_only`: the members of one tag along one axis sum to
    the gap. The filer reported the line only as a breakdown, never as a
    company-wide total, so the consolidated filter never sees it. BlackRock's
    temporary equity is tagged this way.

    Only credit-balance tags (crdr = 'C': liabilities and equity) can explain a
    gap on that side of the balance sheet. Without this, SPACs "matched" their
    trust account, an asset that merely equals their redeemable shares.

    Failures and the tag dictionary are small, so both are broadcast against
    the large tables and the big side is never shuffled for the join.
    """
    fails = results.filter(F.col("lpe_status") == "fail").select("adsh", "ddate", F.abs("lpe_diff").alias("gap"))
    credit = F.broadcast(_credit_tags(tags))
    tol = F.greatest(F.lit(ABS_TOL), F.lit(DEFAULT_REL_TOL) * F.col("gap"))

    single = (
        facts.join(credit, ["tag", "version"])
        .join(F.broadcast(fails), ["adsh", "ddate"])
        .filter(~F.col("tag").isin(KEY_TAGS) & (F.abs(F.abs("value") - F.col("gap")) <= tol))
        # A filing can match several tags (a subtotal and its only component);
        # prefer standard tags, then alphabetical, so the pick is deterministic.
        .groupBy("adsh")
        .agg(F.min(F.struct((~F.col("version").startswith("us-gaap/")).alias("custom"), "tag")).alias("m"))
        .select("adsh", F.col("m.tag").alias("explained_by"), F.lit("missing_tag").alias("cause"))
    )

    dims = (
        num.filter(
            (F.col("qtrs") == 0) & (F.col("uom") == "USD") & F.col("coreg").isNull()
            & F.col("segments").isNotNull()
            # one axis only: "Axis=Member;" - multi-axis rows overlap and would double count
            & (F.size(F.split(F.regexp_replace("segments", ";$", ""), ";")) == 1)
        )
        .join(credit, ["tag", "version"])
        .join(F.broadcast(fails), ["adsh", "ddate"])
        .withColumn("axis", F.split("segments", "=")[0])
        .groupBy("adsh", "gap", "tag", "axis")
        # Also try distinct values: SPACs often tag one line under two members of
        # the same axis (CommonClassA and ...SharesSubjectToRedemption), which a
        # plain sum would double.
        .agg(F.sum("value").alias("total"), F.sum_distinct("value").alias("distinct_total"))
        .filter(
            (F.abs(F.abs("total") - F.col("gap")) <= tol)
            | (F.abs(F.abs("distinct_total") - F.col("gap")) <= tol)
        )
        .groupBy("adsh")
        .agg(F.min(F.struct("tag", "axis")).alias("m"))
        .select("adsh", F.concat_ws(" by ", "m.tag", "m.axis").alias("explained_by"), F.lit("dimensional_only").alias("cause"))
    )

    # Prefer the simpler explanation when both passes match.
    return (
        fails.select("adsh")
        .join(F.broadcast(single), "adsh", "left")
        .join(F.broadcast(dims.withColumnsRenamed({"explained_by": "dim_by", "cause": "dim_cause"})), "adsh", "left")
        .select(
            "adsh",
            F.coalesce("explained_by", "dim_by").alias("explained_by"),
            F.coalesce("cause", "dim_cause", F.lit("unexplained")).alias("cause"),
        )
    )
