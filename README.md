# EDGAR MCP

[![tests](https://github.com/tlevin1/edgar-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/tlevin1/edgar-mcp/actions/workflows/tests.yml)

A [FastMCP](https://gofastmcp.com) server that gives Claude Code (or any MCP
client) direct access to SEC EDGAR financial data, plus deterministic checks
that a filing's statements actually tie out.

The idea: when I ask Claude about a company's financials, I want the numbers
pulled from the filing itself with a citation, not recalled from training data,
and I want the arithmetic done in code. The LLM decides what to look up and
explains what it means; it never produces a number on its own.

The server checks one company at a time. [`edgar_bulk/`](#bulk-every-filer-with-pyspark)
runs the same balance-sheet checks over **every 10-K and 10-Q filed in a year**
with PySpark: 22,162 filings in about 11 seconds on a laptop.

```
> Does Tesla's latest 10-K tie out? What was free cash flow?

  check_tieouts(ticker="TSLA")      → 5/5 pass, with formulas and inputs
  get_financials(ticker="TSLA")     → FCF = operating cash flow - capex,
                                      each value tagged with XBRL concept,
                                      period and accession number
```

## Tools

| Tool | What it does |
|---|---|
| `lookup_company` | Ticker or name → CIK and SEC name. |
| `list_filings` | A company's 10-K / 10-Q filings that carry XBRL data, newest first. |
| `get_financials` | Core income statement, balance sheet and cash-flow line items for recent filings. Free cash flow is derived in code and labelled as derived. Missing items are `null`, never estimated. |
| `get_concept` | Any raw XBRL concept (e.g. `ResearchAndDevelopmentExpense`). An unknown name returns the closest concepts this company has actually reported, so the model can correct itself instead of guessing. |
| `check_tieouts` | Five internal-consistency checks on one filing (below). |

All tools are annotated read-only.

## Tie-out checks

| Check | Identity |
|---|---|
| `balance_sheet_balances` | Assets = Liabilities and Stockholders' Equity |
| `liabilities_plus_equity` | Liabilities + total equity (+ noncontrolling and temporary equity where tagged) = Liabilities and Stockholders' Equity |
| `gross_profit` | Revenue − Cost of revenue = Gross profit |
| `cash_flow_sections_sum` | CFO + CFI + CFF + FX effect = Change in cash |
| `cash_roll_forward` | Ending cash − Beginning cash = Change in cash |

Each result gives `pass` / `fail` / `skipped`, the formula, every input fact
(concept, value, period, accession), the difference and the tolerance used.

Design rules:

- **One filing at a time.** Every number comes from the same accession number,
  so a check compares what the company filed in that document, not a mix of
  original and later restated values.
- **Like for like periods.** A 10-Q tags both the quarter and the year to date.
  Every term in a duration check shares the same start date. Mixing them is the
  easiest way to get a false break.
- **Skipped, never assumed.** If an input isn't tagged, the check is skipped
  and says which concept was missing. Several big filers (Amazon, Walmart,
  Coca-Cola) never tag a `Liabilities` subtotal, and most don't tag
  `GrossProfit`. Those are tagging gaps, and the output says so.
- **Tolerance:** the larger of 0.05% of the amount and $1M. That absorbs rounding
  to millions and still catches real breaks; `rel_tolerance` is a parameter.
- **Tag variants are handled explicitly.** Companies tag the same line item
  differently (e.g. Nvidia and Amazon report capex as
  `PaymentsToAcquireProductiveAssets`; Microsoft and Tesla use the
  `...IncludingDisposalGroupAndDiscontinuedOperations` cash tags). Each line item
  has an ordered list of fallbacks in [`facts.py`](edgar_mcp/facts.py) and
  [`checks.py`](edgar_mcp/checks.py), and the output always names the tag that
  was used.

### Validation against real filings

Latest 10-K and 10-Q for AAPL, MSFT, NVDA, TSLA, JPM, AMZN, GOOGL, META, KO,
WMT, BRK-B and COST (24 filings): **zero failures**. Every non-pass is a skip
with a correct reason, for example banks have no gross profit.

The first run did fail Microsoft's cash-flow check by exactly $196M. The cause
was an FX-effect tag variant my fallbacks didn't cover yet. That is what the
check is for: a break is either a tagging gap to fix in code or something in the
filing worth reading.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt

# SEC requires a User-Agent with a contact address on every request
export EDGAR_USER_AGENT="Your Name you@example.com"
```

### Use it in Claude Code

`.mcp.json` registers the server for this folder: open Claude Code here and
approve the `edgar` server when prompted. To use it from any project:

```bash
claude mcp add edgar --scope user \
  -e EDGAR_USER_AGENT="Your Name you@example.com" \
  -- "$(pwd)/.venv/bin/python" -m edgar_mcp
```

Run it standalone with `.venv/bin/python -m edgar_mcp` (stdio), or inspect it
with `.venv/bin/fastmcp dev edgar_mcp/server.py`.

## Bulk: every filer, with PySpark

The MCP server checks one company. `edgar_bulk/` rebuilds the balance-sheet
checks to run over every filer at once, using SEC's quarterly
[Financial Statement Data Sets](https://www.sec.gov/data-research/sec-markets-data/financial-statement-data-sets):
`sub.txt` (one row per filing) and `num.txt` (one row per reported value,
3.7M rows a quarter).

```bash
.venv/bin/pip install -r requirements-bulk.txt     # needs Java 17+
.venv/bin/python -m edgar_bulk.job ingest data/raw/2025q4 data/raw/2026q1 data/raw/2026q2 data/raw/2026q3
.venv/bin/python -m edgar_bulk.job check            # -> data/results/{results/, summary.json, plan.txt}
.venv/bin/python -m edgar_bulk.benchmark            # pandas vs Spark -> reports/benchmark.json
```

### Results: 2025 Q4 to 2026 Q3

| | |
|---|---|
| Filings checked (10-K and 10-Q) | **22,162** from 5,986 companies |
| Assets = Liabilities and Equity | 21,671 pass, **0 fail**, 491 skipped (a tag missing) |
| Liabilities + Equity components = total | 18,141 pass, **764 fail**, 3,257 skipped |
| …of which no `Liabilities` subtotal tagged | 2,319 (Amazon/Walmart pattern: a tagging gap, not a break) |
| Failures attributed to a specific cause | **720 of 764 (94%)** |

The first identity never fails, because filing software validates it. The
second one is where the data is messy. Each failure is investigated
automatically by searching the same filing for what fills the gap:

| Cause | Failures | What it means |
|---|---|---|
| Redeemable ("mezzanine") equity tagged only by share class | 476 | The line exists but only as a breakdown, never as a company-wide total, so a consolidated-only filter never sees it. 63% are SPACs (redeemable Class A shares). |
| Mezzanine equity under a tag outside the identity | 114 | e.g. `TemporaryEquityValueExcludingAdditionalPaidInCapital`. The same kind of tag variant as Microsoft's $196M FX tag in the single-company checks. |
| Filer's `Liabilities` subtotal omits a liability line | 108 (48 companies) | The gap equals `AccountsPayableCurrent`, `AccruedLiabilitiesCurrent`, notes payable… Mostly genuine tagging errors, usually small. Not always: utilities like Tucson Electric Power group long-term debt with equity under "Capitalization", so their subtotal excludes it by design. |
| Other single tag | 22 | |
| Unexplained | 44 | Includes BlackRock in all four quarters (below). |

How the investigation works, and the data findings that shaped it:

- **Only credit-balance tags can explain a gap** on the liabilities-and-equity
  side. `tag.txt` records each tag's balance type (`crdr`). Before this filter,
  60 SPACs "matched" their trust account, an asset that merely equals their
  redeemable shares.
- **Same line, two tags.** KKR tags one $2.71B mezzanine line under both
  `TemporaryEquityCarryingAmountAttributableToParent` and
  `RedeemableNoncontrollingInterestEquityCarryingAmount`. Across the quarter,
  when those two values differ, adding them ties out every time (12/12); when
  they are equal, counting once does (4 of 4 with complete data). The rule follows the data.
- **Same line, two members.** SPACs often tag one amount under two members of
  the same axis (`CommonClassA` and `…SharesSubjectToRedemption`), so the
  breakdown search also tries the sum of distinct values.
- **Never sum across axes.** BlackRock's gap ($5.4B at year end) is two pieces
  on different axes: share class "SubCo" ($2.79B) and consolidated investment
  funds ($2.64B). Members of different axes can overlap, so summing across
  them could double count. It stays unexplained, and was confirmed by hand.
- **`sub.period` is rounded to month end.** Apple's 2025-09-27 year end appears
  as 2025-09-30. Joining on it silently drops every 52/53-week filer, so each
  filing's balance-sheet date is taken from its own `num` rows instead.

### Design decisions

- **Explicit schemas.** `inferSchema` reads the file once to guess types and
  again to load it, and guesses wrong (zip codes, accession numbers). Values are
  `Decimal(28,4)`, exact like the source, because the check compares sums.
- **Broadcast joins.** `num` (millions of rows) is joined to the 10-K/10-Q list
  (thousands) and the tag dictionary with `F.broadcast`, so the big side is
  never shuffled. The physical plan (`data/results/plan.txt`) has no
  `SortMergeJoin`; adding explicit hints for the aggregated small tables removed
  4 of them and cut shuffles from 11 to 9. Run time didn't change: at this
  size the shuffles weren't the bottleneck.
- **Parquet once, partitioned by quarter.** 2.5 GB of `num.txt` becomes 233 MB of
  Parquet. Jobs pick quarters, so that's the partition; partitioning by tag
  (90k values) would create tens of thousands of tiny files. The plan shows
  `PushedFilters` on the scan: `qtrs = 0`, `uom = USD`, `segments IS NULL` are
  evaluated inside the Parquet reader, which skips row groups by their statistics.
- **Cache only what's reused.** `facts` feeds both the pivot and the failure
  investigation, so it is cached; nothing else is.
- **Shuffle partitions 200 → 16.** The default is sized for clusters; locally it
  means hundreds of near-empty tasks.
- **Explicit pivot values.** Passing the tag list to `pivot()` saves a pass over
  the data to discover them.

### Spark vs pandas, measured

Same pipeline (load, filter, pivot, both checks) in each engine, each run in a
fresh process. Both produce **identical status counts** on every configuration.

| Data | Source | pandas | Spark |
|---|---|---|---|
| 1 quarter | TSV | 5.7 s | 6.7 s |
| 1 quarter | Parquet | **1.0 s** | 5.6 s |
| 4 quarters | TSV | 21.9 s | **13.0 s** |
| 4 quarters | Parquet | **4.4 s** | 6.7 s |

Spark times exclude 3 to 5 s of JVM and session startup. pandas peaked at 2.5 GB
of RAM on four quarters.

- **Format beats engine.** pandas goes from 21.9 s to 4.4 s just by reading
  Parquet; 90% of its TSV time is parsing text.
- **At this size, pandas on Parquet wins.** Spark's fixed planning and
  scheduling overhead (~5 s) dominates a 22k-filing job.
- **Spark wins on raw text at four quarters** because it parses on all 8 cores;
  pandas parses on one. The crossover is visible between one and four quarters.
- **Where Spark actually pays off:** pandas must hold everything in memory.
  SEC has published ~70 quarters; the full history in TSV is far beyond a 16 GB
  laptop, and Spark streams it in partitions and moves to a cluster unchanged.

## Tests

```bash
.venv/bin/python -m pytest -q     # 29 tests, offline (the 10 bulk tests need Java)
```

Tests use small synthetic payloads shaped exactly like EDGAR's `companyfacts`
JSON, and drive the tools through a real FastMCP client against an in-memory
server. They cover a clean filing passing, a deliberate $10M break failing at
real scale, missing tags being skipped rather than passed, noncontrolling
interest, missing FX effect, alternate cash tags, quarter vs. year-to-date
period matching, and tool errors (unknown ticker, unknown accession, misspelled
concept).

`tests/test_bulk.py` builds a tiny Financial Statement Data Set by hand, one
filing per case found in the real data: a clean filing with segment and
co-registrant rows that must be ignored, a 52-week year, a missing `Liabilities`
subtotal, a line tagged twice, a SPAC's class-only redeemable shares, a custom
mezzanine tag, an asset that coincidentally equals the gap, BlackRock's two-axis
case, and a 20-F that must be excluded. It also checks the pandas baseline
agrees with Spark filing by filing.

## Limitations

- US GAAP in USD only. Foreign filers reporting under IFRS (20-F) mostly come back empty.
- Five identities, not a full audit. Natural next checks: segment revenue sums to
  consolidated revenue, EPS × diluted shares ≈ net income, and comparing a value
  as originally filed with how the next year's filing restated it.
- Bulk checks cover the two balance-sheet identities only; the cash-flow and
  gross-profit checks need duration facts and period matching across `qtrs`.
- Data is cached in memory for an hour per process. EDGAR's companyfacts can lag
  a new filing by a day or so.
