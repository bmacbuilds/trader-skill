# Fundamental Screener

Needs S07 (Fundamental Screener) usable.

A screen turns "I want profitable companies that are still growing and not too expensive" into rules a computer can
check, and shows its work: every pass and every fail comes with the number and the threshold.

```bash
traderos --json fundamentals screen --tickers AAPL,AXP,GOOGL,KO,BAC --criteria rules.json
traderos --json fundamentals screen --tickers AAPL,AXP,GOOGL,KO,BAC --criteria rules.json --prices prices.json
traderos --json fundamentals screen --metrics metrics.json --criteria rules.json --top 10
traderos --json fundamentals screen --metrics my-companies.csv --criteria rules.json
traderos --json fundamentals screen --metrics my-dated-companies.csv --criteria rules.json --as-of 2026-10-13
```

`--tickers` reads the companies' SEC filings, so it needs the network and the user's SEC identification
([earnings.md](earnings.md)); without the identification it refuses (NOT_AUTHORIZED, exit 3) before any call.
`--metrics` reads a file and needs neither.

**Point in time.** `--as-of` screens as of a past day, and nothing from after it may be used. A file whose rows
say when their figures were known (a `filed` or `as_of` column, or an `as_of` for the whole file) can be screened
as of any day on or after those dates. A file that does not say, like a plain CSV of numbers, can only be screened
as of today (no `--as-of`): with a past `--as-of` it refuses (LOOKAHEAD_RISK, exit 3), because its figures may be
from after that day. Say so, and ask the user to date the rows or screen as of today. A row dated after the
`--as-of` is refused the same way, and so is a prices file for a past `--as-of` that states no `as_of` day.

## What it reads and writes

- Metrics: from the companies' own SEC filings with `--tickers` (it needs the user's SEC identification, as in
  [earnings.md](earnings.md)), or a file with `--metrics`: the `metrics.json` that `traderos earnings --metrics`
  writes, or any JSON list or CSV with a `ticker` column. CSV numbers may carry `$`, `%` and commas.
- Prices (optional, the user's own file): `{"as_of": "YYYY-MM-DD", "prices": {"AAPL": "341.27"}}`. With prices
  it adds `price`, `pe` (price / trailing EPS, only when EPS is positive) and `earnings_yield`.
- Rules: a JSON file (below).
- Stores a `fundamental-screen/1` record in the local store: who passed, their score and why, who failed and why,
  and who could not be decided.

## The rules file

```json
{"name": "Growing, profitable, not priced for perfection",
 "rules": [{"metric": "revenue_growth_yoy", "op": ">=", "value": 10},
           {"metric": "operating_margin", "op": ">=", "value": 25},
           {"metric": "pe", "op": "<=", "value": 35}],
 "rank":  [{"metric": "revenue_growth_yoy", "order": "desc", "weight": 2},
           {"metric": "pe", "order": "asc", "weight": 1}],
 "missing": "fail"}
```

Ops: `>`, `>=`, `<`, `<=`, `==`, `!=`, `between` ([low, high]), `in` and `not in` (a list, for tickers or sectors to
leave out). Margins and growth are in percent (16.4 means 16.4%); a rule may say its `"unit"`. A rule on a metric a
company lacks never decides its row (banks have no operating margin in SEC's facts): the row is `passed: null`,
flagged, unless another rule failed; `"missing"` only says where that rule is listed. Ranking scores each passing
company 0 to 1 per rank metric and takes the weighted average. A row whose newest quarter is stale, or a quarter
behind the others, is flagged.

## Steps

1. Turn the user's words into rules. Ask when a threshold is vague ("cheap" means what P/E?). Write them to
   `rules.json`.
2. Get metrics for the candidates: `--tickers` reads their filings directly, or run the Earnings Analyst on them with
   `--metrics metrics.json` ([earnings.md](earnings.md)), or use the user's own CSV. Prices from their own file if
   P/E matters.
3. Run the screen:

   ```bash
   traderos --json fundamentals screen --metrics metrics.json --criteria rules.json --prices prices.json
   ```

   If nothing passed, say so and show the closest misses.
4. Report the shortlist with the reasons, the misses with the numbers that failed, the rows that could not be
   decided and why, and the dates the numbers are from (the latest quarter end per company, the price date).
   Suggest the Earnings Analyst on the top names before anything else.

## Example: the reel's five

An example run: metrics from SEC for AAPL, AXP, GOOGL, KO and BAC, prices at the 2026-10-13 close, and the example
rules (the rules above plus `eps_ttm > 0`):

```
Growing, profitable, not priced for perfection: 2 of 5 companies pass.
 1. PASS GOOGL  Alphabet Inc.  score 0.75   (growth 24.2, P/E 17.8)
 2. PASS KO     COCA COLA CO   score 0.25   (growth 12.1, P/E 28.3)
    FAIL AAPL   pe 39.2, needs <= 35
    FAIL BAC    operating_margin is missing
    FAIL AXP    revenue_growth_yoy 8.6, needs >= 10; operating_margin is missing
```

In TraderOS a missing metric never decides a row on its own: BAC, whose only gap is the operating margin, is
`N/A` (`passed: null`, cannot tell) rather than FAIL, and AXP still fails on its growth. Coca-Cola passes with two
flags, STALE_PERIOD and PERIOD_MISMATCH. Then the caveats a careful analyst
adds: Alphabet's latest quarter has net income above operating income (gains outside the business), and Coca-Cola's
numbers stop at the quarter ending 2026-04-03 because SEC's facts lag its latest 10-Q.

## What it never does

- Never calls a passing company a buy. A screen is a filter on reported numbers, not a recommendation.
- Never fills in a missing number or guesses a metric: missing means missing.
- Never sends anything to sizing or an order path by itself.

## Safety

Education and tooling, not financial advice. This only filters numbers. Nothing places an order except a paper
ticket the user approved, after the risk check passes ([paper-orders.md](paper-orders.md)).
