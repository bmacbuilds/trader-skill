# ADR-011: the upstream SEC tools are S04, S06 and S07, fetched through our SEC adapter (contracts v1.3, additive)

- **Status:** proposed, 2026-11-20, for part C (the SEC build on `claude/traderos-sec`). Revised the same day for
  OSDev6's review of #11 ("changes needed at 6375c1b"): every item is fixed, and each change it asked to name is
  named below (see "OSDev6's review of #11").
- **Owner rule (11-19, posted by OSDev1 on the wall on 2026-11-20 at 07:14 and 07:22 UTC):** "we're not going to
  rewrite any of the code just for the sake of rewriting it. If the code is perfect and it works, leave it as it is."
  "Python files and all code only gets rewritten if there is clearly a flaw or if there is clearly a better method."
- **Spec:** §12.1 S04, S06, S07; §03.3 (reuse workflow); §03.4 FIN-001 and SYS-001; §04.2, §04.3, §06.1(d), §09.4,
  QT01.
- **Upstream review applied:** `upstream_pr_review.md` v3 (OSDev9): PR #24 TAKE WITH CHANGES (change 1), PR #49
  TAKE AS IS.

## Decision

The logic of S04, S06 and S07 is upstream's, started from its files and changed only where a change is named below:

| TraderOS module | The upstream file (tank, MIT) | Upstream |
|---|---|---|
| `traderos.fundamentals.facts` | `tank/day-trader-skill/trader-analyst/facts.py` (v1's copy differs in one docstring line) | day-trader-skill `3d0f4ef`, with PR #24 `ef390ed` and PR #49 `77c720b` |
| `traderos.fundamentals.edgar13f` (+ `filers.json`, `share_classes.json`) | `tank/trader-skill/trader-copycat/` | trader-skill `5189e79` |
| `traderos.fundamentals.screen` | `tank/trader-skill/trader-screener/screen.py` | trader-skill `5189e79` |

Its rules, thresholds, wording and structure are kept: the 80-to-100-day quarter, the latest filing winning while the
first filing labels, Q4 as the year minus three quarters, the concept lists, the exhibit ranking, the 13F name
matching and share-class table, the CUSIP aggregation and class combining, the copy weights, the screener's
operators and its 0-to-1 ranking, and its text reports. Each module's docstring names its tank path and commit and
lists every change with its reason; the MIT notice ships beside them (`src/traderos/fundamentals/NOTICE`).

Our first SEC build's modules are **retired**:
- `earnings.py`: it switched concepts by taking only the first one with facts (so a company that changed revenue
  concepts lost quarters); it had no bank revenue concept (`RevenuesNetOfInterestExpense`); it had no form filter (an
  8-K's revenue of 1 was read as a quarter); and its EPS for a derived Q4 was always unavailable, so `eps_ttm` never
  was.
- `thirteenf.py`: it picked a 13F-HR/A as the latest filing, and it did not aggregate by CUSIP (Apple came out as 12
  rows for Berkshire).
- `screener.py`: it read from `earnings.py`; the upstream screener adds operators, ranking and prices.

**All fetching goes through `traderos.adapters.sec`, which is kept.** The upstream two Fetchers (facts.py:92-122,
edgar13f.py:100-174) have no cache and no size cap; spec 04.3 requires all internet access through a registered
adapter, and 06.1(d) requires caching.

## What changed in `facts.py`

| Change | Reason |
|---|---|
| Only facts filed on or before `as_of` are read; so are the filing links | Flaw (QT01): a restatement filed later changed an earlier day's answer |
| Decimal | Flaw (04.3): `4.50 - (1.00 + 1.10 + 1.20)` was `1.2000000000000002` |
| USD (USD/shares for EPS) only; another currency is refused by name (`OUTSIDE_CAPABILITY`, or a `CURRENCY` limitation for one series) | Flaw: a company reporting in CNY was printed as "US$ millions" |
| Each value carries `first_filed` | FIN-001 "original availability": upstream computed it in `periods()` and dropped it |
| Each value carries `amendment` (10-K/A, 10-Q/A) | §12.1 S04 asks for amendments |
| **PR #24 + review change (1):** a row is `derived` when any series shown in it was (revenue, cost of revenue, gross profit, operating income, net income, diluted EPS); the row names them; trailing totals and the screener's metrics row name a derived quarter they include | Flaw: a derived Q4 gross profit of 65 showed as reported |
| `margin()` is None when the denominator is not positive | Flaw: `margin(-50, -100)` was +50% |
| Trailing twelve months use the same four quarter ends for every series, ending at the newest quarter | Flaw: revenue and EPS could cover different years |
| A three-month value first filed in a 10-K is Q4; a period first seen as a comparative is placed by its own end date | Flaw: "FY 2026" on a quarter, and last year's Q2 labelled "Q2 2026" beside this year's |
| A row's `filed` is the latest filing among its numbers | Flaw (QT01): upstream used the revenue fact's date, though a restated net income was public only later |
| A fact with no readable filing date is skipped | QT01: it cannot be known to have been public |
| A fact whose value is not a finite number, or whose period dates do not read, is skipped (the period then shows as not filed) | Flaw: a NaN entered every sum it touched, and one unreadable date stopped the whole report with a traceback |
| A malformed row of the filings list is skipped; an exhibit lookup that fails in any way falls back to the filing index | QT01 (its filing date cannot be read, so it is never known to be public); the fallback is upstream's, which it applied only to its fetch error |
| The text's first line ends ", filed by <as_of>" | QT01: a past day's report must not read as today's |
| `metrics_row()` also carries `filed` and `as_of` | The screener needs both to refuse figures from after a screen's day (screen.py change 4) |
| A fact with no `fy` takes its period end's calendar year in the record | `earnings/1`'s `fiscal_year` is a required whole number; upstream printed it blank |
| A record for a past `as_of` found by ticker says so (`TICKERS_TODAY`) | Flaw: the ticker list is today's SEC list; a ticker since moved to another company would point to the new one, silently |
| The upstream format strings are kept; only its divisors `1e6` and `1e9` are the integers `10**6` and `10**9` | 04.3: a Decimal cannot be divided by a float. (The first port rewrote them as f-strings for no reason; reverted.) |
| Its `main()`, `Fetcher` and `user_agent()` are replaced by `traderos earnings` and the adapter; its `--metrics FILE` is kept | §04.3; the CLI contract |

Kept from upstream on purpose: with no exhibit found, the 8-K line links the filing index (the first port linked the
8-K's primary document instead: reverted). The earnings record's `filed` is the latest filing that reported a value
(a restatement wins); the first day a value could have been known is `first_filed` (the contract's docstring says so).

**PR #49 lands in `traderos.cli.main`**, as is (its eight lines and comment): `traderos earnings` now prints what its
`main()` printed, and every other command benefits. The guard keeps `redirect_stdout(StringIO)` working.

## What changed in `edgar13f.py`

| Change | Reason |
|---|---|
| Only filings made on or before `as_of`, amendments included | Flaw (QT01): no way to ask what was public on a day |
| A DOCTYPE or ENTITY is refused before parsing (table and cover page), and by the XML parser itself, in whatever encoding the document is in | Flaw (04.2): an internal entity's text became an issuer name. The byte search alone missed a UTF-16 document (review M2) |
| Decimal; a row with no value or share count is refused | 04.3: `0.1 + 0.2` dollars was `0.30000000000000004`; 09.4: a missing value was read as 0 |
| Fetching and the EDGAR filer search through the adapter (`efts.sec.gov` added to its hosts) | 04.3, 06.1(d) |
| OpenFIGI (`--figi`) not ported; `share_classes.json` and your own map (`--map FILE`) kept; messages point at the map | OpenFIGI is not an approved source (06.1) |
| Amendments are applied: RESTATEMENT replaces the table, NEW HOLDINGS adds its rows; any other type is not applied and says so; a filing asked for by accession is read as filed | Flaw: upstream listed amendments and asked the reader to open each |
| The holdings are dated by the latest amendment applied (`filed`, "made public"); the text names the original's own date beside its accession | QT01: the amended holdings were not public before the amendment (review M1) |
| An amendment that cannot be read or is refused (a DOCTYPE in it) is not applied, with a note saying why | One bad amendment refused holdings, diff and copy for the whole quarter (review LOW 1). The original filing still fails closed |
| `copy` carries each weight unrounded: its scaling of the 2-decimal `pct_of_13f` is kept, and only the text rounds. A position whose weight scales to 0 is left out with a line naming it and its true weight (its value's share of the copy) | Flaw: a position under 0.005% of the table got weight 0.00, which `copy-targets/1` cannot hold (a weight is above zero), so `holdings 1067983 --copy` refused Berkshire's Q2 2026 on D.R. Horton (true weight 0.00019%) (review M3) |
| A CIK has at most 10 digits | 04.2: upstream zero-padded an 11-digit number into a wrong URL |
| Your CUSIP map is at most 1 MB of JSON whose tickers are text, and it is only the file you name (`--map`); `trader/tickers.json` is not read by default | 04.2: upstream stopped with a traceback on a malformed map or a ticker that is not text; 06: its default made the same command give other tickers depending on the folder it ran in |
| Every 13F record says its tickers come from today's SEC list and your map (`TICKERS_TODAY`) | Flaw: a company renamed, merged or delisted since the quarter end may show today's ticker, another company's, or none (review LOW 7) |
| `--find` or `--filings` with no hit is an answer: its message, exit 0 | The CLI contract's exit codes are 0, 2, 3 and 4; the upstream 1 is not one |
| Typed records: `holdings/1` (Holding gains ticker, pct_of_13f, share classes), `holdings-diff/1`, `copy-targets/1` | §12.1 S06; contracts |
| `copy` gives HYPOTHETICAL weights (`safe_for` research and hypothetical planning only); it writes no targets file, and the hand-off line to a budget skill is gone | F1 has no order path; nothing sizes from it |

Kept from upstream on purpose: the copy scales the 2-decimal `pct_of_13f` (its documented method: the numbers you see in
the holdings table), and `--min-pct` compares that same figure. Scaling exact values instead would move its own
reference numbers on its fixture (top five: KO 15.11 to 15.12, BAC 12.80 to 12.81, AXP 23.9 to 23.8 at one
decimal), and nothing is wrong with them; only a weight of zero is. The holdings text shows every position unless
`--top` is given, as its did (the first port showed 20, at most 200: reverted). The first port's 20,000-row cap on an
information table is removed: the adapter's 60 MB cap already bounds the table, and the row cap would have refused a
large filer for no added safety.

Data files: `filers.json` and `share_classes.json` are upstream's, with two note sentences changed: the share-class note
keeps "Tickers use a dot for the class (BRK.B)" and drops its example of an outside trading service (F1 has no order
path), and the filers note names `traderos holdings --find` (its command does not exist here). The CUSIP table and
the filer rows are unchanged.

## What changed in `screen.py`

| Change | Reason |
|---|---|
| `to_number` refuses NaN, infinity and true/false | Flaw (04.2): "inf" passed `pe >= 10`; `true` passed `> 0` |
| A rule on a missing metric never decides a row: `passed: null` unless another rule failed; `missing` only says whether the reason is listed under fails or skipped | Flaw (09.4): with "skip" BAC passed on a rule never checked; with "fail" it failed on a number nobody had |
| Units per metric (percent, USD, USD/shares, ratio); a column mixing `%` and bare numbers, or a rule in another unit, is refused | Flaw: "25%" read as 25 and "0.25" as 0.25 in one column |
| The screen has an `as_of`, and nothing from after it is used: a file, prices or a row dated after it are refused (`LOOKAHEAD_RISK`), and a row's `filed`, its own `as_of` and its `period_end` are each checked. STALE_PERIOD (over 182 days old) and PERIOD_MISMATCH (45 days behind the newest) are flagged | QT01; §12.1 S07. Only `filed` was checked: the upstream metrics file without its `as_of`, screened at 2025-06-01, passed AAPL, BAC and GOOGL on quarters ending 2026-06-27 and 2026-06-30 (review H1) |
| Where the input cannot say when a row's figures were known (no `filed`, no `as_of` of its own or of its file), a screen dated before today refuses (`LOOKAHEAD_RISK`) rather than guess; so do undated prices. Screened as of today, the same file is used: what is in it now was known now | QT01: a guess is how the look-ahead above got in (review H1) |
| A metrics file keeps the `as_of` of the data it was built from: each row carries its own (a row already in the file takes the file's), the file's is the latest of them and of its own, and a later `earnings --metrics` never lowers it. A file with a row it cannot date states no `as_of` | Flaw: the merge set the file's `as_of` to the newest row's and dropped the file's own, so `earnings AAPL --as-of 2025-06-01 --metrics FILE` dated the upstream 2026-10-13 file 2025-06-01, and a 2025-06-01 screen passed BAC and GOOGL on 2026-06-30 quarters (review H1) |
| Text where a number belongs never decides a row: in a numeric metric ("#DIV/0!" in `net_margin`, or text in a column of numbers) the rule counts as missing (change 2). Text metrics (sector) keep its string comparisons | Flaw (09.4): "#DIV/0!" passed `!= 0` as a string (review LOW 2) |
| Untrusted files are refused where the upstream code crashed or misread them: a JSON key given twice, companies that are not objects, a rule or rank entry that is not an object, a rank that is not a list, a weight that is not a number of zero or more, a `top` that is not a whole number of 1 or more, a `name` that is not text, a comparison value that is not a number | 04.2: each one stopped its screen with a traceback, or was misread (a duplicate key kept the last value silently; a negative weight inverted a rank; `top: -1` dropped the last pass) |
| The closing line names `traderos earnings TICKER`, not its /trader-analyst and /trader-budget skills | Those skills do not exist here; F1 has no budget or order path |
| Decimal | 04.3 |
| Input: the ported facts records (`--tickers`) or a capped file (`--metrics`, 1 MB, 500 companies); output: `fundamental-screen/1`; refusals exit 3 | Contracts and the CLI contract |

Percent is the upstream convention and stays: `16.4` means 16.4%. Its rules files work unchanged.

## The SEC adapter: kept, with three fixes and one rule of upstream's

| Fix | Evidence |
|---|---|
| The size cap holds after decompression | A 128 KB gzip expanded to 132,120,576 bytes past the 60 MB cap |
| Share-class tickers are normalised (BRK.B = BRK-B = BRK/B) | `cik_for("BRK.B")` was MISSING_DATA |
| `efts.sec.gov` is an SEC host (the filer-name search) | the search was refused as "not an SEC address" |
| The help text's placeholder identification (`you@example.com`) is refused, as the upstream `user_agent()` did | facts.py:87, edgar13f.py:95 |
| A redirect is followed only to another SEC address (HTTPS, an SEC host, no port or user name) | `urlopen` followed any redirect, carrying the person's identification to whatever host the answer named; the host check saw only the first URL (review LOW 5) |

## Contracts v1.3 (additive)

Every new field has a default, so v1.2 JSON still parses; `docs/contracts-v1.md` lists them.
- `earnings/1`: QuarterLine gains gross profit, operating income, the three margins, the three growth figures (unit
  `percent`), `period_start`, `first_filed`, `form`, `derived_series`, `amended_series`; FilingFact gains
  `first_filed`; EarningsReport gains `ttm`, `ttm_quarter_ends`, `ttm_derived`, `concepts`, `changes`,
  `latest_report` and `earnings_release` (new part `FilingLink`).
- `holdings/1`: Holding gains `name`, `ticker`, `ticker_source`, `ticker_check`, `issuer_cik`, `pct_of_13f`, `cusips`,
  `tickers`, `classes`, `rows`; HoldingsReport gains `combined`, `cover_total_usd`, `amendments_applied`, `notes`.
- `fundamental-screen/1`: ScreenRow gains `labels`, `passes`, `skipped`, `score`, `rank_parts`, `rank_gaps`, `flags`,
  `period_end`, `filed`; FundamentalScreen gains `name`, `prices_as_of`, `top`.
- New: `holdings-diff/1` (HoldingsDiff + HoldingChange) and `copy-targets/1` (CopyTargets + TargetWeight +
  SkippedPosition). CopyTargets refuses any `safe_for` other than research and hypothetical planning.
- **Compatibility, stated plainly:** canonical form lists every field, so a v1.2 record's stored hash does not verify
  under v1.3 (`tests/unit/test_sec.py::test_a_v1_2_record_still_parses` pins this). No release stored one: S04, S06
  and S07 are experimental on an unmerged branch. The pre-port goldens are kept in
  `tests/fixtures/contracts/compat-v1.2/`; the golden set is regenerated.

## CLI (additive; `docs/cli-contract-v1.md`)

- `traderos earnings TICKER... [--cik C] [--as-of D] [--quarters N] [--no-filings] [--metrics FILE]`: several tickers
  return a list of records.
- `traderos holdings FILER [--as-of D] [--top N] [--combine] [--period D] [--accession A] [--map FILE]
  [--diff | --copy [--min-pct X] [--exclude T...] [--separate]]`, `holdings FILER --filings`, `holdings --find NAME
  [--sec]`.
- `traderos fundamentals screen (--tickers A,B | --metrics FILE) --criteria FILE [--prices FILE] [--as-of D]
  [--top N]`; `--rules` is the same as `--criteria`. The criteria file is the upstream rules format. The pre-port
  `{"min": ..., "max": ...}` format is refused with a pointer: its metric names and fraction units do not match.

Not ported from the upstream command lines, beyond those above: `--json` (TraderOS's global `--json`), `--user-agent`,
`--config` and `--offline` (the identification is `traderos config set sec_user_agent` or `SEC_USER_AGENT`, kept in one
place; the adapter's cache and the tests' offline transport replace `--offline`), `--tickers FILE` and
`--no-tickers` (the SEC's ticker list comes through the adapter and its cache; when it cannot be read, ticker matching
is skipped with a note), `--thousands` and `--prior-xml` (they go with `--xml`), `--dry-run` and `--out` (the copy
writes no file).

## Evidence

- The upstream project's own tests are ported with its inputs and expected numbers (analyst 13 + PR #24's 1, copycat 24, screener 10),
  on its fixtures, copied byte for byte with `NOTICE` and a `PROVENANCE.csv` the tests re-hash. Each test's
  changes are listed at the top of its file (Decimal; its command lines become `traderos` commands; BAC's missing
  margin is `null`; OpenFIGI tests become "never calls OpenFIGI").
- The review's cases for PR #24 (four) and PR #49 (an in-process run, a cp1252 run, an error path under cp1252), and
  a regression test for every flaw above.
- `tests/unit/test_analyst.py` 33, `test_copycat.py` 34, `test_screener.py` 30, `test_sec.py` 38, and
  `test_sec_review11.py` 49 (OSDev6's review of #11: his repros, and a test for every item).
- **Mutants: `tools/mutants/sec.py`**, committed so anyone can rerun them (`python tools/mutants/sec.py [PYTHON]`):
  77 mutants, each run with `python -B` and `__pycache__` deleted. 33 are the port's named changes and the upstream kept
  rules (the first list of 54, run before the review, was not committed, and is written again from the tables
  above); 41 are the review's fixes, one or more each; 3 are OSDev6's survivors (`edgar13f.py:142` `>` to `>=`,
  `:145`, and `facts.py:116`), now pinned. Its first run killed 73: the four survivors (the 80-100 day window widened
  to 120, a NaN value read, `--min-pct` ignored, the 500-company cap) were rules no test pinned, and each got one.
  The second run killed 77 of 77. Removing PR #49's lines fails two tests.

## OSDev6's review of #11 (changes needed at 6375c1b)

| Item | Change | The test that proves it (`tests/unit/test_sec_review11.py` unless named) |
|---|---|---|
| H1 look-ahead in the screener | A row's `period_end`, own `as_of` and `filed` are each checked against the screen's `as_of`; undated figures or prices for a past screen are refused; a merged metrics file never lowers its `as_of` and dates each row | `test_h1_osdev6_repro_upstream_file_without_its_as_of`, `test_h1_osdev6_repro_the_merge_keeps_the_files_as_of`, `test_h1_a_row_from_after_as_of_is_refused_by_each_of_its_dates`, `test_h1_undated_figures_are_never_guessed_into_the_past`, `test_h1_a_later_command_never_lowers_the_files_as_of`, `test_h1_a_merge_never_vouches_for_a_row_it_cannot_date`, `test_h1_undated_prices_are_never_guessed_into_the_past` |
| M3 `holdings 1067983 --copy` exits 3 | Weights carried unrounded; a weight that scales to 0 is left out with its true weight | `test_m3_osdev6_repro_berkshire_copy_without_top`, `test_m3_a_position_worth_nothing_is_left_out_by_name`; its top-five numbers: `test_copycat.py::ReelNumbers` |
| M1 the 13F date ignores amendments | `filed` and "made public" are the latest amendment applied's | `test_m1_osdev6_repro_the_holdings_are_dated_by_the_latest_amendment_applied`, `test_m1_the_diff_and_the_copy_are_dated_by_the_amendment_too`, `test_m1_an_amendment_not_applied_does_not_date_the_holdings` |
| M2 UTF-16 bypasses the DOCTYPE refusal | The expat parser refuses a DOCTYPE or entity declaration, after decoding | `test_m2_a_doctype_is_refused_in_any_encoding` (UTF-16 with and without a byte-order mark, a DOCTYPE alone, the cover page), `test_m2_utf16_without_a_doctype_still_parses`, `test_m2_broken_xml_is_still_reported_as_broken_not_as_a_doctype` |
| LOW 1 one bad amendment refuses the quarter | Not applied, with a note | `test_low1_a_refused_amendment_is_not_applied_and_the_quarter_still_loads`, `test_low1_the_original_filing_still_fails_closed` |
| LOW 2 text decides a numeric rule | Counts as missing | `test_low2_text_in_a_numeric_metric_is_unknown_not_a_verdict` |
| LOW 3 holdings text shows 20 rows | Every position, as upstream's | `test_low3_holdings_text_shows_every_position_unless_top_is_given` |
| LOW 4 the 8-K links its primary document | The index, as upstream's | `test_low4_the_earnings_8k_falls_back_to_its_index` |
| LOW 5 a redirect leaves the SEC | Refused | `test_low5_a_redirect_off_the_sec_is_refused`, `test_low5_a_redirect_inside_the_sec_is_followed_and_the_transport_uses_that_opener` |
| LOW 6 two named rules untested | Pinned, with his three survivors | `test_low6_a_13f_filed_on_the_as_of_day_is_public_that_day`, `test_low6_a_fact_with_no_readable_filing_date_is_never_read` |
| LOW 7 today's ticker list, unsaid | `TICKERS_TODAY` on earnings (past `as_of`, by ticker), 13F and screen records | `test_low7_records_say_tickers_come_from_todays_list` |
| LOW 8 `filed` called the first day known | The contract's docstring says `first_filed` is | (docstring) |
| Unnamed changes | Named in the tables above, file by file; the f-strings, the 20-row text and the 20,000-row cap went back | `test_your_cusip_map_gives_tickers_as_text`, `test_a_large_information_table_is_read_whole` |

## What this does not claim

- The fixtures are real SEC responses, but they were captured and trimmed by the upstream author, not by an
  independent tester. S04, S06 and S07 stay `EXPERIMENTAL` until one is (ADR-007).
- Not ported: the upstream SKILL.md text, `--xml` (reading a downloaded table), OpenFIGI, Form 4 links, the screener's
  `--out` file (the record is stored instead), and the command-line flags listed under CLI.
- Not verified: whether EDGAR accepts a 13F in UTF-16 (OSDev6's note on M2). The refusal holds either way.
- The SEC's fair-access terms must be revalidated by the implementer (spec §21).
