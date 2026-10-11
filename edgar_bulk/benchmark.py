"""Time the same check pipeline in pandas and Spark, from TSV and from
Parquet, on one quarter and on several. Each configuration runs in a fresh
process so caches and JVM warm-up don't leak between measurements.

    python -m edgar_bulk.benchmark --raw data/raw --lake data/lake --out reports/benchmark.json
"""

from __future__ import annotations

import argparse
import json
import resource
import subprocess
import sys
import time


def _run_pandas(source: str, quarters: list[str], raw: str, lake: str) -> dict:
    import pandas as pd

    from . import pandas_baseline as P

    t0 = time.perf_counter()
    if source == "tsv":
        parts = [P.read_tsv_quarter(f"{raw}/{q}") for q in quarters]
        sub = pd.concat([p[0] for p in parts])
        num = pd.concat([p[1] for p in parts])
    else:
        sub, num = P.read_parquet_lake(lake, quarters)
    t_read = time.perf_counter() - t0
    res = P.run_checks(sub, num)
    total = time.perf_counter() - t0
    peak_gb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**3  # bytes on macOS
    return {
        "seconds": round(total, 2), "read_seconds": round(t_read, 2), "peak_rss_gb": round(peak_gb, 2),
        "statuses": res.groupby(["balance_status", "lpe_status"]).size().to_dict(),
    }


def _run_spark(source: str, quarters: list[str], raw: str, lake: str) -> dict:
    from pyspark.sql import functions as F

    from . import tieouts as T
    from .job import read_lake, spark_session

    t0 = time.perf_counter()
    spark = spark_session("benchmark")
    t_start = time.perf_counter() - t0
    t1 = time.perf_counter()
    if source == "tsv":
        loaded = [T.load_raw(spark, f"{raw}/{q}") for q in quarters]
        sub = loaded[0][0]
        num = loaded[0][1]
        for s, n, _ in loaded[1:]:
            sub, num = sub.unionByName(s), num.unionByName(n)
    else:
        sub, num, _ = read_lake(spark, lake, quarters)
    res = T.run_checks(T.balance_sheets(T.consolidated_instants(num, sub)))
    # collect() is the action: everything above was only a plan until here.
    rows = res.groupBy("balance_status", "lpe_status").count().collect()
    job = time.perf_counter() - t1
    spark.stop()
    return {
        "seconds": round(job, 2), "session_startup_seconds": round(t_start, 2),
        "statuses": {(r["balance_status"], r["lpe_status"]): r["count"] for r in rows},
    }


def _child(engine: str, source: str, quarters: str, raw: str, lake: str) -> None:
    fn = _run_pandas if engine == "pandas" else _run_spark
    out = fn(source, quarters.split(","), raw, lake)
    out["statuses"] = {f"{k[0]}/{k[1]}": int(v) for k, v in out["statuses"].items()}
    print("RESULT " + json.dumps(out))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--raw", default="data/raw")
    p.add_argument("--lake", default="data/lake")
    p.add_argument("--out", default="reports/benchmark.json")
    p.add_argument("--quarter-sets", nargs="+", default=["2026q1", "2025q4,2026q1,2026q2,2026q3"])
    p.add_argument("--child", nargs=5, help=argparse.SUPPRESS)
    a = p.parse_args()
    if a.child:
        _child(*a.child)
        return

    results = []
    for qs in a.quarter_sets:
        for source in ("tsv", "parquet"):
            for engine in ("pandas", "spark"):
                proc = subprocess.run(
                    [sys.executable, "-m", "edgar_bulk.benchmark", "--child", engine, source, qs, a.raw, a.lake],
                    capture_output=True, text=True,
                )
                line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
                if line is None:
                    print(proc.stderr[-2000:])
                    raise SystemExit(f"{engine}/{source}/{qs} failed")
                r = {"engine": engine, "source": source, "quarters": len(qs.split(",")), **json.loads(line[7:])}
                results.append(r)
                print(f"{r['quarters']}q {source:7} {engine:6} {r['seconds']:7}s", flush=True)

    # Both engines must agree, or the timing comparison is meaningless.
    for qs in a.quarter_sets:
        n = len(qs.split(","))
        statuses = {json.dumps(r["statuses"], sort_keys=True) for r in results if r["quarters"] == n}
        print(f"{n} quarter(s): engines agree on every status count: {len(statuses) == 1}")

    with open(a.out, "w") as f:
        json.dump(results, f, indent=2)


if __name__ == "__main__":
    main()
