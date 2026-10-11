"""The same load -> filter -> pivot -> check pipeline in pandas, for an honest
timing comparison with Spark. Mirrors tieouts.run_checks rule for rule.
"""

from __future__ import annotations

import csv

import numpy as np
import pandas as pd

from edgar_mcp.checks import ABS_TOL, DEFAULT_REL_TOL

from . import tieouts as T

NUM_COLS = ["adsh", "tag", "version", "ddate", "qtrs", "uom", "segments", "coreg", "value"]
SUB_COLS = ["adsh", "cik", "name", "sic", "form", "fy", "fp", "filed"]


def read_tsv_quarter(quarter_dir: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    quarter = quarter_dir.rstrip("/").split("/")[-1]
    kw = dict(sep="\t", quoting=csv.QUOTE_NONE, dtype=str, keep_default_na=False, na_values=[""])
    sub = pd.read_csv(f"{quarter_dir}/sub.txt", usecols=SUB_COLS, **kw).assign(quarter=quarter)
    num = pd.read_csv(f"{quarter_dir}/num.txt", usecols=NUM_COLS, **kw)
    num["qtrs"] = num["qtrs"].astype("int32")
    num["value"] = pd.to_numeric(num["value"])
    return sub, num


def read_parquet_lake(lake: str, quarters: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    q = [("quarter", "in", quarters)]
    sub = pd.read_parquet(f"{lake}/sub", columns=SUB_COLS + ["quarter"], filters=q)
    # Same pushdown Spark gets: pyarrow skips row groups that can't match.
    num = pd.read_parquet(
        f"{lake}/num", columns=NUM_COLS,
        filters=[("quarter", "in", quarters), ("qtrs", "==", 0), ("uom", "==", "USD")],
    )
    num["value"] = num["value"].astype("float64")
    return sub, num


def run_checks(sub: pd.DataFrame, num: pd.DataFrame) -> pd.DataFrame:
    filings = sub[sub["form"].isin(T.FORMS)]
    facts = num[(num["qtrs"] == 0) & (num["uom"] == "USD") & num["segments"].isna() & num["coreg"].isna() & num["value"].notna()]
    facts = facts[facts["version"].str.startswith("us-gaap/") & facts["tag"].isin(T.KEY_TAGS)]
    facts = facts.merge(filings, on="adsh")

    bs_date = facts[facts["tag"] == "Assets"].groupby("adsh", as_index=False)["ddate"].max()
    facts = facts.merge(bs_date, on=["adsh", "ddate"])
    # Pivot on the filing key alone, then attach metadata. Pivoting on all the
    # metadata columns would drop any filing with a null key (pandas groupby
    # default) and explode on categorical columns read from Parquet.
    wide = facts.pivot_table(index="adsh", columns="tag", values="value", aggfunc="first")
    bs = filings.merge(bs_date, on="adsh").merge(wide, left_on="adsh", right_index=True)
    for t in T.KEY_TAGS:
        if t not in bs:
            bs[t] = np.nan

    def sum_if_any(series: list[pd.Series]) -> pd.Series:
        """Sum treating NaN as 0, but NaN if every input is NaN."""
        return pd.concat(series, axis=1).sum(axis=1, min_count=1)

    rnci = bs[T.RNCI_TOTAL].fillna(sum_if_any([bs[t] for t in T.RNCI_PARTS]))
    parent = bs[T.TEMP_EQUITY_PARENT]
    parent_plus = sum_if_any([parent, rnci]).where(parent != rnci, parent)
    temp = bs[T.TEMP_EQUITY_TOTAL].fillna(parent_plus).fillna(0)

    a, le, liab = bs["Assets"], bs["LiabilitiesAndStockholdersEquity"], bs["Liabilities"]
    equity = bs["StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"].fillna(
        bs["StockholdersEquity"] + bs["MinorityInterest"].fillna(0)
    )

    def status(reported, computed, present):
        diff = computed - reported
        tol = np.maximum(ABS_TOL, DEFAULT_REL_TOL * np.maximum(computed.abs(), reported.abs()))
        return np.where(~present, "skipped", np.where(diff.abs() <= tol, "pass", "fail")), diff.where(present)

    bs["balance_status"], bs["balance_diff"] = status(le, a, a.notna() & le.notna())
    bs["lpe_status"], bs["lpe_diff"] = status(le, liab + equity + temp, le.notna() & liab.notna() & equity.notna())
    return bs
