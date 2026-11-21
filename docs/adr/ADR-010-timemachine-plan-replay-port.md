# ADR-010: the upstream Time Machine plan replay and ORB study, ported; a plan-replay subject in `timemachine/1`

- **Status:** proposed by OSDev4, 2026-11-20, except the trade minimum, which the founder signed off (below). The
  other numbers in "Numbers to sign off" need the independent quant reviewer's and the founder's sign-off before any
  of them qualifies a result (spec §09.4).
- **Founder sign-off, trade minimum (2026-11-20).** OSDev1 asked Brian "should TraderOS require 100 trades before it
  calls a strategy worth paper trading?" and he answered "Y" (OWNER post by OSDev1 on the wall, 15:59 UTC). So the
  plan replay's `MIN_TRADES` and the lab's `research.MIN_TRADES` are both **100** (ADR-005 records the same). His yes
  covers only the trade minimum: the coin flip, "strongly", many-neither, the interval and the cost stay proposed, and
  every report still says the independent quant review is pending (spec §09.4).
- **Owner rule (Brian, 11-19, relayed by OSDev1 on the wall):** "Python files and all code only gets rewritten if
  there is clearly a flaw or if there is clearly a better method." Every change below names its flaw or its method.
- **Spec:** §12.1 S11, §08.6 Q09, §09.1, §09.2, §09.4, §06.1, §06.5, §08.2, §08.4; QT01, QT02, QT04 to QT07, QT10.
- **Sources** (upstream day-trader-skill, MIT; `tank/PROVENANCE.csv` has each blob and SHA-256):
  - `trader-timemachine/timemachine.py` at `3d0f4ef`, with upstream PRs #49, #25 and #53 applied as published and
    the Time Machine part of #2 merged by hand (OSDev9's upstream PR review v3);
  - `trader-timemachine/backtest.py` and `tests/test_backtest.py` from #53 (head `16133276`);
  - `tests/test_timemachine.py`, `tests/fixtures/trader-timemachine/TSLA-60d5m.json.gz`, `trader-timemachine/SKILL.md`.

## Decision
- **Our daily Time Machine stays** (`traderos.timemachine`, ADR-006) for strategy versions on daily bars, with one
  fix (below). Upstream's has nothing for a typed strategy.
- **The upstream intraday plan replay is ported beside it**, as `traderos.timemachine.plan`. It replays one planned trade
  on a stored 5-minute dataset: whole-session replays, the plan's rule on every past session (the history), a blind
  baseline, block-length sensitivity and three stated stresses.
