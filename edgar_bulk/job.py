"""Bulk balance-sheet tie-outs over SEC Financial Statement Data Sets.

    # 1. Convert each quarter's TSVs to Parquet once
    python -m edgar_bulk.job ingest data/raw/2026q1 data/raw/2025q4 --lake data/lake

    # 2. Run the checks over any set of ingested quarters
    python -m edgar_bulk.job check --lake data/lake --out data/results
"""

from __future__ import annotations

import argparse
import json
import time
from contextlib import contextmanager
from pathlib import Path

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

from . import tieouts as T


def spark_session(app: str = "edgar-bulk") -> SparkSession:
    spark = (
        SparkSession.builder.master("local[*]")
        .appName(app)
        # Default is 200, sized for clusters. Locally that is 200 tiny tasks per
        # shuffle; one or two per core is plenty.
        .config("spark.sql.shuffle.partitions", "16")
        .config("spark.driver.memory", "6g")
        .config("spark.ui.showConsoleProgress", "false")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("ERROR")
    return spark


@contextmanager
def timed(label: str, timings: dict):
    t = time.perf_counter()
    yield
    timings[label] = round(time.perf_counter() - t, 2)
    print(f"  {label}: {timings[label]}s", flush=True)


def ingest(spark: SparkSession, quarter_dirs: list[str], lake: str) -> dict:
    """TSV -> Parquet, partitioned by quarter. Done once per quarter; every
    later job reads typed, compressed, column-pruned Parquet instead."""
    timings: dict = {}
    for qdir in quarter_dirs:
        quarter = Path(qdir).name
        sub, num, tags = T.load_raw(spark, qdir)
        with timed(f"ingest {quarter}", timings):
            for name, df in (("sub", sub), ("num", num), ("tag", tags)):
                (
                    df.withColumn("quarter", F.lit(quarter))  # no-op for sub, which has it
                    .write.mode("overwrite")
                    # Overwrite only this quarter's partition, keep the others.
                    .option("partitionOverwriteMode", "dynamic")
                    .partitionBy("quarter")
                    .parquet(f"{lake}/{name}")
                )
    return timings


def read_lake(spark: SparkSession, lake: str, quarters: list[str] | None = None):
    def read(name: str) -> DataFrame:
        df = spark.read.parquet(f"{lake}/{name}")
        # A filter on the partition column is pruned at planning time: Spark
        # never opens the other quarters' files.
        return df.filter(F.col("quarter").isin(quarters)) if quarters else df

    # The tag dictionary repeats across quarters; one row per (tag, version).
    return read("sub"), read("num"), read("tag").dropDuplicates(["tag", "version"])


def pipeline(sub: DataFrame, num: DataFrame, tags: DataFrame) -> tuple[DataFrame, DataFrame]:
    # `facts` feeds both the pivot and the failure investigation, so it is the
    # one thing worth caching; without it Spark would rescan `num` for each.
    facts = T.consolidated_instants(num, sub).cache()
    results = T.run_checks(T.balance_sheets(facts)).cache()
    causes = T.explain_failures(results, facts, num, tags)
    # Causes exist only for failures (hundreds of rows): broadcast them.
    return results.join(F.broadcast(causes), "adsh", "left"), facts


def summarize(results: DataFrame) -> dict:
    rows = results.groupBy("quarter", "balance_status", "lpe_status", "cause").count().collect()
    out: dict = {"filings": 0, "balance": {}, "liabilities_plus_equity": {}, "causes": {}}
    for r in rows:
        out["filings"] += r["count"]
        out["balance"][r["balance_status"]] = out["balance"].get(r["balance_status"], 0) + r["count"]
        out["liabilities_plus_equity"][r["lpe_status"]] = out["liabilities_plus_equity"].get(r["lpe_status"], 0) + r["count"]
        if r["cause"]:
            out["causes"][r["cause"]] = out["causes"].get(r["cause"], 0) + r["count"]
    top = (
        results.filter(F.col("explained_by").isNotNull())
        .groupBy("cause", "explained_by").count().orderBy(F.desc("count")).limit(10).collect()
    )
    out["top_explanations"] = [r.asDict() for r in top]
    out["no_liabilities_subtotal"] = results.filter("no_liabilities_subtotal").count()
    return out


def check(spark: SparkSession, lake: str, out: str, quarters: list[str] | None) -> dict:
    timings: dict = {}
    sub, num, tags = read_lake(spark, lake, quarters)
    results, _ = pipeline(sub, num, tags)
    Path(out).mkdir(parents=True, exist_ok=True)
    # The physical plan: look for BroadcastHashJoin (no shuffle of num),
    # PushedFilters and PartitionFilters on the Parquet scans.
    plan = results._jdf.queryExecution().explainString(
        spark._jvm.org.apache.spark.sql.execution.ExplainMode.fromString("formatted")
    )
    Path(out, "plan.txt").write_text(plan)
    with timed("check", timings):
        results.write.mode("overwrite").partitionBy("quarter").parquet(f"{out}/results")
    summary = summarize(spark.read.parquet(f"{out}/results"))
    summary["timings"] = timings
    Path(out, "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    return summary


def main() -> None:
    p = argparse.ArgumentParser()
    sp = p.add_subparsers(dest="cmd", required=True)
    pi = sp.add_parser("ingest")
    pi.add_argument("quarter_dirs", nargs="+")
    pi.add_argument("--lake", default="data/lake")
    pc = sp.add_parser("check")
    pc.add_argument("--lake", default="data/lake")
    pc.add_argument("--out", default="data/results")
    pc.add_argument("--quarters", nargs="*")
    a = p.parse_args()
    spark = spark_session()
    if a.cmd == "ingest":
        print(ingest(spark, a.quarter_dirs, a.lake))
    else:
        print(json.dumps(check(spark, a.lake, a.out, a.quarters), indent=2, default=str))
    spark.stop()


if __name__ == "__main__":
    main()
