# EDGAR MCP

A [FastMCP](https://gofastmcp.com) server that gives Claude Code (or any MCP
client) direct access to SEC EDGAR financial data, plus deterministic checks
that a filing's statements actually tie out.

The idea: when I ask Claude about a company's financials, I want the numbers
pulled from the filing itself with a citation, not recalled from training data,
and I want the arithmetic done in code. The LLM decides what to look up and
explains what it means; it never produces a number on its own.

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

## Tests

```bash
.venv/bin/python -m pytest -q     # 19 tests, offline
```

Tests use small synthetic payloads shaped exactly like EDGAR's `companyfacts`
JSON, and drive the tools through a real FastMCP client against an in-memory
server. They cover a clean filing passing, a deliberate $10M break failing at
real scale, missing tags being skipped rather than passed, noncontrolling
interest, missing FX effect, alternate cash tags, quarter vs. year-to-date
period matching, and tool errors (unknown ticker, unknown accession, misspelled
concept).

## Limitations

- US GAAP in USD only. Foreign filers reporting under IFRS (20-F) mostly come back empty.
- Five identities, not a full audit. Natural next checks: segment revenue sums to
  consolidated revenue, EPS × diluted shares ≈ net income, and comparing a value
  as originally filed with how the next year's filing restated it.
- Data is cached in memory for an hour per process. EDGAR's companyfacts can lag
  a new filing by a day or so.