- **The upstream ORB study (#53's backtest.py) is ported** as `traderos.timemachine.orb_study`, an intraday study of one
  rule. It is not S10: it books R numbers with no cash, position, sizing or settlement, so `traderos.backtest`
  stays the lab's engine (reuse plan, "S10 engine: keep ours").
- **CLI** (additive; `docs/cli-contract-v1.md`): `traderos timemachine <strategy-version-id> --dataset D` is
  unchanged; `traderos timemachine plan --plan FILE --dataset D` (or the plan as flags) and
  `traderos timemachine orb-study --dataset D` are new. A strategy version id can never be `plan` or `orb-study`
  (version ids are `SV-…`). A plan file and the plan's flags are never given together: the flags would be ignored,
  so the pair is refused (OSDev6's review of #9, LOW 7).

## The contract change: a plan-replay subject in `timemachine/1` (additive)
- `TimeMachineReport` gains `plan_replay: PlanReplaySubject | None = None`. `strategy_version_id` and
  `strategy_semantic_hash` become optional. A report has exactly one subject: the two strategy fields, or a
  `plan_replay`. Construction refuses both and neither.
- `PlanReplaySubject` (a new part) records the symbol and instrument, the direction, the session, the plan's entry,
  stop and target, when the entry bar closed and when the replay ends, the setup and stop rule, the plan's hash,
  the as-of instant the data was read at, the fill model (`next_open_bracket/1`), the cost per side and the code
  version (package version and the module's source hash).
- For a plan replay, `window_start`/`window_end` are the first and last pool session and `horizon_sessions` is 1
  (each path is the rest of one session). The history and the blind baseline are OBSERVED, the replays RESAMPLED,
  the stresses STRESS.
- **Compatibility:** every `timemachine/1` JSON written before still parses unchanged (the new key is optional).
  The canonical form writes every field, so a strategy report's canonical bytes now carry `"plan_replay": null`
  and its hash moves; `tests/fixtures/contracts/timemachine_v1.json` is the only golden file that changed. No
  `timemachine/1` record exists outside a test store: the record is on the unmerged `claude/traderos-timemachine`
  branch. A store written by that branch before this change would refuse its old Time Machine reports as hash
  mismatches; delete them and run again.
- **Example and goldens:** `examples.variants()` holds a plan-replay report, frozen in
  `tests/fixtures/contracts/variants/` by `tools/contracts/regen_golden.py` and checked by
  `tests/contract/test_contracts_v1.py`.

## What changed from the upstream plan replay, each with its reason
| Change | Reason |
|---|---|
| Replays draw one whole past session (`session`); a stationary-block variant (mean 6 and 12 bars) is shown beside it | Flaw: S11 forbids independent-bar shuffling. In the reuse comparison's demo 73.75 % of the upstream paths were mixes no session produced, and the TSLA 5-to-95 range was about 22 % too wide |
| Replays fill at the next drawn bar's open, skip a bracket that opens past the stop or the target, exit a gap at the open, charge costs | Flaw: they filled at the entry bar's own close and booked -1R at a stop a bar had gapped through (QT04, QT05, §09.1) |
| Each cost leg on its own price; one default (2 bps a side) everywhere | Flaw: #53 charged both legs on the fill, and history_replay and baseline defaulted to 0 bps while the run defaulted to 2 (QT06). 2 bps equals the lab's default per side |
| R unrounded for every average; signals, fills and rejections counted apart | Flaw: averaging Rs rounded to the cent moves the average (#53 review change 6) |
| Uncertainty: `traderos.research.statistics.stationary_bootstrap_mean` over the trades in date order; the edge is each trade's R minus its own blind entries' average; the "blind trades did as well" share is replaced by the edge's interval | Flaw: #53's iid bootstrap treats trades on nearby days as independent (§09.4) |
| A bar that touches both stop and target still counts as the stop, and is counted as AMBIGUOUS_BAR_ASSUMPTION | §09.1 asks for the label; #2's both_touched count |
| Three stresses, each stated: a 10 % gap against the position at the first bar after the fill; every move doubled in log terms; five times the costs | S11 and QT10: deliberate stress with explicit assumptions, copied from the daily Time Machine |
| Data from a stored 5-minute dataset through the as-of BarReader; an incomplete bar is dropped and counted; an incomplete entry bar is INCOMPLETE_BAR | Flaw: Yahoo is held (§06.1), and a network read has no point-in-time guarantee. The upstream parse rules (grid, regular hours, full 78-bar sessions, today out of the pool) are kept |
| XNYS calendar and offsets | Flaw: a hand-kept 2026-2027 early-close table and a hand-rolled DST rule |
| TMError is a ResearchError with codes: MISSING_DATA, INCOMPLETE_BAR, INSUFFICIENT_EVIDENCE, LOOKAHEAD_RISK (#25), OUTSIDE_CAPABILITY, INVALID_INPUT, RESOURCE_BUDGET_EXCEEDED | §05.3 typed refusals; the upstream words are kept |
| #2 taken in part: the default end is the plan's flat_by; an unexpected error is one line (the CLI's exit 4); the caveats. #2's Monte Carlo next-open mode is not taken | Its next-open mode computes (stop - fill) / risk and books a positive R on a stop-out (#2 review change 2) |
| A plan file is one JSON object of at most 1 MB, naming each key once | §04.2 untrusted input bounds; a repeated key silently kept the last value (OSDev6's review of #9, LOW 6). Floats stay accepted: the Exit Planner's plan shape uses them |
| A plan's prices go through Decimal, and a boolean is not a price (`_num`) | Flaw: `float(True)` read a JSON `true` as the price 1.00 |
| A value the record cannot hold (non-finite, past 2**53, anywhere in the plan, lists included) is refused when the plan is read, naming its place | Flaw: a float in a list under `opening_range` was refused only after the whole replay, in the canonical form's words (OSDev6's review, LOW 11) |
| An opening-range plan whose entry bar closes before its range ends is refused (LOOKAHEAD_RISK); `resolve_rule` reads only bars closed by the entry | Flaw (the upstream code too): with `stop_rule` auto, `resolve_rule` read range bars that closed after the entry, so the stop rule and the history changed with the as-of time (QT01; OSDev6's review, HIGH 1: entry 10:15, a 60-minute range, `pct` at 10:16 but `or_mid` at 10:50) |
| A bar's open decides before any touch (`walk`): both gaps are checked before the stop's and the target's touches | Flaw (#53's code): a bar opening past the target and then trading through the stop was booked as an ambiguous stop (OSDev6's review, MEDIUM 3) |
| The crash stress says "not applied" when no bar follows the fill bar before the end | Flaw: the gap lands on the first bar after the fill, so with only the fill bar left the stress reported the unshocked replays as shocked (a 3:55 PM entry; a 12:55 PM entry on a half day; OSDev6's review, MEDIUM 4) |
| The replay pool is every full session the dataset holds before the plan's date | The upstream pool was the 60 days its one Yahoo request returned; with a stored dataset, the dataset decides how far back it goes. The report and the record give the pool's first and last session and its size |
| The result is a stored `timemachine/1` record; `--write` and argparse `main` are replaced by the store and `traderos timemachine plan` | §11 one store; the frozen CLI |

Kept as upstream wrote it: the plan checks (`normalize_plan`, plus the range-before-entry refusal), its opening-range
rule and stop rules (`cents_away`, `rule_stop`, `resolve_rule`, which now reads only bars closed by the entry), #53's
`walk` and `one_trade` (now with the cost leg, the ambiguous flag and the open deciding first),
`history_replay`, #25's `real_outcome` refusal, the verdict and its notes, the report wording, the fan chart, the
seed (19) and the replay count (10,000). Its independent draw is kept unchanged as `simulate_legacy`, used only by
a test that proves the port reads the 59 sessions the reel read (the upstream reel numbers, bit for bit). A legacy run is
never recorded.

## What changed from #53's backtest.py
| Change | Reason |
|---|---|
| Bars from the store as of a time; the universe is the dataset's instruments; no fetch, no cache | Flaw: Yahoo and a community S&P 500 CSV with no point-in-time date (§06.1, §09.2). The cache's uncaught EOFError on a truncated file and its non-atomic writes go with it (§11.2) |
| Symbols checked against the ticker pattern, resolved to one instrument, counted once; unknown ones named | Flaw: "../ESCAPED" reached a cache path, and two spellings of one file counted its trades twice |
| `--until` and `--draws` checked | Flaw: unchecked values went into the arithmetic |
| The result records the dataset id and hash, as-of, seed, draws and code version | QT07 |
| Costs: each leg on its own price | As above |
| The four variants are one research family (`RF-orb-15m-or_mid-long`): all four are always reported, with the number of earlier runs of the family on that dataset, and each run is a research-memory event; a rerun with the same settings and results is a replay and is not counted | §09.4 and FS10: multiple testing by research family; §08.2: a duplicate is not new evidence |
| The report names the dataset | Flaw: "S&P 500" was hard-coded |

## Our daily Time Machine: the holdout fix
With no window given, the daily Time Machine ran to the last day of the dataset, which is the sealed holdout the
lab uses for the same strategy and dataset, and the lab still reported that holdout as unseen (QT02, §08.4). It now
runs over the development and validation windows only, and stops before the **earliest sealed holdout of the
strategy's research family on that dataset**: this version's own default holdout and the holdout of every experiment
registered for the family (`RF-{strategy_id}`) on that dataset. A revision with a longer warm-up has later default
splits, so using its own splits alone read the holdout registered for an earlier revision, said it had not, and
flipped that experiment to HOLDOUT_COMPROMISED (OSDev6's review of #9, HIGH 2). The observed scenario's first
assumption names the holdout it stopped before. A window a person sets into any of those holdouts still runs, with a
`HOLDOUT_READ` limitation naming each one it reached (a window ending on a holdout's first day has read it); since
88ca16c the lab counts such a holdout as opened, and a later experiment of the family on that dataset reports
HOLDOUT_COMPROMISED. Tests: `tests/unit/test_timemachine.py` (`test_the_default_window_never_reads_the_sealed_holdout`,
`test_the_default_window_stops_before_any_holdout_of_the_family`,
`test_holdout_read_and_the_labs_overlap_are_inclusive_at_the_edges`,
`test_a_time_machine_run_into_the_holdout_opens_it_for_the_lab`).

The daily run's `report_id` now also names the costs and the starting cash: a second run with other costs or cash
returned the first run's report (OSDev6's review, MEDIUM 5).

## Record identity
A `report_id` (daily or plan replay) names the inputs. The record's canonical hash also covers `created_at`, by
design: it is that record, made then. So two stores that run the same inputs hold records with the same `report_id`
and different hashes, and within one store the first record stands and a rerun returns it (OSDev6's review, LOW 9).

## Numbers to sign off
| Number | Value | Where | Status |
|---|---|---|---|
| Minimum history trades before the baseline line says anything | **100** (upstream's) | `timemachine.plan.MIN_TRADES` | **signed off by the founder 2026-11-20** |
| Minimum in-sample trades for `candidate_for_paper` | **100** (was V1's 30 while proposed) | `research.MIN_TRADES`, ADR-005 | **signed off by the founder 2026-11-20** |
| Coin flip / close to break-even | under 5 points from break-even | `plan.COIN_FLIP` | proposed |
| "Strongly" toward the target or the stop | 15 points or more | `plan.STRONG` | proposed; the word is withheld |
| Many replays hit neither | more than 40 % | `plan.MANY_NEITHER` | proposed |
| Edge interval | 95 %, stationary bootstrap, 10,000 resamples, block max(5, n^(1/3)) | `plan.baseline` | proposed |
| Cost per side | 2 bps (equals the lab's default per side) | `plan.DEFAULT_COST_BPS` | proposed |

**The trade minimum is settled; the rest is not.** This ADR first stated the upstream 100 beside the lab's 30 and chose
neither; the founder chose 100 for both (above). Until the other numbers are signed off (`plan.SIGNED_OFF` stays
False), and while the independent quant review is pending (spec §09.4):
- the plan replay's "too few to tell" line uses 100, and every report says "Trade minimum: 100 trades, signed off
  by the founder 2026-11-20 (the lab's is the same, ADR-005)", that the other thresholds, the interval and the cost
  are proposals, and that the independent quant review is pending; the record's baseline scenario says the same;
- no result is called "strongly" anything: past the 15-point line the verdict says "Leans" and a note says why;
- a baseline line at 100 trades or more (the only kind that compares the edge with zero) ends "Not a
  qualification: the interval and the verdict's other thresholds are proposals (ADR-010), and the independent quant
  review is pending (spec 09.4)"; under 100 the line says "Too few trades to tell" and claims nothing;
- every record carries `THRESHOLDS_NOT_SIGNED_OFF`, naming the trade minimum as signed off and the others as still
  proposed.

## Fixture rights (spec §06.5)
`tests/fixtures/timemachine/TSLA-60d5m.json.gz` is Yahoo data in the upstream MIT test folder. Its rights are unverified.
It stays under `tests/fixtures/` with a README saying it is test-only and never shipped, beside the upstream MIT notice
(`NOTICE`). Before any public package or repository carries the tests, clear its rights or replace it.

## Evidence
- `tests/unit/test_timemachine_plan.py`: the upstream tests, ported (its reel bit for bit as `LegacyReel`); the review's
  cases for #25, #53 and #2; a regression test for each flaw above; the CLI.
- `tests/unit/test_orb_study.py`: #53's three tests, ported, and the backtest.py review items.
- Mutants: `tools/mutants/timemachine.py` (run `python tools/mutants/timemachine.py`) holds 70 edits of the rules:
  - **port** (40): the session draw, the fill, the rejected bracket, the gap exit, the stop-first order, the
    ambiguous flag, the cost legs and default, unrounded R, the stationary bootstrap, the edge series, the trade
    minimum and its comparison, the withheld "strongly", the not-a-qualification tail, the #25 refusal and its code,
    incomplete bars, the as-of read, today out of the pool, each stress, #2's flat_by default and green-from-the-fill,
    the plan file bound, the legacy refusal, the holdout default and HOLDOUT_READ, both subject checks, the study's
    symbol, until, draws and replay checks, the CLI's mode check;
  - **osdev6** (13): the 7 OSDev6 found surviving in his review of #9 (walk's stop touch and ambiguous flag at
    equality, a bracket rejected at the stop, the last bar in `real_outcome`, HOLDOUT_READ's edge, the lab's overlap
    edge, a close equal to the range high) with their mirrors (short side, the target, the other edge);
  - **review** (17): the fixes for that review (HIGH 1 and 2, MEDIUM 3 to 5, LOW 6, 7 and 11, the trade-minimum text).

  All 70 fail the tests (70 of 70 killed, Python 3.12). Measured with `python -B`, PYTHONDONTWRITEBYTECODE and every
  `__pycache__` removed before and after each mutant, so no mutant is judged by another's cached bytecode.
